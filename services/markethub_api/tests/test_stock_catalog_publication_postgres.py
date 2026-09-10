from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app import app  # noqa: E402
from services import stocks, versioned_object_cache  # noqa: E402
from services.stock_catalog_candidate import (  # noqa: E402
    CatalogSourceEvidence,
    build_stock_catalog_candidate,
)
from services.stock_catalog_publication import (  # noqa: E402
    current_stock_catalog_version,
    publish_stock_catalog_candidate,
    resolve_stock_catalog_data_version,
)


def _database_configured() -> bool:
    return all(
        os.getenv(name, "")
        for name in (
            "MARKETHUB_DB_HOST",
            "MARKETHUB_DB_PORT",
            "MARKETHUB_DB_NAME",
            "MARKETHUB_DB_USER",
            "MARKETHUB_DB_PASSWORD",
        )
    )


def _candidate(name: str):
    return build_stock_catalog_candidate(
        [
            {
                "identity_status": "authoritative",
                "identity_source": "tushare_catalog",
                "market": "SHSE",
                "code": "600000",
                "name": name,
                "listing_board": "主板",
                "listed_date": "1999-11-10",
                "delisted_date": "",
                "industry": "银行",
                "area": "上海",
                "list_status": "L",
            }
        ],
        CatalogSourceEvidence(
            input_id="a" * 64,
            content_sha256="b" * 64,
            provider="tushare",
            source_refreshed_at=datetime(2026, 9, 10, 9, tzinfo=UTC),
            fresh_through=date(2026, 9, 9),
            provisional_count=0,
            conflict_count=0,
        ),
        expected_fresh_through=date(2026, 9, 9),
    )


@pytest.mark.skipif(
    not _database_configured(), reason="requires an isolated PostgreSQL catalog test database"
)
def test_postgres_publication_integrity_and_pages() -> None:
    with psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
    ) as connection:
        connection.execute("create schema if not exists audit")

    old = _candidate("旧版本")
    new = _candidate("新版本")
    publish_stock_catalog_candidate(old, data_version="mhf-v1-old", known_dirty_data_versions=())
    publish_stock_catalog_candidate(
        new, data_version="mhf-v1-new", known_dirty_data_versions=("mhf-v1-dirty",)
    )

    assert current_stock_catalog_version() is not None
    assert resolve_stock_catalog_data_version("mhf-v1-old").catalog_version == old.version
    assert resolve_stock_catalog_data_version("mhf-v1-new").catalog_version == new.version
    with pytest.raises(HTTPException) as dirty:
        resolve_stock_catalog_data_version("mhf-v1-dirty")
    assert dirty.value.detail["code"] == "CATALOG_DATA_VERSION_QUARANTINED"

    versioned_object_cache.clear()
    stocks._REFERENCE_RESPONSE_CACHE.clear()
    client = TestClient(app)
    old_page = client.get(
        "/api/stocks/catalog?include_delisted=true&limit=5000&offset=0&data_version=mhf-v1-old"
    )
    new_page = client.get(
        "/api/stocks/catalog?include_delisted=true&limit=5000&offset=0&data_version=mhf-v1-new"
    )
    not_modified = client.get(
        "/api/stocks/catalog?include_delisted=true&limit=5000&offset=0&data_version=mhf-v1-new",
        headers={"If-None-Match": new_page.headers["ETag"]},
    )

    assert old_page.status_code == 200 and old_page.json()[0]["name"] == "旧版本"
    assert new_page.status_code == 200 and new_page.json()[0]["name"] == "新版本"
    assert not_modified.status_code == 304
