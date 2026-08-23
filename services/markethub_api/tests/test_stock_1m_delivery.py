from __future__ import annotations

import json
from pathlib import Path
import sys

from fastapi import HTTPException
import pyarrow as pa
import pytest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from quotemux.infra.db.read_client import QueryBatch
from routers.stock_quote_models import StockQuotesQueryPayload
from services import stock_1m_delivery


class _FakeStream:
    def __init__(self, coverage: QueryBatch, batches: tuple[QueryBatch, ...]) -> None:
        self.coverage = coverage
        self._batches = batches
        self.closed = False

    def __enter__(self):
        return self

    def __iter__(self):
        return iter(self._batches)

    def __exit__(self, *_args):
        self.closed = True
        return False


def _payload() -> StockQuotesQueryPayload:
    return StockQuotesQueryPayload(codes=["600000"], freq="1m", trade_date="2026-08-14", dataset_version="mhd-v1-test")


def test_stock_1m_arrow_streams_record_batches_without_row_models(monkeypatch) -> None:
    monkeypatch.setattr(stock_1m_delivery, "_is_open_trade_date", lambda _date: True)
    coverage = QueryBatch(("code", "row_count", "first_trade_time", "last_trade_time"), (("600000", 240, "2026-08-14 09:31:00", "2026-08-14 15:00:00"),))
    rows = tuple(("600000", f"2026-08-14 09:{31 + index:02d}:00", 1.0, 2.0, 0.5, 1.5, 10.0, 20.0) for index in range(29))
    rows += tuple(("600000", f"2026-08-14 10:{index:02d}:00", 1.0, 2.0, 0.5, 1.5, 10.0, 20.0) for index in range(60))
    rows += tuple(("600000", f"2026-08-14 11:{index:02d}:00", 1.0, 2.0, 0.5, 1.5, 10.0, 20.0) for index in range(31))
    rows += tuple(("600000", f"2026-08-14 13:{index + 1:02d}:00", 1.0, 2.0, 0.5, 1.5, 10.0, 20.0) for index in range(59))
    rows += tuple(("600000", f"2026-08-14 14:{index:02d}:00", 1.0, 2.0, 0.5, 1.5, 10.0, 20.0) for index in range(60))
    rows += (("600000", "2026-08-14 15:00:00", 1.0, 2.0, 0.5, 1.5, 10.0, 20.0),)
    batch = QueryBatch(("code", "trade_time", "open", "high", "low", "close", "volume", "amount"), rows)
    stream = _FakeStream(coverage, (batch,))
    monkeypatch.setattr(stock_1m_delivery, "_READER", type("Reader", (), {"open_stock_1m_batch_stream": lambda *_args, **_kwargs: stream})())
    monkeypatch.setattr(stock_1m_delivery, "require_dataset_version", lambda *_args: "mhd-v1-test")

    prepared = stock_1m_delivery.prepare_arrow(_payload())
    content = b"".join(prepared.body)
    reader = pa.ipc.open_stream(content)

    assert reader.read_all().num_rows == 240
    assert json.loads(reader.schema.metadata[b"markethub.meta"])["dataset_version"] == "mhd-v1-test"
    assert stream.closed is True


def test_stock_1m_json_uses_same_order_and_coverage(monkeypatch) -> None:
    monkeypatch.setattr(stock_1m_delivery, "_is_open_trade_date", lambda _date: True)
    coverage = QueryBatch(("code", "row_count", "first_trade_time", "last_trade_time"), (("600000", 240, "2026-08-14 09:31:00", "2026-08-14 15:00:00"),))
    batch = QueryBatch(("code", "trade_time", "open", "high", "low", "close", "volume", "amount"), (("600000", "2026-08-14 09:31:00", 1.0, 2.0, .5, 1.5, 10.0, 20.0),))
    stream = _FakeStream(coverage, (batch,))
    monkeypatch.setattr(stock_1m_delivery, "_READER", type("Reader", (), {"open_stock_1m_batch_stream": lambda *_args, **_kwargs: stream})())
    monkeypatch.setattr(stock_1m_delivery, "require_dataset_version", lambda *_args: "mhd-v1-test")
    payload = StockQuotesQueryPayload(codes=["600000"], freq="1m", start_time="2026-08-14 09:31:00", end_time="2026-08-14 09:31:00", dataset_version="mhd-v1-test")

    result = stock_1m_delivery.build_json(payload)

    assert result["items"][0]["trade_time"] == "2026-08-14 09:31:00"
    assert result["meta"]["complete"] is True


def test_stock_1m_incomplete_fails_before_streaming(monkeypatch) -> None:
    monkeypatch.setattr(stock_1m_delivery, "_is_open_trade_date", lambda _date: True)
    coverage = QueryBatch(("code", "row_count", "first_trade_time", "last_trade_time"), (("600000", 239, "", ""),))
    stream = _FakeStream(coverage, ())
    monkeypatch.setattr(stock_1m_delivery, "_READER", type("Reader", (), {"open_stock_1m_batch_stream": lambda *_args, **_kwargs: stream})())
    monkeypatch.setattr(stock_1m_delivery, "require_dataset_version", lambda *_args: "mhd-v1-test")

    with pytest.raises(HTTPException) as error:
        stock_1m_delivery.prepare_arrow(_payload())

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "DATA_INCOMPLETE"
    assert stream.closed is True
