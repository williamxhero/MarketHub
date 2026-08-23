from __future__ import annotations

import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from quotemux.models import StockBasicInfo
from app import app
from services import stocks


def test_catalog_encoded_cache_is_versioned_immutable_and_reuses_bytes(monkeypatch) -> None:
    calls = 0

    def load(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return [StockBasicInfo(
            code="600000", name="浦发银行", exchange="SHSE", market="主板", list_status="L",
            list_date="1999-11-10", delist_date="",
        )]

    monkeypatch.setattr(stocks, "require_market_data_version", lambda value: value)
    monkeypatch.setattr(stocks, "current_dataset_version", lambda _dataset: "mhd-v1-reference-cache-test")
    monkeypatch.setattr(stocks._QUOTEMUX.stocks, "get_catalog", load)

    first = stocks.get_catalog_encoded("", "", "", "", False, 5000, 0, "mhf-v1-test")
    second = stocks.get_catalog_encoded("", "", "", "", False, 5000, 0, "mhf-v1-test")

    assert calls == 1
    assert first is second
    assert json.loads(first.content)[0]["code"] == "600000"
    assert first.headers["Cache-Control"] == "public,max-age=31536000,immutable"
    assert first.headers["Vary"] == "Accept, Accept-Encoding"
    assert first.headers["ETag"].startswith('"')


def test_catalog_endpoint_serves_cached_bytes_and_304(monkeypatch) -> None:
    encoded = stocks.EncodedReferenceResponse(
        content=b'[{"code":"600000"}]',
        headers={"ETag": '"catalog-test"', "Cache-Control": "public,max-age=31536000,immutable", "Vary": "Accept, Accept-Encoding"},
    )
    monkeypatch.setattr(stocks, "get_catalog_encoded", lambda *_args: encoded)
    client = TestClient(app)

    response = client.get("/api/stocks/catalog?data_version=mhf-v1-test")
    not_modified = client.get(
        "/api/stocks/catalog?data_version=mhf-v1-test",
        headers={"If-None-Match": '"catalog-test"'},
    )

    assert response.status_code == 200
    assert response.content == encoded.content
    assert response.headers["etag"] == '"catalog-test"'
    assert not_modified.status_code == 304
    assert not_modified.content == b""
