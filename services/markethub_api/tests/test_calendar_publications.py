from __future__ import annotations

from pathlib import Path
import sys


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from platform_models import TradingCalendarItem
from services import calendar_publications, market_data_version, markets


OLD_VERSION = "mhf-v1-3383f7b3b712cf987e884034481e4778945979db59d391afe9544273515b00ac"


def test_publication_reader_resolves_exact_frozen_range(monkeypatch) -> None:
    import pandas as pd

    responses = iter(
        (
            pd.DataFrame(
                [
                    {
                        "snapshot_sha256": "a" * 64,
                        "range_start": "2012-01-01",
                        "range_end": "2026-08-11",
                        "row_count": 2,
                        "open_day_count": 2,
                    }
                ]
            ),
            pd.DataFrame(
                [
                    {"trade_date": "2012-01-04", "is_open": True},
                    {"trade_date": "2012-01-05", "is_open": True},
                ]
            ),
        )
    )
    monkeypatch.setattr(calendar_publications, "query_dataframe", lambda *_args: next(responses))

    items = calendar_publications.read_published_trading_calendar(
        OLD_VERSION, "SSE", "2012-01-01", "2026-08-11", True
    )

    assert items == [
        TradingCalendarItem(exchange="SSE", trade_date="2012-01-04", is_open=True),
        TradingCalendarItem(exchange="SSE", trade_date="2012-01-05", is_open=True),
    ]


def test_historical_published_calendar_survives_global_version_drift(monkeypatch) -> None:
    expected = [TradingCalendarItem(exchange="SSE", trade_date="2012-01-04", is_open=True)]
    monkeypatch.setattr(market_data_version, "current_market_data_version", lambda: "mhf-v1-current")
    monkeypatch.setattr(
        markets,
        "read_published_trading_calendar",
        lambda version, exchange, start_date, end_date, is_open: expected
        if version == OLD_VERSION
        else None,
    )
    monkeypatch.setattr(
        markets._QUOTEMUX.markets,
        "get_trading_calendar",
        lambda _request: (_ for _ in ()).throw(AssertionError("historical reads must not hit mutable facts")),
    )

    assert markets.get_trading_calendar(
        "SSE", "2012-01-01", "2026-08-11", True, OLD_VERSION
    ) == expected


def test_unpublished_historical_calendar_still_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(market_data_version, "current_market_data_version", lambda: "mhf-v1-current")
    monkeypatch.setattr(markets, "read_published_trading_calendar", lambda *_args: None)

    try:
        markets.get_trading_calendar("SSE", "2012-01-01", "2026-08-11", True, OLD_VERSION)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 409
    else:
        raise AssertionError("unpublished stale versions must fail closed")
