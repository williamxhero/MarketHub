from __future__ import annotations

import math
from datetime import date
from typing import Any, Literal

import pandas as pd
from fastapi import HTTPException
from quotemux.infra.db.client import query_dataframe

from routers.concept_research_models import (
    ConceptCatalogEnvelope,
    ConceptCatalogResearchItem,
    ConceptDailyBarsEnvelope,
    ConceptDailyBarResearchItem,
    ConceptDailyStatsEnvelope,
    ConceptDailyStatsResearchItem,
    ConceptLineageItem,
    ConceptMemberHistoryEnvelope,
    ConceptMemberHistoryResearchItem,
    ConceptMembershipEnvelope,
    ConceptMembershipResearchItem,
    ConceptMoneyFlowEnvelope,
    ConceptMoneyFlowResearchItem,
    ConceptResearchMeta,
)
from services.market_data_version import current_market_data_lineage, require_market_data_version


_MEMBERSHIP_PIT_COLUMNS_QUERY = """
select column_name
from information_schema.columns
where table_schema = 'ref'
  and table_name = 'concept_stock_membership'
  and column_name in ('knowledge_time', 'knowledge_time_status')
order by column_name
"""

PITMode = Literal["effective-date", "approx-historical", "strict"]
EFFECTIVE_DATE_ISSUE = "CONCEPT_MEMBERSHIP_EFFECTIVE_DATE"
APPROX_HISTORICAL_ISSUE = "CONCEPT_MEMBERSHIP_APPROX_HISTORICAL_EFFECTIVE_DATE"
EFFECTIVE_DATE_SEMANTICS = (
    "membership eligibility uses the stored valid_from/valid_to interval on the trade date; "
    "knowledge_time is neither read nor inferred"
)


def _uses_effective_date(pit_mode: PITMode) -> bool:
    """Keep the former spelling readable while publishing one canonical contract."""
    return pit_mode in {"effective-date", "approx-historical"}


def _effective_date_issue(pit_mode: PITMode) -> str:
    return EFFECTIVE_DATE_ISSUE if pit_mode == "effective-date" else APPROX_HISTORICAL_ISSUE


def _plain(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [{str(key): _plain(value) for key, value in row.items()} for row in frame.to_dict("records")]


def _validate_date_range(trade_date: str = "", start_date: str = "", end_date: str = "") -> None:
    try:
        for value in (trade_date, start_date, end_date):
            if value:
                date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "CONCEPT_RESEARCH_DATE_INVALID", "message": "dates must use YYYY-MM-DD"},
        ) from exc
    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=400,
            detail={"code": "CONCEPT_RESEARCH_DATE_RANGE_INVALID", "message": "start_date must not be after end_date"},
        )


def _lineage(source_names: set[str], effective_date: bool = True) -> list[ConceptLineageItem]:
    return [
        ConceptLineageItem(
            **item,
            approximate=effective_date,
            membership_basis="effective-date" if effective_date else "knowledge-time",
            pit_quality="effective_date_approximation" if effective_date else "strict_knowledge_time",
        )
        for item in current_market_data_lineage()
        if str(item["source"]) in source_names
    ]


def _meta(
    *,
    version: str,
    rows: int,
    total_rows: int | None = None,
    issues: list[str] | None = None,
    unavailable: bool = False,
    sources: set[str],
    source_semantics: dict[str, str] | None = None,
    pit_mode: PITMode = "strict",
) -> ConceptResearchMeta:
    actual_issues = issues or []
    effective_date = _uses_effective_date(pit_mode)
    non_blocking_issue = _effective_date_issue(pit_mode) if effective_date else ""
    blocking_issues = [issue for issue in actual_issues if issue != non_blocking_issue]
    complete = not blocking_issues and not unavailable
    capability = "available" if complete else ("unavailable" if unavailable else "incomplete")
    lineage = _lineage(sources, effective_date)
    require_market_data_version(version)
    semantics = dict(source_semantics or {})
    if effective_date:
        semantics["membership_basis"] = EFFECTIVE_DATE_SEMANTICS
        if pit_mode == "approx-historical":
            semantics["membership_pit"] = "APPROXIMATE: " + EFFECTIVE_DATE_SEMANTICS
    return ConceptResearchMeta(
        data_version=version,
        pit_mode=pit_mode,
        approximate=pit_mode == "approx-historical",
        research_profile=(
            "damxj-effective-date-v1" if pit_mode == "effective-date"
            else "damxj-approx-historical-v1" if pit_mode == "approx-historical"
            else "research-v1"
        ),
        complete=complete,
        capability=capability,
        total_rows=rows if total_rows is None else total_rows,
        returned_rows=rows,
        issues=actual_issues,
        lineage=lineage,
        source_semantics=semantics,
    )


def _validate_after_read(data_version: str) -> None:
    require_market_data_version(data_version)


def _snapshot_catalog_rows(concept_ids: list[str], trade_date: str) -> int | None:
    """Return the required research catalog size for an unfiltered daily snapshot.

    A snapshot endpoint has no concept selector.  Returning only the fact rows
    that happen to exist would otherwise make missing concepts invisible to a
    consumer which first read the same catalog.  Keep filtered queries and
    range queries on their existing, explicit-key semantics.
    """
    if concept_ids or not trade_date:
        return None
    frame = query_dataframe("select count(*) as catalog_rows from ref.concept where concept_id <> '000000'")
    return int(frame.iloc[0]["catalog_rows"]) if not frame.empty else 0


def _snapshot_effective_membership_rows(concept_ids: list[str], trade_date: str) -> int | None:
    if concept_ids or not trade_date:
        return None
    frame = query_dataframe(
        """
        select count(distinct concept_id) as expected_rows
        from ref.concept_stock_membership
        where valid_from <= %s::date
          and (valid_to is null or valid_to >= %s::date)
        """,
        (trade_date, trade_date),
    )
    return int(frame.iloc[0]["expected_rows"]) if not frame.empty else 0


def _effective_membership_exists(daily_alias: str) -> str:
    return (
        "exists (select 1 from ref.concept_stock_membership effective_membership "
        f"where effective_membership.concept_id = {daily_alias}.concept_id "
        f"and effective_membership.valid_from <= {daily_alias}.trade_date "
        f"and (effective_membership.valid_to is null or effective_membership.valid_to >= {daily_alias}.trade_date))"
    )


def _pit_columns_available() -> bool:
    frame = query_dataframe(_MEMBERSHIP_PIT_COLUMNS_QUERY)
    return set(frame.get("column_name", pd.Series(dtype=str)).astype(str)) == {"knowledge_time", "knowledge_time_status"}


def _pit_quality_issues(columns_available: bool, unknown_count: int, not_visible_count: int, pit_mode: PITMode) -> list[str]:
    if _uses_effective_date(pit_mode):
        return [_effective_date_issue(pit_mode)]
    if not columns_available:
        return ["CONCEPT_MEMBERSHIP_KNOWLEDGE_TIME_SCHEMA_UNAVAILABLE"]
    issues = []
    if unknown_count:
        issues.append(f"CONCEPT_MEMBERSHIP_KNOWLEDGE_TIME_UNKNOWN:{unknown_count}")
    if not_visible_count:
        issues.append(f"CONCEPT_MEMBERSHIP_NOT_YET_VISIBLE:{not_visible_count}")
    return issues


def get_catalog_research(category: str, market: str, status: str, limit: int, offset: int, data_version: str) -> ConceptCatalogEnvelope:
    version = require_market_data_version(data_version)
    clauses = ["concept_id <> '000000'"]
    params: list[object] = []
    for column, value in (("concept_type", category), ("market", market), ("status", status)):
        if value:
            clauses.append(f"{column} = %s")
            params.append(value)
    count_frame = query_dataframe(f"select count(*) as total_rows from ref.concept where {' and '.join(clauses)}", tuple(params))
    frame = query_dataframe(
        f"""
        select concept_id, coalesce(concept_type, '') as concept_type, coalesce(name, '') as name,
               coalesce(market, '') as market, coalesce(status, '') as status
        from ref.concept
        where {' and '.join(clauses)}
        order by concept_id
        limit %s offset %s
        """,
        tuple(params + [limit, offset]),
    )
    rows = _records(frame)
    _validate_after_read(version)
    total = int(count_frame.iloc[0]["total_rows"]) if not count_frame.empty else 0
    issues = [] if total > 0 else ["CONCEPT_CATALOG_EMPTY"]
    return ConceptCatalogEnvelope(
        items=[ConceptCatalogResearchItem(**row) for row in rows],
        meta=_meta(version=version, rows=len(rows), total_rows=total, issues=issues, sources={"ref.concept"}),
    )


def get_daily_bars_research(
    concept_ids: list[str],
    trade_date: str,
    start_date: str,
    end_date: str,
    limit: int,
    offset: int,
    include_stats: bool,
    data_version: str,
    pit_mode: PITMode = "strict",
) -> ConceptDailyBarsEnvelope:
    _validate_date_range(trade_date, start_date, end_date)
    version = require_market_data_version(data_version)
    clauses: list[str] = []
    params: list[object] = []
    if concept_ids:
        clauses.append("daily.concept_id = any(%s)")
        params.append(concept_ids)
    if trade_date:
        clauses.append("daily.trade_date = %s::date")
        params.append(trade_date)
    if start_date:
        clauses.append("daily.trade_date >= %s::date")
        params.append(start_date)
    if end_date:
        clauses.append("daily.trade_date <= %s::date")
        params.append(end_date)
    if _uses_effective_date(pit_mode):
        clauses.append(_effective_membership_exists("daily"))
    where_sql = f"where {' and '.join(clauses)}" if clauses else ""
    count_frame = query_dataframe(
        f"""
        select count(*) as total_rows,
               count(*) filter (where open is null or high is null or low is null or close is null) as missing_ohlc
        from fact.concept_daily_1d daily
        {where_sql}
        """,
        tuple(params),
    )
    frame = query_dataframe(
        f"""
        select daily.concept_id, coalesce(concept.name, '') as concept_name,
               daily.trade_date::text as trade_date, daily.open, daily.high, daily.low, daily.close,
               daily.volume, daily.amount
        from fact.concept_daily_1d daily
        left join ref.concept concept on concept.concept_id = daily.concept_id
        {where_sql}
        order by daily.concept_id, daily.trade_date
        limit %s offset %s
        """,
        tuple(params + [limit, offset]),
    )
    rows = _records(frame)
    _validate_after_read(version)
    total = int(count_frame.iloc[0]["total_rows"]) if not count_frame.empty else 0
    missing_ohlc = int(count_frame.iloc[0]["missing_ohlc"]) if not count_frame.empty else 0
    catalog_rows = _snapshot_catalog_rows(concept_ids, trade_date) if pit_mode == "strict" else None
    effective_rows = _snapshot_effective_membership_rows(concept_ids, trade_date) if _uses_effective_date(pit_mode) else None
    issues: list[str] = []
    if total == 0:
        issues.append("CONCEPT_DAILY_BARS_EMPTY")
    if missing_ohlc:
        issues.append(f"CONCEPT_DAILY_OHLC_UNAVAILABLE:{missing_ohlc}")
    if catalog_rows is not None and total != catalog_rows:
        issues.append(f"CONCEPT_DAILY_CATALOG_COVERAGE_INCOMPLETE:{total}/{catalog_rows}")
    if effective_rows is not None and total != effective_rows:
        issues.append(f"CONCEPT_DAILY_EFFECTIVE_MEMBERSHIP_COVERAGE_INCOMPLETE:{total}/{effective_rows}")
    if len(rows) != total:
        issues.append(f"CONCEPT_DAILY_BARS_TRUNCATED:{len(rows)}/{total}")
    result = ConceptDailyBarsEnvelope(
        items=[ConceptDailyBarResearchItem(**row) for row in rows],
        meta=_meta(
            version=version,
            rows=len(rows),
            total_rows=total,
            issues=issues,
            unavailable=missing_ohlc > 0 or (catalog_rows is not None and total != catalog_rows) or (effective_rows is not None and total != effective_rows),
            sources={"fact.concept_daily_1d", "ref.concept"} | ({"ref.concept_stock_membership"} if _uses_effective_date(pit_mode) else set()),
            source_semantics={
                "ohlc": "stored fact values only; pct_chg is never used to synthesize OHLC",
                **({"concept_universe": "valid_from <= trade_date and (valid_to is null or trade_date <= valid_to)"} if _uses_effective_date(pit_mode) else {}),
            },
        ),
    )
    if include_stats:
        stats = get_daily_stats_research(concept_ids, trade_date, start_date, end_date, limit, offset, data_version, pit_mode)
        result.daily_stats = stats.items
        result.daily_stats_meta = stats.meta
    if _uses_effective_date(pit_mode):
        result.meta = _meta(
            version=version,
            rows=len(rows),
            total_rows=total,
            issues=issues + [_effective_date_issue(pit_mode)],
            unavailable=missing_ohlc > 0 or (effective_rows is not None and total != effective_rows),
            sources={"fact.concept_daily_1d", "ref.concept", "ref.concept_stock_membership"},
            source_semantics={
                "ohlc": "stored fact values only; pct_chg is never used to synthesize OHLC",
                "concept_universe": "valid_from <= trade_date and (valid_to is null or trade_date <= valid_to)",
            },
            pit_mode=pit_mode,
        )
    return result


def _membership_rows(
    *, concept_id: str, trade_date: str, start_date: str, end_date: str, limit: int, offset: int, pit_mode: PITMode
) -> tuple[list[dict[str, Any]], int, bool, dict[str, int]]:
    pit_columns = _pit_columns_available()
    knowledge_time = "membership.knowledge_time::text" if pit_columns else "null::text"
    knowledge_status = "coalesce(membership.knowledge_time_status, 'unknown')" if pit_columns else "'unavailable'"
    clauses = ["membership.concept_id = %s"]
    params: list[object] = [concept_id]
    if trade_date:
        clauses.extend(["membership.valid_from <= %s::date", "(membership.valid_to is null or membership.valid_to >= %s::date)"])
        params.extend([trade_date, trade_date])
    if start_date:
        clauses.append("(membership.valid_to is null or membership.valid_to >= %s::date)")
        params.append(start_date)
    if end_date:
        clauses.append("membership.valid_from <= %s::date")
        params.append(end_date)
    where_sql = " and ".join(clauses)
    if _uses_effective_date(pit_mode):
        unknown_expression = "false"
        not_visible_expression = "false"
    elif pit_columns:
        unknown_expression = "knowledge_time is null or knowledge_time_status <> 'known'"
        not_visible_expression = (
            "knowledge_time_status = 'known' and knowledge_time is not null "
            "and knowledge_time >= ((%s::date + interval '1 day') at time zone 'Asia/Shanghai')"
            if trade_date
            else "false"
        )
    else:
        unknown_expression = "true"
        not_visible_expression = "false"
    count_params = ([trade_date] if pit_columns and trade_date and pit_mode == "strict" else []) + list(params)
    count_frame = query_dataframe(
        f"""
        select count(*) as total_rows,
               count(*) filter (where {unknown_expression}) as unknown_count,
               count(*) filter (where {not_visible_expression}) as not_visible_count
        from ref.concept_stock_membership membership
        where {where_sql}
        """,
        tuple(count_params),
    )
    frame = query_dataframe(
        f"""
        select membership.concept_id, membership.stock_code as code, coalesce(stock.name, '') as name,
               membership.valid_from::text as valid_from, membership.valid_to::text as valid_to,
               {knowledge_time} as knowledge_time, {knowledge_status} as knowledge_time_status
        from ref.concept_stock_membership membership
        left join ref.stock stock on stock.market = membership.stock_market and stock.code = membership.stock_code
        where {where_sql}
        order by membership.concept_id, membership.stock_code, membership.valid_from
        limit %s offset %s
        """,
        tuple(params + [limit, offset]),
    )
    total = int(count_frame.iloc[0]["total_rows"]) if not count_frame.empty else 0
    quality = {
        "unknown_count": int(count_frame.iloc[0]["unknown_count"]) if not count_frame.empty else 0,
        "not_visible_count": int(count_frame.iloc[0]["not_visible_count"]) if not count_frame.empty else 0,
    }
    return _records(frame), total, pit_columns, quality


def get_members_research(
    concept_id: str, trade_date: str, limit: int, offset: int, data_version: str, pit_mode: PITMode = "strict"
) -> ConceptMembershipEnvelope:
    _validate_date_range(trade_date)
    version = require_market_data_version(data_version)
    rows, total, pit_columns, quality = _membership_rows(
        concept_id=concept_id, trade_date=trade_date, start_date="", end_date="", limit=limit, offset=offset, pit_mode=pit_mode
    )
    _validate_after_read(version)
    issues = _pit_quality_issues(pit_columns, quality["unknown_count"], quality["not_visible_count"], pit_mode)
    if total == 0:
        issues.append("CONCEPT_MEMBERSHIP_EMPTY")
    if len(rows) != total:
        issues.append(f"CONCEPT_MEMBERSHIP_TRUNCATED:{len(rows)}/{total}")
    return ConceptMembershipEnvelope(
        items=[ConceptMembershipResearchItem(**row) for row in rows],
        meta=_meta(
            version=version,
            rows=len(rows),
            total_rows=total,
            issues=issues,
            unavailable=bool(_pit_quality_issues(pit_columns, quality["unknown_count"], quality["not_visible_count"], "strict")) and pit_mode == "strict",
            sources={"ref.concept_stock_membership", "ref.stock", "schema.ref.concept_stock_membership.pit"},
            source_semantics={"knowledge_time": "never inferred from effective_date or updated_at"},
            pit_mode=pit_mode,
        ),
    )


def get_member_history_research(
    concept_id: str, start_date: str, end_date: str, limit: int, offset: int, data_version: str, pit_mode: PITMode = "strict"
) -> ConceptMemberHistoryEnvelope:
    _validate_date_range("", start_date, end_date)
    version = require_market_data_version(data_version)
    rows, total_intervals, pit_columns, quality = _membership_rows(
        concept_id=concept_id, trade_date="", start_date=start_date, end_date=end_date, limit=limit, offset=offset, pit_mode=pit_mode
    )
    events: list[ConceptMemberHistoryResearchItem] = []
    for row in rows:
        events.append(
            ConceptMemberHistoryResearchItem(
                **row, effective_date=row["valid_from"], action="in", action_timing="start_of_day"
            )
        )
        if row.get("valid_to"):
            events.append(
                ConceptMemberHistoryResearchItem(
                    **row, effective_date=row["valid_to"], action="out", action_timing="end_of_day"
                )
            )
    events.sort(key=lambda item: (item.effective_date, item.code, item.action))
    _validate_after_read(version)
    issues = _pit_quality_issues(pit_columns, quality["unknown_count"], quality["not_visible_count"], pit_mode)
    if total_intervals == 0:
        issues.append("CONCEPT_MEMBERSHIP_HISTORY_EMPTY")
    if len(rows) != total_intervals:
        issues.append(f"CONCEPT_MEMBERSHIP_HISTORY_TRUNCATED:{len(rows)}/{total_intervals}")
    return ConceptMemberHistoryEnvelope(
        items=events,
        meta=_meta(
            version=version,
            rows=len(events),
            total_rows=total_intervals,
            issues=issues,
            unavailable=bool(_pit_quality_issues(pit_columns, quality["unknown_count"], quality["not_visible_count"], "strict")) and pit_mode == "strict",
            sources={"ref.concept_stock_membership", "ref.stock", "schema.ref.concept_stock_membership.pit"},
            source_semantics={
                "effective_date": "valid_from emits start_of_day in; stored inclusive valid_to emits end_of_day out",
                "knowledge_time": "never inferred from effective_date or updated_at",
            },
            pit_mode=pit_mode,
        ),
    )


def _research_range_clauses(concept_id: str | list[str], trade_date: str, start_date: str, end_date: str) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if isinstance(concept_id, list) and concept_id:
        clauses.append("spine.concept_id = any(%s)")
        params.append(concept_id)
    elif concept_id:
        clauses.append("spine.concept_id = %s")
        params.append(concept_id)
    if trade_date:
        clauses.append("spine.trade_date = %s::date")
        params.append(trade_date)
    if start_date:
        clauses.append("spine.trade_date >= %s::date")
        params.append(start_date)
    if end_date:
        clauses.append("spine.trade_date <= %s::date")
        params.append(end_date)
    return clauses, params


def get_daily_stats_research(
    concept_id: str | list[str], trade_date: str, start_date: str, end_date: str, limit: int, offset: int, data_version: str,
    pit_mode: PITMode = "strict",
) -> ConceptDailyStatsEnvelope:
    _validate_date_range(trade_date, start_date, end_date)
    version = require_market_data_version(data_version)
    pit_columns = _pit_columns_available()
    pit_visible = (
        "membership.stock_code is not null"
        if _uses_effective_date(pit_mode)
        else
        "membership.knowledge_time_status = 'known' "
        "and membership.knowledge_time is not null "
        "and membership.knowledge_time < ((scoped.trade_date + interval '1 day') at time zone 'Asia/Shanghai')"
        if pit_columns
        else "false"
    )
    clauses, params = _research_range_clauses(concept_id, trade_date, start_date, end_date)
    where_sql = f"where {' and '.join(clauses)}" if clauses else ""
    spine_filter = f"where {_effective_membership_exists('concept_daily')}" if _uses_effective_date(pit_mode) else ""
    base_sql = f"""
        with spine as (
            select concept_daily.concept_id, concept_daily.trade_date, concept_daily.amount as turnover_amount
            from fact.concept_daily_1d concept_daily
            {spine_filter}
        ), scoped as (
            select * from spine {where_sql}
        ), member_facts as (
            select scoped.concept_id, scoped.trade_date, scoped.turnover_amount,
                   membership.stock_market, membership.stock_code,
                   ({pit_visible}) as pit_visible,
                   stock_ref.code is not null
                     and stock_ref.listed_date <= scoped.trade_date
                     and (stock_ref.delisted_date is null or stock_ref.delisted_date >= scoped.trade_date)
                     as stock_eligible,
                    stock_daily.close, stock_daily.is_suspended,
                    price_band.upper_limit, price_band.lower_limit, price_band.price_band_status,
                   money_flow.main_inflow, money_flow.main_outflow, money_flow.net_inflow
            from scoped
            left join ref.concept_stock_membership membership
              on membership.concept_id = scoped.concept_id
             and membership.valid_from <= scoped.trade_date
             and (membership.valid_to is null or membership.valid_to >= scoped.trade_date)
            left join fact.stock_daily_1d stock_daily
              on stock_daily.market = membership.stock_market and stock_daily.code = membership.stock_code
             and stock_daily.trade_date = scoped.trade_date
            left join ref.stock stock_ref
              on stock_ref.market = membership.stock_market and stock_ref.code = membership.stock_code
            left join fact.stock_price_band_daily price_band
              on price_band.market = membership.stock_market and price_band.code = membership.stock_code
             and price_band.trade_date = scoped.trade_date
            left join fact.stock_money_flow_daily money_flow
              on money_flow.market = membership.stock_market and money_flow.code = membership.stock_code
             and money_flow.trade_date = scoped.trade_date
        ), aggregated as (
            select concept_id, trade_date, max(turnover_amount) as turnover_amount,
                   count(stock_code) filter (where pit_visible and stock_eligible)::int as member_count,
                   count(*) filter (
                       where pit_visible and stock_eligible and not coalesce(is_suspended, false)
                         and close is not null and upper_limit is not null
                         and abs(close - upper_limit) <= 0.001
                   )::int as limit_up_count,
                   sum(net_inflow) filter (
                       where pit_visible and stock_eligible and not coalesce(is_suspended, false)
                   ) as main_net_inflow_amount,
                    count(*) filter (
                        where stock_code is not null and pit_visible and stock_eligible
                          and not coalesce(
                              price_band_status = 'no_price_limit'
                              or (upper_limit is not null and lower_limit is not null)
                          , false)
                    )::int as missing_price_band_count,
                   count(*) filter (
                       where stock_code is not null and pit_visible and stock_eligible
                         and not coalesce(is_suspended, false) and net_inflow is null
                   )::int as missing_money_flow_count,
                   count(*) filter (
                       where stock_code is not null and stock_eligible and not pit_visible
                   )::int as unknown_knowledge_count
            from member_facts
            group by concept_id, trade_date
        )
    """
    count_frame = query_dataframe(
        base_sql
        + """
        select count(*) as total_rows,
               coalesce(sum(missing_price_band_count), 0)::bigint as missing_price_band_count,
               coalesce(sum(missing_money_flow_count), 0)::bigint as missing_money_flow_count,
               coalesce(sum(unknown_knowledge_count), 0)::bigint as unknown_knowledge_count,
               count(*) filter (where member_count = 0)::bigint as no_visible_member_count
        from aggregated
        """,
        tuple(params),
    )
    frame = query_dataframe(
        base_sql
        + """
        select concept_id, trade_date::text as trade_date, member_count, limit_up_count,
               main_net_inflow_amount, turnover_amount, missing_price_band_count, missing_money_flow_count
               , unknown_knowledge_count
        from aggregated
        order by concept_id, trade_date
        limit %s offset %s
        """,
        tuple(params + [limit, offset]),
    )
    rows = _records(frame)
    _validate_after_read(version)
    total = int(count_frame.iloc[0]["total_rows"]) if not count_frame.empty else 0
    issues: list[str] = []
    if total == 0:
        issues.append("CONCEPT_DAILY_STATS_EMPTY")
    missing_price = int(count_frame.iloc[0]["missing_price_band_count"]) if not count_frame.empty else 0
    missing_flow = int(count_frame.iloc[0]["missing_money_flow_count"]) if not count_frame.empty else 0
    unknown_knowledge = int(count_frame.iloc[0]["unknown_knowledge_count"]) if not count_frame.empty else 0
    no_visible_members = int(count_frame.iloc[0]["no_visible_member_count"]) if not count_frame.empty else 0
    if missing_price:
        issues.append(f"CONCEPT_DAILY_STATS_PRICE_BAND_INCOMPLETE:{missing_price}")
    if missing_flow:
        issues.append(f"CONCEPT_DAILY_STATS_MONEY_FLOW_INCOMPLETE:{missing_flow}")
    if unknown_knowledge:
        issues.append(f"CONCEPT_MEMBERSHIP_KNOWLEDGE_TIME_UNKNOWN:{unknown_knowledge}")
    if no_visible_members:
        issues.append(f"CONCEPT_DAILY_STATS_NO_VISIBLE_MEMBERS:{no_visible_members}")
    if len(rows) != total:
        issues.append(f"CONCEPT_DAILY_STATS_TRUNCATED:{len(rows)}/{total}")
    pit_issues = [_effective_date_issue(pit_mode)] if _uses_effective_date(pit_mode) else ([] if pit_columns else ["CONCEPT_MEMBERSHIP_KNOWLEDGE_TIME_SCHEMA_UNAVAILABLE"])
    issues.extend(pit_issues)
    return ConceptDailyStatsEnvelope(
        items=[ConceptDailyStatsResearchItem(**row) for row in rows],
        meta=_meta(
            version=version,
            rows=len(rows),
            total_rows=total,
            issues=issues,
            unavailable=(bool(pit_issues) or unknown_knowledge > 0) and pit_mode == "strict",
            sources={
                "fact.concept_daily_1d",
                "ref.concept_stock_membership",
                "ref.stock",
                "schema.ref.concept_stock_membership.pit",
                "fact.stock_daily_1d",
                "fact.stock_price_band_daily",
                "fact.stock_money_flow_daily",
            },
            source_semantics={
                "main_net_inflow_amount": "sum of non-suspended member stock fact values in CNY",
                "turnover_amount": "stored concept daily amount in CNY",
                "limit_up_count": "member close equals stored upper_limit within 0.001; no synthetic limit price",
                "price_band_coverage": "both source-native limits, or explicit source-evidenced no_price_limit; one-sided, zero, and unknown states remain incomplete",
            },
            pit_mode=pit_mode,
        ),
    )


def get_money_flow_research(
    concept_id: str, trade_date: str, start_date: str, end_date: str, limit: int, offset: int, data_version: str,
    pit_mode: PITMode = "strict",
) -> ConceptMoneyFlowEnvelope:
    _validate_date_range(trade_date, start_date, end_date)
    version = require_market_data_version(data_version)
    pit_columns = _pit_columns_available()
    clauses, params = _research_range_clauses(concept_id, trade_date, start_date, end_date)
    where_sql = f"where {' and '.join(clauses)}" if clauses else ""
    spine_filter = f"where {_effective_membership_exists('concept_daily')}" if _uses_effective_date(pit_mode) else ""
    pit_visible = (
        "membership.stock_code is not null"
        if _uses_effective_date(pit_mode)
        else
        "membership.knowledge_time_status = 'known' "
        "and membership.knowledge_time is not null "
        "and membership.knowledge_time < ((scoped.trade_date + interval '1 day') at time zone 'Asia/Shanghai')"
        if pit_columns
        else "false"
    )
    base_sql = f"""
        with spine as (
            select concept_daily.concept_id, concept_daily.trade_date
            from fact.concept_daily_1d concept_daily
            {spine_filter}
        ), scoped as (
            select * from spine {where_sql}
        ), member_flows as (
            select scoped.concept_id, scoped.trade_date, membership.stock_code, ({pit_visible}) as pit_visible,
                   stock_ref.code is not null
                     and stock_ref.listed_date <= scoped.trade_date
                     and (stock_ref.delisted_date is null or stock_ref.delisted_date >= scoped.trade_date)
                     as stock_eligible,
                   stock_daily.is_suspended,
                   flow.main_inflow, flow.main_outflow, flow.net_inflow
            from scoped
            left join ref.concept_stock_membership membership
              on membership.concept_id = scoped.concept_id
             and membership.valid_from <= scoped.trade_date
             and (membership.valid_to is null or membership.valid_to >= scoped.trade_date)
            left join fact.stock_money_flow_daily flow
              on flow.market = membership.stock_market and flow.code = membership.stock_code
             and flow.trade_date = scoped.trade_date
            left join fact.stock_daily_1d stock_daily
              on stock_daily.market = membership.stock_market and stock_daily.code = membership.stock_code
             and stock_daily.trade_date = scoped.trade_date
            left join ref.stock stock_ref
              on stock_ref.market = membership.stock_market and stock_ref.code = membership.stock_code
        ), aggregated as (
            select concept_id, trade_date,
                   sum(main_inflow) filter (
                       where pit_visible and stock_eligible and not coalesce(is_suspended, false)
                   ) as inflow,
                   sum(main_outflow) filter (
                       where pit_visible and stock_eligible and not coalesce(is_suspended, false)
                   ) as outflow,
                   sum(net_inflow) filter (
                       where pit_visible and stock_eligible and not coalesce(is_suspended, false)
                   ) as net_inflow,
                   count(*) filter (
                       where stock_code is not null and pit_visible and stock_eligible
                         and not coalesce(is_suspended, false)
                   )::int as visible_member_count,
                   count(*) filter (
                       where stock_code is not null and pit_visible and stock_eligible
                         and not coalesce(is_suspended, false) and net_inflow is null
                   )::int as missing_flow_count,
                   count(*) filter (
                       where stock_code is not null and stock_eligible and not pit_visible
                   )::int as unknown_knowledge_count
            from member_flows
            group by concept_id, trade_date
        )
    """
    count_frame = query_dataframe(
        base_sql
        + """
        select count(*) as total_rows,
               coalesce(sum(missing_flow_count), 0)::bigint as missing_flow_count,
               coalesce(sum(unknown_knowledge_count), 0)::bigint as unknown_knowledge_count,
               count(*) filter (where visible_member_count = 0)::bigint as no_visible_member_count
        from aggregated
        """,
        tuple(params),
    )
    frame = query_dataframe(
        base_sql
        + """
        select concept_id, trade_date::text as trade_date, 'concept' as scope,
               inflow, outflow, net_inflow, visible_member_count, missing_flow_count, unknown_knowledge_count
        from aggregated
        order by concept_id, trade_date
        limit %s offset %s
        """,
        tuple(params + [limit, offset]),
    )
    raw_rows = _records(frame)
    items = [
        ConceptMoneyFlowResearchItem(
            **{
                key: value
                for key, value in row.items()
                if key not in {"visible_member_count", "missing_flow_count", "unknown_knowledge_count"}
            }
        )
        for row in raw_rows
    ]
    _validate_after_read(version)
    total = int(count_frame.iloc[0]["total_rows"]) if not count_frame.empty else 0
    missing_flow = int(count_frame.iloc[0]["missing_flow_count"]) if not count_frame.empty else 0
    unknown_knowledge = int(count_frame.iloc[0]["unknown_knowledge_count"]) if not count_frame.empty else 0
    no_visible_members = int(count_frame.iloc[0]["no_visible_member_count"]) if not count_frame.empty else 0
    issues: list[str] = []
    if total == 0:
        issues.append("CONCEPT_MONEY_FLOW_EMPTY")
    if missing_flow:
        issues.append(f"CONCEPT_MONEY_FLOW_INCOMPLETE:{missing_flow}")
    if unknown_knowledge:
        issues.append(f"CONCEPT_MEMBERSHIP_KNOWLEDGE_TIME_UNKNOWN:{unknown_knowledge}")
    if no_visible_members:
        issues.append(f"CONCEPT_MONEY_FLOW_NO_VISIBLE_MEMBERS:{no_visible_members}")
    if len(items) != total:
        issues.append(f"CONCEPT_MONEY_FLOW_TRUNCATED:{len(items)}/{total}")
    if _uses_effective_date(pit_mode):
        issues.append(_effective_date_issue(pit_mode))
    elif not pit_columns:
        issues.append("CONCEPT_MEMBERSHIP_KNOWLEDGE_TIME_SCHEMA_UNAVAILABLE")
    return ConceptMoneyFlowEnvelope(
        items=items,
        meta=_meta(
            version=version,
            rows=len(items),
            total_rows=total,
            issues=issues,
            unavailable=(not pit_columns or unknown_knowledge > 0) and pit_mode == "strict",
            sources={
                "fact.concept_daily_1d",
                "ref.concept_stock_membership",
                "ref.stock",
                "schema.ref.concept_stock_membership.pit",
                "fact.stock_daily_1d",
                "fact.stock_money_flow_daily",
            },
            source_semantics={
                "inflow": "CNY",
                "outflow": "CNY",
                "net_inflow": "CNY; stored member stock facts only; suspended members excluded",
            },
            pit_mode=pit_mode,
        ),
    )
