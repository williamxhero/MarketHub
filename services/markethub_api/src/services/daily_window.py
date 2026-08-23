from __future__ import annotations

import base64
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Generator, Iterator
from itertools import chain
from threading import RLock

import pandas as pd
import pyarrow as pa
from fastapi import HTTPException

from quotemux.infra.db.client import query_dataframe, stream_query_batches
from routers.stock_quote_models import StockDailyWindowQueryPayload
from services.market_data_version import require_market_data_version
from services.runtime_memory import process_rss_mb


_LOGGER = logging.getLogger("uvicorn.error")


_BASE_CTE = """
with catalog as materialized (
    select distinct on (code)
        market,
        code,
        listed_date,
        delisted_date
    from ref.stock
    where code <> '000000'
    order by code, (delisted_date is null) desc, listed_date desc, market
), requested_codes as materialized (
    select requested.code
    from unnest(%s::text[]) as requested(code)
), universe as materialized (
    select catalog.market, catalog.code,
        case when catalog.market = 'BJSE' then greatest(catalog.listed_date, date '2021-11-15') else catalog.listed_date end as listed_date,
        catalog.delisted_date
    from catalog
    where (
        %s = 'codes'
        and exists (select 1 from requested_codes where requested_codes.code = catalog.code)
    ) or (
        %s = 'all_a'
        and (case when catalog.market = 'BJSE' then greatest(catalog.listed_date, date '2021-11-15') else catalog.listed_date end) <= %s::date
        and (catalog.delisted_date is null or catalog.delisted_date > %s::date)
        and (case when catalog.market = 'BJSE' then greatest(catalog.listed_date, date '2021-11-15') else catalog.listed_date end) < coalesce(catalog.delisted_date, date 'infinity')
        and (
            (catalog.market = 'SHSE' and left(catalog.code, 1) = '6')
            or (catalog.market = 'SZSE' and left(catalog.code, 1) in ('0', '3'))
            or (
                catalog.market = 'BJSE'
                and (left(catalog.code, 1) in ('4', '8', '9'))
            )
        )
    )
), open_dates as materialized (
    select trade_date
    from ref.trade_calendar
    where exchange = 'SHSE'
      and is_open = true
      and trade_date between %s::date and %s::date
)
"""


_UNIVERSE_CTE = _BASE_CTE.rstrip() + """
, expected as materialized (
    select universe.market, universe.code, open_dates.trade_date
    from universe
    cross join open_dates
    where (universe.listed_date is null or universe.listed_date <= open_dates.trade_date)
      and (universe.delisted_date is null or open_dates.trade_date < universe.delisted_date)
      and not exists (
          select 1
          from fact.stock_suspension_history suspensions
          where suspensions.market = universe.market
            and suspensions.code = universe.code
            and suspensions.status = 'suspended'
            and suspensions.suspend_start_date <= open_dates.trade_date
            and suspensions.suspend_end_date >= open_dates.trade_date
      )
)
"""


_COVERAGE_QUERY = _UNIVERSE_CTE + """
, expected_state as materialized (
    select
        expected.code,
        expected.trade_date,
        count(daily_rows.code)::int as row_count
    from expected
    left join fact.stock_daily_1d daily_rows
      on daily_rows.market = expected.market
     and daily_rows.code = expected.code
     and daily_rows.trade_date = expected.trade_date
     and coalesce(daily_rows.is_suspended, false) = false
     and daily_rows.open is not null
     and daily_rows.high is not null
     and daily_rows.low is not null
     and daily_rows.close is not null
     and daily_rows.volume is not null
    group by expected.code, expected.trade_date
), coverage as materialized (
    select
        universe.code,
        count(expected_state.trade_date)::int as expected_rows,
        count(expected_state.trade_date) filter (where expected_state.row_count = 1)::int as actual_rows,
        count(expected_state.trade_date) filter (where expected_state.row_count = 0)::int as missing_rows,
        coalesce(
            json_agg(expected_state.trade_date::text order by expected_state.trade_date)
                filter (where expected_state.row_count = 0),
            '[]'::json
        ) as missing_trade_dates,
        bool_and(coalesce(expected_state.row_count, 1) = 1) as complete
    from universe
    left join expected_state on expected_state.code = universe.code
    group by universe.code
), unknown_codes as materialized (
    select requested_codes.code
    from requested_codes
    left join catalog on catalog.code = requested_codes.code
    where catalog.code is null
)
select
    (select count(*)::int from universe) as universe_size,
    (select coalesce(sum(expected_rows), 0)::int from coverage) as expected_total,
    (select coalesce(sum(actual_rows), 0)::int from coverage) as actual_total,
    (select coalesce(sum(missing_rows), 0)::int from coverage) as missing_total,
    (select coalesce(sum(greatest(row_count - 1, 0)), 0)::int from expected_state) as duplicate_total,
    coalesce(
        (
            select json_agg(
                json_build_object(
                    'code', coverage.code,
                    'expected_rows', coverage.expected_rows,
                    'actual_rows', coverage.actual_rows,
                    'missing_rows', coverage.missing_rows,
                    'missing_trade_dates', coverage.missing_trade_dates,
                    'complete', coverage.complete
                ) order by coverage.code
            )
            from coverage
        ),
        '[]'::json
    )::text as coverage_json,
    coalesce((select json_agg(code order by code) from unknown_codes), '[]'::json)::text as unknown_codes_json
"""


_PAGE_QUERY = _BASE_CTE + """
, page_candidates as materialized (
    select
        daily_rows.code,
        daily_rows.trade_date,
        daily_rows.open,
        daily_rows.high,
        daily_rows.low,
        daily_rows.close,
        daily_rows.pre_close,
        daily_rows.change,
        daily_rows.pct_chg,
        daily_rows.volume,
        daily_rows.amount,
        coalesce(daily_rows.is_st, false) as is_st
    from fact.stock_daily_1d daily_rows
    join universe
      on universe.market = daily_rows.market
     and universe.code = daily_rows.code
    join open_dates
      on open_dates.trade_date = daily_rows.trade_date
    where coalesce(daily_rows.is_suspended, false) = false
      and (universe.listed_date is null or universe.listed_date <= daily_rows.trade_date)
      and (universe.delisted_date is null or daily_rows.trade_date < universe.delisted_date)
      and not exists (
          select 1
          from fact.stock_suspension_history suspensions
          where suspensions.market = universe.market
            and suspensions.code = universe.code
            and suspensions.status = 'suspended'
            and suspensions.suspend_start_date <= daily_rows.trade_date
            and suspensions.suspend_end_date >= daily_rows.trade_date
      )
      and daily_rows.open is not null
      and daily_rows.high is not null
      and daily_rows.low is not null
      and daily_rows.close is not null
      and daily_rows.volume is not null
      and (
          %s::date is null
          or (daily_rows.trade_date, daily_rows.code) > (%s::date, %s::text)
      )
    order by daily_rows.trade_date, daily_rows.code
    limit %s
), delivered as materialized (
    select *
    from page_candidates
    order by trade_date, code
    limit %s
)
select
    coalesce(
        json_agg(
            json_build_object(
                'code', delivered.code,
                'trade_time', delivered.trade_date::text,
                'freq', '1d',
                'open', delivered.open,
                'high', delivered.high,
                'low', delivered.low,
                'close', delivered.close,
                'pre_close', delivered.pre_close,
                'change', delivered.change,
                'pct_chg', delivered.pct_chg,
                'volume', delivered.volume,
                'amount', delivered.amount,
                'adjust', 'none',
                'is_suspended', false,
                'is_st', delivered.is_st
            ) order by delivered.trade_date, delivered.code
        ),
        '[]'::json
    )::text as items_json,
    count(delivered.code)::int as returned_rows,
    (select count(*) > %s from page_candidates) as has_more,
    (
        select trade_date::text
        from delivered
        order by trade_date desc, code desc
        limit 1
    ) as last_trade_time,
    (
        select code
        from delivered
        order by trade_date desc, code desc
        limit 1
    ) as last_code
from delivered
"""


_COVERAGE_ROWS_QUERY = _UNIVERSE_CTE + """
, expected_state as materialized (
    select expected.code,expected.trade_date,count(daily_rows.code)::int as row_count
    from expected
    left join fact.stock_daily_1d daily_rows
      on daily_rows.market=expected.market and daily_rows.code=expected.code
     and daily_rows.trade_date=expected.trade_date
     and coalesce(daily_rows.is_suspended,false)=false
     and daily_rows.open is not null and daily_rows.high is not null
     and daily_rows.low is not null and daily_rows.close is not null and daily_rows.volume is not null
    group by expected.code,expected.trade_date
)
select universe.code,
       count(expected_state.trade_date)::int as expected_rows,
       count(expected_state.trade_date) filter(where expected_state.row_count=1)::int as actual_rows,
       count(expected_state.trade_date) filter(where expected_state.row_count=0)::int as missing_rows,
       coalesce(array_agg(expected_state.trade_date order by expected_state.trade_date)
         filter(where expected_state.row_count=0),'{}'::date[]) as missing_trade_dates,
       bool_and(coalesce(expected_state.row_count,1)=1) as complete,
       coalesce(sum(greatest(expected_state.row_count-1,0)),0)::int as duplicate_rows
from universe left join expected_state on expected_state.code=universe.code
group by universe.code order by universe.code
"""


_UNKNOWN_CODES_QUERY = _BASE_CTE + """
select requested_codes.code
from requested_codes left join catalog on catalog.code=requested_codes.code
where catalog.code is null order by requested_codes.code
"""


_PAGE_ROWS_CTE = _BASE_CTE + """
, page_candidates as materialized (
    select daily_rows.code,daily_rows.trade_date,daily_rows.open,daily_rows.high,daily_rows.low,
           daily_rows.close,daily_rows.pre_close,daily_rows.change,daily_rows.pct_chg,
           daily_rows.volume,daily_rows.amount,coalesce(daily_rows.is_st,false) as is_st
    from fact.stock_daily_1d daily_rows
    join universe on universe.market=daily_rows.market and universe.code=daily_rows.code
    join open_dates on open_dates.trade_date=daily_rows.trade_date
    where coalesce(daily_rows.is_suspended,false)=false
      and (universe.listed_date is null or universe.listed_date<=daily_rows.trade_date)
      and (universe.delisted_date is null or daily_rows.trade_date<universe.delisted_date)
      and not exists (
          select 1 from fact.stock_suspension_history suspensions
          where suspensions.market=universe.market and suspensions.code=universe.code
            and suspensions.status='suspended'
            and suspensions.suspend_start_date<=daily_rows.trade_date
            and suspensions.suspend_end_date>=daily_rows.trade_date
      )
      and daily_rows.open is not null and daily_rows.high is not null
      and daily_rows.low is not null and daily_rows.close is not null and daily_rows.volume is not null
      and (%s::date is null or (daily_rows.trade_date,daily_rows.code)>(%s::date,%s::text))
    order by daily_rows.trade_date,daily_rows.code
    limit %s
), delivered as materialized (
    select * from page_candidates order by trade_date,code limit %s
)
"""


_PAGE_META_QUERY = _PAGE_ROWS_CTE + """
select count(delivered.code)::int as returned_rows,
       (select count(*)>%s from page_candidates) as has_more,
       (select trade_date from delivered order by trade_date desc,code desc limit 1) as last_trade_time,
       (select code from delivered order by trade_date desc,code desc limit 1) as last_code
from delivered
"""


_PAGE_ROWS_QUERY = _PAGE_ROWS_CTE + """
select code,trade_date,open,high,low,close,pre_close,change,pct_chg,volume,amount,is_st
from delivered order by trade_date,code
"""


ARROW_MEDIA_TYPE = "application/vnd.apache.arrow.stream"
ARROW_SCHEMA_VERSION = "markethub-daily-window-arrow-v1"
ARROW_RECORD_BATCH_ROWS = 8_192
COVERAGE_CACHE_MAX_ENTRIES = 32
_COVERAGE_CACHE: OrderedDict[str, tuple[dict[str, object], list[dict[str, object]]]] = OrderedDict()
_COVERAGE_CACHE_LOCK = RLock()
ARROW_SCHEMA = pa.schema(
    [
        ("code", pa.string()), ("trade_time", pa.string()), ("freq", pa.string()),
        ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()), ("close", pa.float64()),
        ("pre_close", pa.float64()), ("change", pa.float64()), ("pct_chg", pa.float64()),
        ("volume", pa.float64()), ("amount", pa.float64()), ("adjust", pa.string()),
        ("is_suspended", pa.bool_()), ("is_st", pa.bool_()),
    ]
)


@dataclass(frozen=True)
class EncodedDailyWindowResponse:
    content: bytes
    headers: dict[str, str]


@dataclass(frozen=True)
class PreparedDailyWindowArrowResponse:
    body: Iterator[bytes]
    headers: dict[str, str]


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _request_fingerprint(payload: StockDailyWindowQueryPayload) -> str:
    value = {
        "freq": payload.freq,
        "universe": payload.universe,
        "codes": sorted(payload.codes),
        "start_date": payload.start_date,
        "end_date": payload.end_date,
    }
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def clear_coverage_cache() -> None:
    with _COVERAGE_CACHE_LOCK:
        _COVERAGE_CACHE.clear()


def _coverage_cache_key(payload: StockDailyWindowQueryPayload) -> str:
    return f"{payload.data_version}:{_request_fingerprint(payload)}"


def _encode_cursor(payload: StockDailyWindowQueryPayload, trade_time: str, code: str) -> str:
    value = {
        "v": 1,
        "data_version": payload.data_version,
        "fingerprint": _request_fingerprint(payload),
        "trade_time": trade_time,
        "code": code,
    }
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(payload: StockDailyWindowQueryPayload) -> tuple[str | None, str]:
    if payload.cursor is None:
        return None, ""
    try:
        padding = "=" * (-len(payload.cursor) % 4)
        raw = base64.urlsafe_b64decode((payload.cursor + padding).encode("ascii"))
        value = json.loads(raw)
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail={"code": "DAILY_WINDOW_CURSOR_INVALID", "message": "cursor 无法解析"}) from exc
    if (
        value.get("v") != 1
        or value.get("data_version") != payload.data_version
        or value.get("fingerprint") != _request_fingerprint(payload)
    ):
        raise HTTPException(status_code=409, detail={"code": "DAILY_WINDOW_CURSOR_MISMATCH", "message": "cursor 不属于当前版本或查询窗口"})
    trade_time = str(value.get("trade_time", ""))
    code = str(value.get("code", ""))
    if len(trade_time) != 10 or len(code) != 6 or not code.isdigit():
        raise HTTPException(status_code=400, detail={"code": "DAILY_WINDOW_CURSOR_INVALID", "message": "cursor 页边界无效"})
    return trade_time, code


def _universe_params(payload: StockDailyWindowQueryPayload) -> tuple[object, ...]:
    return (
        payload.codes,
        payload.universe,
        payload.universe,
        payload.end_date,
        payload.start_date,
        payload.start_date,
        payload.end_date,
    )


def _single_row(frame: pd.DataFrame, stage: str) -> dict[str, object]:
    if len(frame.index) != 1:
        raise HTTPException(
            status_code=503,
            detail={"code": "DAILY_WINDOW_INTEGRITY_UNAVAILABLE", "message": f"{stage} 查询不可用"},
        )
    return frame.iloc[0].to_dict()


def _parse_json_text(value: object, stage: str) -> list[dict[str, object]] | list[str]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "DAILY_WINDOW_INTEGRITY_UNAVAILABLE", "message": f"{stage} JSON 无法解析"},
        ) from exc
    if not isinstance(parsed, list):
        raise HTTPException(
            status_code=503,
            detail={"code": "DAILY_WINDOW_INTEGRITY_UNAVAILABLE", "message": f"{stage} 结构无效"},
        )
    return parsed


def _raise_incomplete(coverage_row: dict[str, object], coverage: list[dict[str, object]], unknown_codes: list[str]) -> None:
    universe_size = int(coverage_row.get("universe_size", 0) or 0)
    missing_total = int(coverage_row.get("missing_total", 0) or 0)
    duplicate_total = int(coverage_row.get("duplicate_total", 0) or 0)
    expected_total = int(coverage_row.get("expected_total", 0) or 0)
    actual_total = int(coverage_row.get("actual_total", 0) or 0)
    incomplete_codes = [str(item.get("code", "")) for item in coverage if not bool(item.get("complete", False))]
    if universe_size == 0 or unknown_codes or missing_total or duplicate_total or expected_total != actual_total or incomplete_codes:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MARKET_DATA_INCOMPLETE",
                "message": "日线窗口完整性校验失败，拒绝返回部分数据",
                "details": {
                    "universe_size": universe_size,
                    "expected_rows": expected_total,
                    "actual_rows": actual_total,
                    "missing_rows": missing_total,
                    "duplicate_rows": duplicate_total,
                    "unknown_codes": unknown_codes[:20],
                    "incomplete_codes": incomplete_codes[:20],
                },
            },
        )


def _load_coverage_uncached(payload: StockDailyWindowQueryPayload) -> tuple[dict[str, object], list[dict[str, object]]]:
    coverage_row = _single_row(query_dataframe(_COVERAGE_QUERY, _universe_params(payload)), "coverage")
    coverage = _parse_json_text(coverage_row.get("coverage_json", "[]"), "coverage")
    unknown_codes = _parse_json_text(coverage_row.get("unknown_codes_json", "[]"), "unknown_codes")
    normalized_coverage = [dict(item) for item in coverage if isinstance(item, dict)]
    if len(normalized_coverage) != len(coverage):
        raise HTTPException(
            status_code=503,
            detail={"code": "DAILY_WINDOW_INTEGRITY_UNAVAILABLE", "message": "coverage 行结构无效"},
        )
    _raise_incomplete(coverage_row, normalized_coverage, [str(code) for code in unknown_codes])
    return coverage_row, normalized_coverage


def _cached_coverage(payload: StockDailyWindowQueryPayload) -> tuple[dict[str, object], list[dict[str, object]]]:
    key = _coverage_cache_key(payload)
    with _COVERAGE_CACHE_LOCK:
        cached = _COVERAGE_CACHE.get(key)
        if cached is not None:
            _COVERAGE_CACHE.move_to_end(key)
            return cached
        loaded = _load_coverage_uncached(payload)
        _COVERAGE_CACHE[key] = loaded
        _COVERAGE_CACHE.move_to_end(key)
        while len(_COVERAGE_CACHE) > COVERAGE_CACHE_MAX_ENTRIES:
            _COVERAGE_CACHE.popitem(last=False)
        return loaded


def build_response(payload: StockDailyWindowQueryPayload, accept_gzip: bool) -> EncodedDailyWindowResponse:
    request_started = time.perf_counter()
    start_rss_mb = process_rss_mb()
    cursor_trade_time, cursor_code = _decode_cursor(payload)

    version_pre_started = time.perf_counter()
    require_market_data_version(payload.data_version)
    version_pre_ms = _elapsed_ms(version_pre_started)

    coverage_started = time.perf_counter()
    coverage_row, coverage = _cached_coverage(payload)
    coverage_db_ms = _elapsed_ms(coverage_started)

    version_coverage_started = time.perf_counter()
    require_market_data_version(payload.data_version)
    version_coverage_ms = _elapsed_ms(version_coverage_started)

    page_started = time.perf_counter()
    page_params = _universe_params(payload) + (
        cursor_trade_time,
        cursor_trade_time,
        cursor_code,
        payload.page_size + 1,
        payload.page_size,
        payload.page_size,
    )
    page_row = _single_row(query_dataframe(_PAGE_QUERY, page_params), "page")
    page_db_ms = _elapsed_ms(page_started)

    version_post_started = time.perf_counter()
    require_market_data_version(payload.data_version)
    version_post_ms = _elapsed_ms(version_post_started)

    returned_rows = int(page_row.get("returned_rows", 0) or 0)
    has_more = bool(page_row.get("has_more", False))
    last_trade_time = str(page_row.get("last_trade_time", "") or "")
    last_code = str(page_row.get("last_code", "") or "")
    if returned_rows < 0 or returned_rows > payload.page_size or (has_more and returned_rows != payload.page_size):
        raise HTTPException(status_code=503, detail={"code": "DAILY_WINDOW_PAGE_INVALID", "message": "页边界或行数无效"})
    if returned_rows > 0 and (len(last_trade_time) != 10 or len(last_code) != 6):
        raise HTTPException(status_code=503, detail={"code": "DAILY_WINDOW_PAGE_INVALID", "message": "末行 continuation key 无效"})
    if has_more and returned_rows == 0:
        raise HTTPException(status_code=503, detail={"code": "DAILY_WINDOW_PAGE_INVALID", "message": "空页不能携带 continuation"})

    next_cursor = _encode_cursor(payload, last_trade_time, last_code) if has_more else None
    total_rows = int(coverage_row.get("actual_total", 0) or 0)
    meta = {
        "data_version": payload.data_version,
        "total_rows": total_rows,
        "returned_rows": returned_rows,
        "complete": True,
        "truncated": False,
        "page_complete": True,
        "request_complete": True,
        "delivery_complete": not has_more,
        "next_cursor": next_cursor,
        "universe_kind": payload.universe,
        "universe_size": int(coverage_row.get("universe_size", 0) or 0),
        "page_size": payload.page_size,
        "coverage": coverage,
    }

    serialization_started = time.perf_counter()
    items_json = str(page_row.get("items_json", "[]"))
    if not items_json.startswith("[") or not items_json.endswith("]"):
        raise HTTPException(status_code=503, detail={"code": "DAILY_WINDOW_PAGE_INVALID", "message": "items JSON 无效"})
    meta_json = json.dumps(meta, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    raw_content = b'{"items":' + items_json.encode("utf-8") + b',"meta":' + meta_json + b"}"
    serialization_ms = _elapsed_ms(serialization_started)

    compression_started = time.perf_counter()
    if accept_gzip:
        content = gzip.compress(raw_content, compresslevel=1, mtime=0)
        content_encoding = "gzip"
    else:
        content = raw_content
        content_encoding = "identity"
    compression_ms = _elapsed_ms(compression_started)

    server_timing = (
        f"version_pre;dur={version_pre_ms:.3f}, "
        f"coverage_db;dur={coverage_db_ms:.3f}, "
        f"version_coverage;dur={version_coverage_ms:.3f}, "
        f"page_db;dur={page_db_ms:.3f}, "
        f"version_post;dur={version_post_ms:.3f}, "
        f"serialize;dur={serialization_ms:.3f}, "
        f"compress;dur={compression_ms:.3f}"
    )
    finish_rss_mb = process_rss_mb()
    elapsed_ms = _elapsed_ms(request_started)
    _LOGGER.info(
        "daily_window_v2 complete universe=%s universe_size=%s returned_rows=%s total_rows=%s "
        "wire_bytes=%s decoded_bytes=%s rss_mb=%.1f rss_delta_mb=%.1f elapsed_ms=%.3f server_timing=%s",
        payload.universe,
        meta["universe_size"],
        returned_rows,
        total_rows,
        len(content),
        len(raw_content),
        finish_rss_mb,
        finish_rss_mb - start_rss_mb,
        elapsed_ms,
        server_timing,
    )
    headers = {
        "Content-Encoding": content_encoding,
        "Content-Length": str(len(content)),
        "Vary": "Accept-Encoding",
        "Server-Timing": server_timing,
        "X-MarketHub-Decoded-Bytes": str(len(raw_content)),
        "X-MarketHub-Data-Version": payload.data_version,
    }
    return EncodedDailyWindowResponse(content=content, headers=headers)


def _stream_rows(query: str, params: tuple[object, ...], *, batch_size: int = 1_000) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    stream = stream_query_batches(query, params, batch_size=batch_size)
    try:
        for batch in stream:
            rows.extend(dict(row) for row in batch)
    finally:
        stream.close()
    return rows


def _stream_single_row(query: str, params: tuple[object, ...], stage: str) -> dict[str, object]:
    rows = _stream_rows(query, params, batch_size=2)
    if len(rows) != 1:
        raise HTTPException(status_code=503, detail={"code": "DAILY_WINDOW_INTEGRITY_UNAVAILABLE", "message": f"{stage} 查询不可用"})
    return rows[0]


def _page_params(payload: StockDailyWindowQueryPayload, cursor_trade_time: str | None, cursor_code: str) -> tuple[object, ...]:
    return _universe_params(payload) + (
        cursor_trade_time,
        cursor_trade_time,
        cursor_code,
        payload.page_size + 1,
        payload.page_size,
        payload.page_size,
    )


def _arrow_item(row: dict[str, object]) -> dict[str, object]:
    trade_date = row.get("trade_date")
    return {
        "code": str(row["code"]),
        "trade_time": trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date),
        "freq": "1d",
        "open": row.get("open"), "high": row.get("high"), "low": row.get("low"), "close": row.get("close"),
        "pre_close": row.get("pre_close"), "change": row.get("change"), "pct_chg": row.get("pct_chg"),
        "volume": row.get("volume"), "amount": row.get("amount"), "adjust": "none",
        "is_suspended": False, "is_st": bool(row.get("is_st", False)),
    }


class _ArrowChunkSink(io.RawIOBase):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, value: bytes | bytearray | memoryview) -> int:
        data = bytes(value)
        self._chunks.append(data)
        return len(data)

    def drain(self) -> list[bytes]:
        chunks, self._chunks = self._chunks, []
        return chunks


def _arrow_body(
    schema: pa.Schema,
    first_batch: list[dict[str, object]],
    database_batches: Generator[list[dict[str, object]], None, None],
    expected_rows: int,
    request_started: float,
    start_rss_mb: float,
) -> Iterator[bytes]:
    sink = _ArrowChunkSink()
    output = pa.PythonFile(sink, mode="w")
    writer = pa.ipc.new_stream(output, schema)
    emitted_rows = 0
    first_wire_at: float | None = None
    try:
        for chunk in sink.drain():
            first_wire_at = first_wire_at or time.perf_counter()
            yield chunk
        for rows in chain((first_batch,), database_batches):
            if not rows:
                continue
            table = pa.Table.from_pylist([_arrow_item(dict(row)) for row in rows], schema=schema)
            for batch in table.to_batches(max_chunksize=ARROW_RECORD_BATCH_ROWS):
                writer.write_batch(batch)
                emitted_rows += batch.num_rows
                for chunk in sink.drain():
                    first_wire_at = first_wire_at or time.perf_counter()
                    yield chunk
        if emitted_rows != expected_rows:
            raise RuntimeError(f"Arrow row count changed during streaming: expected={expected_rows} actual={emitted_rows}")
        writer.close()
        for chunk in sink.drain():
            first_wire_at = first_wire_at or time.perf_counter()
            yield chunk
    finally:
        database_batches.close()
        if not output.closed:
            try:
                writer.close()
            except Exception:
                pass
        elapsed_ms = _elapsed_ms(request_started)
        first_batch_ms = round(((first_wire_at or time.perf_counter()) - request_started) * 1000, 3)
        finish_rss_mb = process_rss_mb()
        _LOGGER.info(
            "daily_window_arrow complete=%s returned_rows=%s first_batch_ms=%.3f elapsed_ms=%.3f rss_mb=%.1f rss_delta_mb=%.1f",
            emitted_rows == expected_rows,
            emitted_rows,
            first_batch_ms,
            elapsed_ms,
            finish_rss_mb,
            finish_rss_mb - start_rss_mb,
        )


def prepare_arrow_response(payload: StockDailyWindowQueryPayload) -> PreparedDailyWindowArrowResponse:
    request_started = time.perf_counter()
    start_rss_mb = process_rss_mb()
    cursor_trade_time, cursor_code = _decode_cursor(payload)
    require_market_data_version(payload.data_version)

    coverage_started = time.perf_counter()
    coverage_row, coverage = _cached_coverage(payload)
    coverage_db_ms = _elapsed_ms(coverage_started)
    require_market_data_version(payload.data_version)

    page_params = _page_params(payload, cursor_trade_time, cursor_code)
    page_meta_started = time.perf_counter()
    page_row = _stream_single_row(_PAGE_META_QUERY, page_params, "page_meta")
    page_meta_db_ms = _elapsed_ms(page_meta_started)
    returned_rows = int(page_row.get("returned_rows", 0) or 0)
    has_more = bool(page_row.get("has_more", False))
    last_value = page_row.get("last_trade_time")
    last_trade_time = last_value.isoformat() if hasattr(last_value, "isoformat") else str(last_value or "")
    last_code = str(page_row.get("last_code", "") or "")
    if returned_rows < 0 or returned_rows > payload.page_size or (has_more and returned_rows != payload.page_size):
        raise HTTPException(status_code=503, detail={"code": "DAILY_WINDOW_PAGE_INVALID", "message": "页边界或行数无效"})
    if returned_rows > 0 and (len(last_trade_time) != 10 or len(last_code) != 6):
        raise HTTPException(status_code=503, detail={"code": "DAILY_WINDOW_PAGE_INVALID", "message": "末行 continuation key 无效"})
    next_cursor = _encode_cursor(payload, last_trade_time, last_code) if has_more else None
    meta = {
        "data_version": payload.data_version,
        "total_rows": int(coverage_row["actual_total"]),
        "returned_rows": returned_rows,
        "complete": True,
        "truncated": False,
        "page_complete": True,
        "request_complete": True,
        "delivery_complete": not has_more,
        "next_cursor": next_cursor,
        "universe_kind": payload.universe,
        "universe_size": int(coverage_row["universe_size"]),
        "page_size": payload.page_size,
        "coverage": coverage,
    }
    metadata = {
        b"markethub.meta": json.dumps(meta, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(),
        b"markethub.schema_version": ARROW_SCHEMA_VERSION.encode(),
        b"markethub.data_version": payload.data_version.encode(),
    }
    schema = ARROW_SCHEMA.with_metadata(metadata)
    database_batches = stream_query_batches(_PAGE_ROWS_QUERY, page_params[:-1], batch_size=ARROW_RECORD_BATCH_ROWS)
    try:
        first_batch = next(database_batches, [])
        require_market_data_version(payload.data_version)
    except BaseException:
        database_batches.close()
        raise
    if returned_rows > 0 and not first_batch:
        database_batches.close()
        raise HTTPException(status_code=503, detail={"code": "DAILY_WINDOW_PAGE_INVALID", "message": "Arrow 页在流式读取前发生变化"})
    headers = {
        "Vary": "Accept, Accept-Encoding",
        "Server-Timing": f"coverage_db;dur={coverage_db_ms:.3f}, page_meta_db;dur={page_meta_db_ms:.3f}",
        "X-MarketHub-Data-Version": payload.data_version,
        "X-MarketHub-Returned-Rows": str(returned_rows),
        "X-MarketHub-Delivery-Complete": str(not has_more).lower(),
        "X-MarketHub-Next-Cursor": next_cursor or "",
        "X-MarketHub-Arrow-Schema-Version": ARROW_SCHEMA_VERSION,
    }
    body = _arrow_body(schema, first_batch, database_batches, returned_rows, request_started, start_rss_mb)
    return PreparedDailyWindowArrowResponse(body=body, headers=headers)
