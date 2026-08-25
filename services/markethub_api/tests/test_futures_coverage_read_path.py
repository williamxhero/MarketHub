from __future__ import annotations

from pathlib import Path
import sys


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from quotemux.infra.db.read_client import QueryBatch
from quotemux.strict_read import strict_public_read_boundary
from services import futures


def test_futures_coverage_uses_strict_public_reader(monkeypatch) -> None:
    batch = QueryBatch(
        ("product_code", "exchange", "series_type", "row_count", "first_bar_time", "last_bar_time"),
        (("IF", "CFFEX", "main_continuous", 10, "2026-08-20 09:31:00", "2026-08-20 15:00:00"),),
    )
    monkeypatch.setattr(futures._PUBLIC_READER, "list_futures_coverage_batch", lambda value: batch)
    monkeypatch.setattr(
        futures._QUOTEMUX.futures,
        "list_coverage",
        lambda _value: (_ for _ in ()).throw(AssertionError("provider-capable path must not run")),
    )

    assert futures.list_coverage("main_continuous") == [{
        "product_code": "IF", "exchange": "CFFEX", "series_type": "main_continuous", "row_count": 10,
        "first_bar_time": "2026-08-20 09:31:00", "last_bar_time": "2026-08-20 15:00:00",
    }]


def test_futures_quotes_use_strict_public_reader_without_provider_or_schema_write(monkeypatch) -> None:
    batch = QueryBatch(
        ("product_code", "exchange", "series_type", "bar_time", "open", "high", "low", "close", "volume", "open_interest", "adjustment_offset"),
        (("ag", "SHFE", "back_adjusted_continuous", "2018-11-29 13:31:00", 1.0, 2.0, 0.5, 1.5, 10.0, 20.0, 0.0),),
    )
    captured: dict[str, object] = {}

    def strict_read(codes: str, series_type: str, start_time: str, end_time: str, *, limit: int) -> QueryBatch:
        captured.update(codes=codes, series_type=series_type, start_time=start_time, end_time=end_time, limit=limit)
        return batch

    monkeypatch.setattr(futures._PUBLIC_READER, "get_futures_quotes_1m_batch", strict_read, raising=False)
    monkeypatch.setattr(
        futures._QUOTEMUX.futures,
        "get_quotes_1m",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider-capable schema path must not run")),
    )

    with strict_public_read_boundary():
        items = futures.get_quotes_1m("ag", "back_adjusted_continuous", "2018-11-29 13:31:00", "2018-11-29 13:52:00", 100)

    assert captured == {
        "codes": "ag", "series_type": "back_adjusted_continuous",
        "start_time": "2018-11-29 13:31:00", "end_time": "2018-11-29 13:52:00", "limit": 100,
    }
    assert items[0] == futures.FutureBar1mItem(
        product_code="ag", exchange="SHFE", series_type="back_adjusted_continuous", bar_time="2018-11-29 13:31:00",
        open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0, open_interest=20.0, adjustment_offset=0.0,
    )
