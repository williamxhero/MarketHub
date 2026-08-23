from __future__ import annotations

import json
from pathlib import Path
import sys

import pyarrow as pa
from fastapi.testclient import TestClient


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app import app
from services import stock_quotes_arrow, stocks


def _result() -> dict[str, object]:
    return {
        "items": [
            {
                "code": "600000",
                "trade_time": "2026-08-14 09:31:00",
                "freq": "1m",
                "open": 10.0,
                "high": 10.2,
                "low": 9.9,
                "close": 10.1,
                "pre_close": None,
                "change": None,
                "pct_chg": None,
                "volume": 100.0,
                "amount": 1000.0,
                "adjust": "none",
                "is_suspended": False,
                "is_st": False,
            }
        ],
        "meta": {"data_version": "mhf-v1-test", "total_rows": 1, "returned_rows": 1, "complete": True, "truncated": False, "codes": []},
    }


def test_stock_quote_arrow_keeps_items_and_meta_equivalent() -> None:
    encoded = stock_quotes_arrow.encode(_result())
    reader = pa.ipc.open_stream(encoded.content)

    assert reader.read_all().to_pylist() == _result()["items"]
    assert json.loads(reader.schema.metadata[b"markethub.meta"]) == _result()["meta"]
    assert encoded.headers["X-MarketHub-Returned-Rows"] == "1"
    assert encoded.headers["X-MarketHub-Data-Version"] == "mhf-v1-test"


def test_stock_quote_query_negotiates_arrow_and_rejects_unknown_media(monkeypatch) -> None:
    monkeypatch.setattr(stocks, "get_quotes_query_result", lambda *_args: type("Result", (), {"model_dump": lambda self: _result(), "items": []})())
    client = TestClient(app)
    payload = {"codes": ["600000"], "freq": "1m", "trade_date": "2026-08-14", "data_version": "mhf-v1-test"}

    arrow = client.post("/api/stocks/quotes/query", json=payload, headers={"Accept": stock_quotes_arrow.ARROW_MEDIA_TYPE})
    rejected = client.post("/api/stocks/quotes/query", json=payload, headers={"Accept": "text/csv"})

    assert arrow.status_code == 200
    assert arrow.headers["content-type"].startswith(stock_quotes_arrow.ARROW_MEDIA_TYPE)
    assert pa.ipc.open_stream(arrow.content).read_all().num_rows == 1
    assert rejected.status_code == 406
