from __future__ import annotations

import hashlib
import json
import os

from fastapi import HTTPException

from quotemux.infra.db.client import query_dataframe


_VERSION_QUERY = """
with sources as (
    select 'fact.stock_daily_1d' as source, count(*)::text as row_count,
           coalesce(max(trade_date)::text, '') as watermark,
           coalesce(max(xmin::text::bigint)::text, '0') as mutation
    from fact.stock_daily_1d
    union all
    select 'fact.stock_financial_pit_factor', count(*)::text,
           coalesce(max(announcement_date)::text, ''), coalesce(max(xmin::text::bigint)::text, '0')
    from fact.stock_financial_pit_factor
    union all
    select 'fact.stock_listing_board_history', count(*)::text,
           coalesce(max(valid_from)::text, ''), coalesce(max(xmin::text::bigint)::text, '0')
    from fact.stock_listing_board_history
    union all
    select 'fact.stock_market_indicators_daily', count(*)::text,
           coalesce(max(trade_date)::text, ''), coalesce(max(xmin::text::bigint)::text, '0')
    from fact.stock_market_indicators_daily
    union all
    select 'fact.stock_money_flow_daily', count(*)::text,
           coalesce(max(trade_date)::text, ''), coalesce(max(xmin::text::bigint)::text, '0')
    from fact.stock_money_flow_daily
    union all
    select 'fact.stock_price_band_daily', count(*)::text,
           coalesce(max(trade_date)::text, ''), coalesce(max(xmin::text::bigint)::text, '0')
    from fact.stock_price_band_daily
    union all
    select 'fact.concept_daily_1d', count(*)::text,
           coalesce(max(trade_date)::text, ''), coalesce(max(xmin::text::bigint)::text, '0')
    from fact.concept_daily_1d
    union all
    select 'audit.stock_bar_1m_write_event', count(*)::text,
           coalesce(max(max_bar_time)::text, ''), coalesce(max(event_id)::text, '0')
    from audit.stock_bar_1m_write_event
    union all
    select 'ref.concept_stock_membership', count(*)::text,
           coalesce(max(valid_from)::text, ''), coalesce(max(xmin::text::bigint)::text, '0')
    from ref.concept_stock_membership
    union all
    select 'ref.concept', count(*)::text,
           coalesce(max(updated_at)::text, ''), coalesce(max(xmin::text::bigint)::text, '0')
    from ref.concept
    union all
    select 'schema.ref.concept_stock_membership.pit', count(*)::text,
           coalesce(string_agg(column_name || ':' || data_type, ',' order by ordinal_position), ''), '0'
    from information_schema.columns
    where table_schema = 'ref'
      and table_name = 'concept_stock_membership'
      and column_name in ('knowledge_time', 'knowledge_time_status')
    union all
    select 'ref.stock', count(*)::text, coalesce(max(listed_date)::text, ''),
           coalesce(max(xmin::text::bigint)::text, '0')
    from ref.stock
    union all
    select 'ref.trade_calendar', count(*)::text, coalesce(max(trade_date)::text, ''),
           coalesce(max(xmin::text::bigint)::text, '0')
    from ref.trade_calendar
    union all
    select 'public.capability_cache_rows.market_inputs', count(*)::text,
           coalesce(max(updated_at)::text, ''), coalesce(max(xmin::text::bigint)::text, '0')
    from public.capability_cache_rows
    where capability_id = any(%s)
)
select source, row_count, watermark, mutation
from sources
order by source
"""

_MARKET_CACHE_CAPABILITIES = (
    "markets.calendar.trading",
    "stocks.indicators.premarket",
    "stocks.corporate_actions.dividends",
    "stocks.corporate_actions.repurchases",
    "stocks.corporate_actions.rights_issues",
    "stocks.corporate_actions.share_changes",
    "stocks.corporate_actions.unlock_schedules",
)

_VERSION_STATE_QUERY = """
select baseline_id, generation
from audit.market_data_version_state
where singleton = true
"""


def _version_from_state() -> str:
    frame = query_dataframe(_VERSION_STATE_QUERY)
    if len(frame.index) != 1:
        return ""
    baseline_id = str(frame.iloc[0].get("baseline_id", "") or "")
    generation = int(frame.iloc[0].get("generation", 0) or 0)
    if baseline_id == "" or generation < 1:
        return ""
    fingerprint = {
        "contract": "markethub-market-facts-v1-triggered",
        "baseline_id": baseline_id,
        "generation": generation,
        "adjustment_base_date": os.getenv("QUOTEMUX_ADJUSTMENT_BASE_DATE", "").strip(),
    }
    encoded = json.dumps(fingerprint, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"mhf-v1-{hashlib.sha256(encoded).hexdigest()}"


def _compute_market_data_version() -> str:
    frame = query_dataframe(_VERSION_QUERY, (list(_MARKET_CACHE_CAPABILITIES),))
    if len(frame.index) != 14:
        return ""
    sources = [
        {
            "source": str(row["source"]),
            "row_count": str(row["row_count"]),
            "watermark": str(row["watermark"]),
            "mutation": str(row["mutation"]),
        }
        for _, row in frame.iterrows()
    ]
    fingerprint = {
        "contract": "markethub-market-facts-v1",
        "adjustment_base_date": os.getenv("QUOTEMUX_ADJUSTMENT_BASE_DATE", "").strip(),
        "sources": sources,
    }
    encoded = json.dumps(fingerprint, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"mhf-v1-{hashlib.sha256(encoded).hexdigest()}"


def current_market_data_version() -> str:
    """返回当前市场事实的可复算版本；任一受管事实写入会改变该值。"""
    state_version = _version_from_state()
    if state_version != "":
        return state_version
    # Backward-compatible bootstrap fallback. Production deployment installs
    # the trigger-backed state before switching the release symlink.
    return _compute_market_data_version()


def current_market_data_lineage() -> list[dict[str, object]]:
    """Return the exact source rows used by the market-data fingerprint."""
    frame = query_dataframe(_VERSION_QUERY, (list(_MARKET_CACHE_CAPABILITIES),))
    if len(frame.index) != 14:
        return []
    return [
        {
            "source": str(row["source"]),
            "row_count": int(row["row_count"]),
            "watermark": str(row["watermark"]),
            "mutation": str(row["mutation"]),
        }
        for _, row in frame.iterrows()
    ]


def require_market_data_version(requested_version: str) -> str:
    actual_version = current_market_data_version()
    if actual_version == "":
        raise HTTPException(status_code=503, detail={"code": "MARKET_DATA_VERSION_UNAVAILABLE", "message": "无法生成市场数据版本，拒绝读取未冻结市场事实"})
    if requested_version == "":
        raise HTTPException(status_code=409, detail={"code": "MARKET_DATA_VERSION_REQUIRED", "message": "市场查询必须携带 /api/health 返回的 data_version"})
    if requested_version != actual_version:
        raise HTTPException(status_code=409, detail={"code": "MARKET_DATA_VERSION_MISMATCH", "message": "请求版本已失效，请重新读取 /api/health", "details": actual_version})
    return actual_version
