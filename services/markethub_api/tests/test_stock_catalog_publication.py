from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from services.stock_catalog_candidate import CatalogSourceEvidence, build_stock_catalog_candidate
from services.stock_catalog_publication import (
    CatalogPublicationReadiness,
    current_stock_catalog_version,
    publish_stock_catalog_candidate,
)
from app import app
import main


def _evidence(**changes: object) -> CatalogSourceEvidence:
    payload: dict[str, object] = {
        "input_id": "a" * 64,
        "content_sha256": "b" * 64,
        "provider": "tushare",
        "source_refreshed_at": datetime(2026, 9, 10, 9, tzinfo=timezone.utc),
        "fresh_through": date(2026, 9, 9),
        "provisional_count": 1,
        "conflict_count": 0,
    }
    payload.update(changes)
    return CatalogSourceEvidence(**payload)


def _row(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "identity_status": "authoritative",
        "identity_source": "tushare_catalog",
        "market": "SZSE",
        "code": "301699",
        "name": "  回归样例  ",
        "listing_board": "创业板",
        "listed_date": "2026-09-08",
        "delisted_date": "",
        "industry": "",
        "area": "",
    }
    payload.update(changes)
    return payload


class _Result:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self.row = row

    def fetchone(self) -> dict[str, object] | None:
        return self.row


class _PublicationConnection:
    def __init__(self, *, fail_audit: bool = False, previous_version: str = "") -> None:
        self.fail_audit = fail_audit
        self.previous_version = previous_version
        self.calls: list[str] = []
        self.batch: list[tuple[object, ...]] = []

    def __enter__(self) -> _PublicationConnection:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, sql: str, _params: object = ()) -> _Result:
        self.calls.append(sql)
        if "from readmodel.stock_catalog_current" in sql:
            return _Result(
                {"catalog_version": self.previous_version} if self.previous_version else None
            )
        if "count(*)::int as row_count" in sql:
            return _Result({"row_count": len(self.batch)})
        if "insert into audit.stock_catalog_publication_attempt" in sql and self.fail_audit:
            raise OSError("audit storage unavailable")
        return _Result()

    def cursor(self) -> _PublicationConnection:
        return self

    def executemany(self, _sql: str, values: list[tuple[object, ...]]) -> None:
        self.batch.extend(values)


def test_publication_serializes_and_switches_only_after_audited_snapshot_registration() -> None:
    connection = _PublicationConnection(previous_version="mhc-v1-previous")
    candidate = build_stock_catalog_candidate(
        [_row()], _evidence(), expected_fresh_through=date(2026, 9, 9)
    )

    result = publish_stock_catalog_candidate(
        candidate,
        data_version="mhf-v1-new-healthy-version",
        known_dirty_data_versions=("mhf-v1-dirty",),
        connection_factory=lambda: connection,
    )

    assert result.catalog_version == candidate.version
    assert result.previous_catalog_version == "mhc-v1-previous"
    assert result.row_count == 1
    audit_index = next(
        index
        for index, call in enumerate(connection.calls)
        if "stock_catalog_publication_attempt" in call
    )
    current_index = next(
        index
        for index, call in enumerate(connection.calls)
        if "insert into readmodel.stock_catalog_current" in call
    )
    quarantine_index = next(
        index
        for index, call in enumerate(connection.calls)
        if "catalog integrity gate failed" in call
    )
    assert audit_index < quarantine_index < current_index
    assert any("pg_advisory_xact_lock" in call for call in connection.calls)
    assert any("interval '24 hours'" in call for call in connection.calls)


def test_failed_audit_cannot_partially_switch_current_or_cacheable_version() -> None:
    connection = _PublicationConnection(fail_audit=True)
    candidate = build_stock_catalog_candidate(
        [_row()], _evidence(), expected_fresh_through=date(2026, 9, 9)
    )

    with pytest.raises(OSError, match="audit storage"):
        publish_stock_catalog_candidate(
            candidate,
            data_version="mhf-v1-new-healthy-version",
            known_dirty_data_versions=(),
            connection_factory=lambda: connection,
        )

    assert any("pg_advisory_xact_lock" in call for call in connection.calls)
    assert not any(
        "insert into readmodel.stock_catalog_current" in call for call in connection.calls
    )


class _StateConnection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    def __enter__(self) -> _StateConnection:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, _sql: str) -> _Result:
        return _Result(self.row)


def test_current_lookup_advertises_only_a_healthy_catalog_version() -> None:
    healthy = current_stock_catalog_version(
        connection_factory=lambda: _StateConnection(
            {"catalog_version": "mhc-v1-healthy", "activated_at_utc": "2026-09-10T00:00:00+00:00"}
        )
    )
    unavailable = current_stock_catalog_version(connection_factory=lambda: _StateConnection(None))

    assert healthy is not None and healthy.catalog_version == "mhc-v1-healthy"
    assert unavailable is None


def test_health_fails_closed_when_the_active_catalog_registry_has_no_healthy_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main,
        "catalog_publication_readiness",
        lambda: CatalogPublicationReadiness(registry_active=True, current=None),
    )

    response = TestClient(app).get("/api/health")

    assert response.status_code == 503
    assert response.json()["code"] == "CATALOG_UNAVAILABLE"
