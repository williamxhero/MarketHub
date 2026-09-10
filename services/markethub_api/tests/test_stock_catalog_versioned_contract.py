from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from services import market_data_version, stocks, versioned_object_cache
from services.stock_catalog_publication import (
    CatalogCurrentVersion,
    CatalogPublicationReadiness,
    ResolvedCatalogVersion,
    read_stock_catalog_page,
    resolve_stock_catalog_data_version,
)
from app import app


class _Result:
    def __init__(self, row: dict[str, object] | None = None, rows: list[dict[str, object]] | None = None) -> None:
        self.row = row
        self.rows = rows or []

    def fetchone(self) -> dict[str, object] | None:
        return self.row

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class _VersionConnection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    def __enter__(self) -> _VersionConnection:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, sql: str, _params: object) -> _Result:
        if "select 1 as retained" in sql:
            return _Result({"retained": 1})
        return _Result(self.row)


def test_resolver_accepts_retained_healthy_version_and_rejects_quarantine() -> None:
    healthy = resolve_stock_catalog_data_version(
        "mhf-v1-healthy",
        connection_factory=lambda: _VersionConnection({"data_version": "mhf-v1-healthy", "catalog_version": "mhc-v1-healthy", "status": "healthy", "reason": ""}),
    )

    assert healthy == ResolvedCatalogVersion("mhf-v1-healthy", "mhc-v1-healthy")

    with pytest.raises(HTTPException) as error:
        resolve_stock_catalog_data_version(
            "mhf-v1-quarantined",
            connection_factory=lambda: _VersionConnection({"data_version": "mhf-v1-quarantined", "catalog_version": None, "status": "quarantined", "reason": "bad names"}),
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "CATALOG_DATA_VERSION_QUARANTINED"

    with pytest.raises(HTTPException) as stale_error:
        resolve_stock_catalog_data_version(
            "mhf-v1-stale",
            connection_factory=lambda: _VersionConnection(None),
        )

    assert stale_error.value.status_code == 409
    assert stale_error.value.detail["code"] == "CATALOG_DATA_VERSION_STALE"


class _PageConnection:
    def __init__(self) -> None:
        self.query = ""
        self.params: tuple[object, ...] = ()

    def __enter__(self) -> _PageConnection:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, query: str, params: tuple[object, ...]) -> _Result:
        self.query = query
        self.params = params
        return _Result(
            rows=[
                {"code": "301699", "name": "样例一", "exchange": "SZSE", "market": "创业板", "list_status": "L", "list_date": "2026-09-08", "delist_date": "", "industry": "", "listing_board": "创业板", "area": ""},
                {"code": "920268", "name": "样例二", "exchange": "BJSE", "market": "北交所", "list_status": "L", "list_date": "2026-09-08", "delist_date": "", "industry": "", "listing_board": "北交所", "area": ""},
            ]
        )


def test_page_reads_one_immutable_snapshot_with_a_stable_scope() -> None:
    connection = _PageConnection()
    items = read_stock_catalog_page(
        ResolvedCatalogVersion("mhf-v1-health", "mhc-v1-snapshot"),
        codes=[], name="", exchange="", list_status="", include_delisted=True, limit=5000, offset=0,
        connection_factory=lambda: connection,
    )

    assert [item["code"] for item in items] == ["301699", "920268"]
    assert all(item["name"].strip() for item in items)
    assert "where catalog_version=%s" in connection.query
    assert connection.params[0] == "mhc-v1-snapshot"


def test_catalog_cache_uses_snapshot_version_and_never_calls_live_reader_when_registry_is_active(monkeypatch: pytest.MonkeyPatch) -> None:
    versioned_object_cache.clear()
    stocks._REFERENCE_RESPONSE_CACHE.clear()
    calls: list[str] = []
    current = {"version": "mhc-v1-first"}
    monkeypatch.setattr(
        stocks,
        "catalog_publication_readiness",
        lambda: CatalogPublicationReadiness(True, CatalogCurrentVersion(current["version"], "2026-09-10T00:00:00+00:00")),
    )
    monkeypatch.setattr(
        stocks,
        "resolve_stock_catalog_data_version",
        lambda data_version: ResolvedCatalogVersion(data_version, current["version"]),
    )
    monkeypatch.setattr(
        stocks,
        "read_stock_catalog_page",
        lambda version, **_kwargs: calls.append(version.catalog_version) or [
            {"code": "600000", "name": "浦发银行", "exchange": "SHSE", "market": "主板", "list_status": "L", "list_date": "1999-11-10", "delist_date": "", "industry": "", "listing_board": "主板", "area": ""}
        ],
    )
    monkeypatch.setattr(stocks._QUOTEMUX.stocks, "get_catalog", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("snapshot path must not use the live reader")))

    first = stocks.get_catalog_encoded("", "", "", "", True, 5000, 0, "mhf-v1-health")
    same_snapshot = stocks.get_catalog_encoded("", "", "", "", True, 5000, 0, "mhf-v1-health")
    versioned_object_cache.clear()
    stocks._REFERENCE_RESPONSE_CACHE.clear()
    restarted = stocks.get_catalog_encoded("", "", "", "", True, 5000, 0, "mhf-v1-health")
    current["version"] = "mhc-v1-second"
    next_snapshot = stocks.get_catalog_encoded("", "", "", "", True, 5000, 0, "mhf-v1-new-health")

    assert calls == ["mhc-v1-first", "mhc-v1-first", "mhc-v1-second"]
    assert first.content == same_snapshot.content
    assert first.headers["ETag"] == same_snapshot.headers["ETag"]
    assert restarted.content == first.content
    assert restarted.headers["ETag"] == first.headers["ETag"]
    assert next_snapshot.content == first.content


def test_health_data_version_changes_only_when_catalog_snapshot_or_market_facts_change(monkeypatch: pytest.MonkeyPatch) -> None:
    current = {"catalog_version": "mhc-v1-first"}
    monkeypatch.setattr(market_data_version, "_current_market_data_base_version", lambda: "mhf-v1-facts")
    monkeypatch.setattr(
        "services.stock_catalog_publication.catalog_publication_readiness",
        lambda: CatalogPublicationReadiness(True, CatalogCurrentVersion(current["catalog_version"], "2026-09-10T00:00:00+00:00")),
    )

    first = market_data_version.current_market_data_version()
    replay = market_data_version.current_market_data_version()
    current["catalog_version"] = "mhc-v1-second"
    switched = market_data_version.current_market_data_version()

    assert first == replay
    assert switched != first


def test_catalog_http_keeps_success_shape_etag_and_304_while_quarantine_bypasses_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    versioned_object_cache.clear()
    stocks._REFERENCE_RESPONSE_CACHE.clear()
    page_calls: list[str] = []
    monkeypatch.setattr(
        stocks,
        "catalog_publication_readiness",
        lambda: CatalogPublicationReadiness(True, CatalogCurrentVersion("mhc-v1-current", "2026-09-10T00:00:00+00:00")),
    )

    def resolve(data_version: str) -> ResolvedCatalogVersion:
        if data_version == "mhf-v1-quarantined":
            raise HTTPException(status_code=409, detail={"code": "CATALOG_DATA_VERSION_QUARANTINED", "message": "re-pin"})
        return ResolvedCatalogVersion(data_version, "mhc-v1-current")

    monkeypatch.setattr(stocks, "resolve_stock_catalog_data_version", resolve)
    monkeypatch.setattr(
        stocks,
        "read_stock_catalog_page",
        lambda version, **_kwargs: page_calls.append(version.catalog_version) or [
            {"code": "600000", "name": "浦发银行", "exchange": "SHSE", "market": "主板", "list_status": "L", "list_date": "1999-11-10", "delist_date": "", "industry": "", "listing_board": "主板", "area": ""}
        ],
    )
    client = TestClient(app)

    healthy = client.get("/api/stocks/catalog?data_version=mhf-v1-current")
    cached = client.get(
        "/api/stocks/catalog?data_version=mhf-v1-current",
        headers={"If-None-Match": healthy.headers["ETag"]},
    )
    quarantined = client.get("/api/stocks/catalog?data_version=mhf-v1-quarantined")

    assert healthy.status_code == 200
    assert healthy.json() == [{"code": "600000", "name": "浦发银行", "exchange": "SHSE", "market": "主板", "list_status": "L", "list_date": "1999-11-10", "delist_date": "", "industry": "", "listing_board": "主板", "area": ""}]
    assert cached.status_code == 304
    assert quarantined.status_code == 409
    assert quarantined.json()["code"] == "CATALOG_DATA_VERSION_QUARANTINED"
    assert page_calls == ["mhc-v1-current"]


def test_concurrent_pages_stay_with_their_pinned_immutable_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    versioned_object_cache.clear()
    snapshots = {
        "mhf-v1-old": "mhc-v1-old",
        "mhf-v1-new": "mhc-v1-new",
    }
    monkeypatch.setattr(
        stocks,
        "catalog_publication_readiness",
        lambda: CatalogPublicationReadiness(True, CatalogCurrentVersion("mhc-v1-new", "2026-09-10T00:00:00+00:00")),
    )
    monkeypatch.setattr(
        stocks,
        "resolve_stock_catalog_data_version",
        lambda data_version: ResolvedCatalogVersion(data_version, snapshots[data_version]),
    )
    monkeypatch.setattr(
        stocks,
        "read_stock_catalog_page",
        lambda version, **kwargs: [
            {
                "code": f"00000{int(kwargs['offset']) + 1}",
                "name": f"{version.catalog_version}-page-{kwargs['offset']}",
                "exchange": "SHSE",
                "market": "主板",
                "list_status": "L",
                "list_date": "1999-11-10",
                "delist_date": "",
                "industry": "",
                "listing_board": "主板",
                "area": "",
            }
        ],
    )

    with ThreadPoolExecutor(max_workers=3) as pool:
        old_page_one = pool.submit(stocks.get_catalog, "", "", "", "", True, 1, 0, "mhf-v1-old")
        old_page_two = pool.submit(stocks.get_catalog, "", "", "", "", True, 1, 1, "mhf-v1-old")
        new_page_one = pool.submit(stocks.get_catalog, "", "", "", "", True, 1, 0, "mhf-v1-new")

    assert old_page_one.result()[0].name == "mhc-v1-old-page-0"
    assert old_page_two.result()[0].name == "mhc-v1-old-page-1"
    assert new_page_one.result()[0].name == "mhc-v1-new-page-0"
