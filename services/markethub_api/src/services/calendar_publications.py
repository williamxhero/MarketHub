from __future__ import annotations

from datetime import date

from fastapi import HTTPException
from platform_models import TradingCalendarItem
from quotemux.infra.db.client import query_dataframe


_PUBLICATION_QUERY = """
select snapshot_sha256, range_start, range_end, row_count, open_day_count
from audit.trade_calendar_publication
where market_data_version = %s
  and exchange = %s
  and range_start <= %s::date
  and range_end >= %s::date
order by (range_end - range_start), published_at_utc desc
limit 1
"""

_ROWS_QUERY = """
select trade_date, is_open
from readmodel.trade_calendar_snapshot_row
where snapshot_sha256 = %s
  and exchange = %s
  and trade_date between %s::date and %s::date
  and (%s::boolean is null or is_open = %s::boolean)
order by trade_date
"""


def read_published_trading_calendar(
    market_data_version: str,
    exchange: str,
    start_date: str,
    end_date: str,
    is_open: bool | None,
) -> list[TradingCalendarItem] | None:
    if not market_data_version.startswith("mhf-v1-"):
        return None
    try:
        requested_start = date.fromisoformat(start_date)
        requested_end = date.fromisoformat(end_date)
    except ValueError:
        return None
    if requested_start > requested_end:
        return None

    publication = query_dataframe(
        _PUBLICATION_QUERY,
        (market_data_version, exchange, start_date, end_date),
    )
    if publication.empty:
        return None
    record = publication.iloc[0]
    rows = query_dataframe(
        _ROWS_QUERY,
        (
            str(record["snapshot_sha256"]),
            exchange,
            start_date,
            end_date,
            is_open,
            is_open,
        ),
    )
    items = [
        TradingCalendarItem(
            exchange=exchange,
            trade_date=str(row["trade_date"]),
            is_open=bool(row["is_open"]),
        )
        for _, row in rows.iterrows()
    ]
    publication_start = str(record["range_start"])
    publication_end = str(record["range_end"])
    if start_date == publication_start and end_date == publication_end:
        expected_rows = (
            int(record["open_day_count"])
            if is_open is True
            else int(record["row_count"])
            if is_open is None
            else int(record["row_count"]) - int(record["open_day_count"])
        )
        if len(items) != expected_rows:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "CALENDAR_PUBLICATION_CORRUPT",
                    "message": "冻结交易日历 publication 行数校验失败",
                    "details": market_data_version,
                },
            )
    return items
