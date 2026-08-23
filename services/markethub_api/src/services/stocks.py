from __future__ import annotations

from typing import Literal

import pandas as pd
from fastapi import HTTPException

from core.config import DEFAULT_LIMIT
from quotemux import QuoteMux, QuoteMuxPublicReader, StockQuotesRequest
from quotemux.infra.db.client import query_dataframe
from quotemux.models import AdjFactorItem, AuditItem, AuctionItem, BSECodeMappingItem, CcassHoldingDetailItem, CcassHoldingItem, ChipDistributionItem, ChipPerformanceItem, DisclosureDateItem, DividendItem, DividendPage, ExpressItem, ForecastItem, HKConnectHoldingItem, HKConnectTargetItem, HLSignalItem, LimitOrderAmountItem, MainBusinessItem, ManagementRewardItem, NameHistoryItem, NineTurnItem, PledgeDetailItem, PledgeStatItem, RepurchaseItem, ResearchReportItem, RightsIssueItem, RightsIssuePage, ShareChangeItem, ShareholderChangeItem, ShareholderCountItem, ShareholderTop10Item, StockAHComparisonItem, StockArchiveItem, StockBasicInfo, StockDailyBasicItem, StockDailyMarketValueItem, StockDailyValuationItem, StockFinanceIndicatorItem, StockFinancialPitRawItem, StockFinancialStatementItem, StockManagerItem, StockMarginItem, StockMoneyFlowItem, StockPremarketItem, StockProfileItem, StockQuoteItem, StockQuotesQueryResult, StockRiskFlagItem, StockStrategyFactorItem, SurveyItem, TechnicalFactorItem, UnlockScheduleItem
from services.common import ensure_limit, require_adjust, require_codes, require_money_flow_view, require_quote_freq, require_report_type
from services.market_data_version import require_market_data_version
from services.dataset_versions import STOCK_DAILY_DATASET_ID, current_dataset_version, require_dataset_version
from services.request_timing import record_stage_ms
from services.versioned_object_cache import get_or_build as get_or_build_object


_QUOTEMUX = QuoteMux()


def _read_stage(stage: str, duration_seconds: float) -> None:
    mapped = {"pool_wait": "db_pool", "sql_execute": "sql", "sql_fetch": "sql"}.get(stage)
    if mapped is not None:
        record_stage_ms(mapped, duration_seconds * 1_000)


_PUBLIC_READER = QuoteMuxPublicReader(stage_callback=_read_stage)


_STRATEGY_WINDOW_COVERAGE_QUERY = """
with open_dates as (
    select trade_date
    from ref.trade_calendar
    where exchange = 'SHSE'
      and is_open = true
      and trade_date between %s::date and %s::date
), eligible_stocks as (
    select market, code, listed_date, delisted_date
    from ref.stock
    where (
        (market = 'SHSE' and left(code, 1) = '6')
        or (market = 'SZSE' and left(code, 1) in ('0', '3'))
        or (market = 'BJSE' and (left(code, 1) in ('4', '8') or left(code, 3) = '920'))
    )
), strict_expected_rows as (
    select stocks.market, stocks.code, open_dates.trade_date
    from eligible_stocks stocks
    cross join open_dates
    where stocks.listed_date <= open_dates.trade_date
      and (stocks.delisted_date is null or stocks.delisted_date > open_dates.trade_date)
), migration_expected_rows as (
    select migration.old_market as market, migration.old_code as code, open_dates.trade_date
    from ref.stock_code_migration migration
    join open_dates
      on open_dates.trade_date = migration.trade_date
    join ref.stock successor_rows
      on successor_rows.market = migration.new_market
     and successor_rows.code = migration.new_code
     and successor_rows.listed_date <= open_dates.trade_date
     and (successor_rows.delisted_date is null or successor_rows.delisted_date > open_dates.trade_date)
    where migration.old_market = 'BJSE'
      and migration.new_market = 'BJSE'
      and (
          migration.old_code like '4%%'
          or migration.old_code like '8%%'
          or migration.old_code like '920%%'
      )
      and not exists (
          select 1
          from ref.stock_code_migration competing_migration
          where competing_migration.old_market = migration.old_market
            and competing_migration.old_code = migration.old_code
            and competing_migration.trade_date = migration.trade_date
            and (competing_migration.new_market, competing_migration.new_code)
                is distinct from (migration.new_market, migration.new_code)
      )
), expected_rows as (
    select market, code, trade_date from strict_expected_rows
    union
    select market, code, trade_date from migration_expected_rows
), filtered_expected_rows as (
    select market, code, trade_date
    from expected_rows
    where (%s = '' or code = any(%s))
), covered_rows as (
    select
        expected_rows.market,
        expected_rows.code,
        expected_rows.trade_date,
        daily_rows.code is not null as has_daily,
        exists (
            select 1
            from fact.stock_suspension_history suspensions
            where suspensions.market = expected_rows.market
              and suspensions.code = expected_rows.code
              and suspensions.status = 'suspended'
              and suspensions.suspend_start_date <= expected_rows.trade_date
              and suspensions.suspend_end_date >= expected_rows.trade_date
        ) as has_authoritative_suspension
    from filtered_expected_rows expected_rows
    left join fact.stock_daily_1d daily_rows
      on daily_rows.market = expected_rows.market
     and daily_rows.code = expected_rows.code
     and daily_rows.trade_date = expected_rows.trade_date
), strategy_expected_rows as (
    select market, code, trade_date, has_daily
    from covered_rows
    where has_daily or not has_authoritative_suspension
), missing_rows as (
    select market, code, trade_date
    from strategy_expected_rows
    where not has_daily
)
select
    (select count(*)::int from strategy_expected_rows) as expected_count,
    (select count(*)::int from missing_rows) as missing_count
"""


def get_quotes(
    code: str,
    codes: str,
    freq: str,
    trade_date: str,
    start_date: str,
    end_date: str,
    start_time: str,
    end_time: str,
    count: int | None,
    adjust: str,
    limit: int | None,
    skip_suspended: bool,
    skip_st: bool,
    fill_missing: bool,
) -> list[StockQuoteItem]:
    return _QUOTEMUX.stocks.get_quotes(
        StockQuotesRequest(
            codes=require_codes(code, codes),
            freq=require_quote_freq(freq),
            trade_date=trade_date,
            start_date=start_date,
            end_date=end_date,
            start_time=start_time,
            end_time=end_time,
            count=count,
            adjust=require_adjust(adjust),
            limit=limit,
            skip_suspended=skip_suspended,
            skip_st=skip_st,
            fill_missing=fill_missing,
        )
    )


def get_quotes_query_result(
    code: str,
    codes: str,
    freq: str,
    trade_date: str,
    start_date: str,
    end_date: str,
    start_time: str,
    end_time: str,
    count: int | None,
    adjust: str,
    limit: int | None,
    skip_suspended: bool,
    skip_st: bool,
    fill_missing: bool,
    meta_detail: Literal["summary", "full"],
    data_version: str,
) -> StockQuotesQueryResult:
    result = _QUOTEMUX.stocks.get_quotes_query_result(
        StockQuotesRequest(
            codes=require_codes(code, codes),
            freq=require_quote_freq(freq),
            trade_date=trade_date,
            start_date=start_date,
            end_date=end_date,
            start_time=start_time,
            end_time=end_time,
            count=count,
            adjust=require_adjust(adjust),
            limit=limit,
            skip_suspended=skip_suspended,
            skip_st=skip_st,
            fill_missing=fill_missing,
            meta_detail=meta_detail,
            data_version=require_market_data_version(data_version),
        )
    )
    if not result.meta.complete:
        raise HTTPException(
            status_code=409,
            detail={"code": "MARKET_DATA_INCOMPLETE", "message": "市场事实不完整，拒绝返回部分窗口", "details": result.meta.model_dump_json()},
        )
    return result


def _daily_batch_rows(batch: object) -> list[dict[str, object]]:
    rows = []
    for row in batch.as_dicts():
        rows.append({**row, "freq": "1d", "adjust": "none"})
    return rows


def get_market_daily_snapshot(
    trade_date: str,
    limit: int,
    offset: int,
    skip_suspended: bool,
    skip_st: bool,
    data_version: str = "",
    dataset_version: str = "",
) -> list[dict[str, object]]:
    try:
        actual_version = require_dataset_version(STOCK_DAILY_DATASET_ID, dataset_version, data_version)
        key = f"daily-snapshot|{actual_version}|{trade_date}|{limit}|{offset}|{skip_suspended}|{skip_st}"
        result, _ = get_or_build_object(
            key,
            lambda: _daily_batch_rows(
                _PUBLIC_READER.get_stock_daily_snapshot_batch(
                    trade_date, limit=limit, offset=offset, skip_suspended=skip_suspended, skip_st=skip_st,
                )
            ),
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def get_market_daily_local_window(
    start_date: str,
    end_date: str,
    limit: int,
    offset: int,
    skip_suspended: bool,
    skip_st: bool,
    data_version: str = "",
    dataset_version: str = "",
) -> list[dict[str, object]]:
    try:
        actual_version = require_dataset_version(STOCK_DAILY_DATASET_ID, dataset_version, data_version)
        key = f"daily-local-window|{actual_version}|{start_date}|{end_date}|{limit}|{offset}|{skip_suspended}|{skip_st}"
        result, _ = get_or_build_object(
            key,
            lambda: _daily_batch_rows(
                _PUBLIC_READER.get_stock_daily_local_window_batch(
                    start_date, end_date, limit=limit, offset=offset, skip_suspended=skip_suspended, skip_st=skip_st,
                )
            ),
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def get_money_flow(code: str, trade_date: str, start_date: str, end_date: str, view: str) -> list[StockMoneyFlowItem]:
    return _QUOTEMUX.stocks.get_money_flow(require_codes(code, "")[0], trade_date, start_date, end_date, require_money_flow_view(view))


def get_money_flow_batch(codes: str, trade_date: str, view: str) -> list[StockMoneyFlowItem]:
    code_list = require_codes("", codes)
    view_type = require_money_flow_view(view)
    return _QUOTEMUX.stocks.get_money_flow_batch(",".join(code_list), trade_date, view_type)


def get_money_flow_snapshot(trade_date: str, view: str) -> list[StockMoneyFlowItem]:
    return _QUOTEMUX.stocks.get_money_flow_snapshot(trade_date, require_money_flow_view(view))


def get_margin_snapshot(trade_date: str) -> list[StockMarginItem]:
    return _QUOTEMUX.stocks.get_margin_snapshot(trade_date)


def get_express_snapshot(announce_date: str) -> list[ExpressItem]:
    return _QUOTEMUX.stocks.get_express_snapshot(announce_date)


def get_forecast_snapshot(announce_date: str) -> list[ForecastItem]:
    return _QUOTEMUX.stocks.get_forecast_snapshot(announce_date)


def get_financial_statements(code: str, codes: str, report_period: str, start_period: str, end_period: str, report_type: str) -> list[StockFinancialStatementItem]:
    return _QUOTEMUX.stocks.get_financial_statements(require_codes(code, codes), report_period, start_period, end_period, require_report_type(report_type))


def get_finance_indicators(code: str, codes: str, report_period: str, start_period: str, end_period: str) -> list[StockFinanceIndicatorItem]:
    return _QUOTEMUX.stocks.get_finance_indicators(code, codes, report_period, start_period, end_period)


def get_catalog(codes: str, name: str, exchange: str, list_status: str, include_delisted: bool, limit: int, offset: int, data_version: str) -> list[StockBasicInfo]:
    actual_codes = require_codes("", codes) if codes else []
    market_version = require_market_data_version(data_version)
    dataset_version = current_dataset_version("stock_reference")
    key = "|".join(("catalog", dataset_version, ",".join(sorted(actual_codes)), name, exchange, list_status, str(include_delisted), str(limit), str(offset)))
    result, _ = get_or_build_object(
        key,
        lambda: _QUOTEMUX.stocks.get_catalog(actual_codes, name, exchange, list_status, include_delisted, limit or DEFAULT_LIMIT, offset, data_version=market_version),
    )
    return result


def get_archive(trade_date: str, code: str, name: str, industry: str, area: str, limit: int, offset: int) -> list[StockArchiveItem]:
    return _QUOTEMUX.stocks.get_archive(trade_date, code, name, industry, area, limit or DEFAULT_LIMIT, offset)


def get_basic(code: str, data_version: str = "", dataset_version: str = "") -> StockBasicInfo | None:
    actual_version = require_dataset_version("stock_reference", dataset_version, data_version)
    result, _ = get_or_build_object(
        f"profile-basic|{actual_version}|{code}",
        lambda: next(iter(_QUOTEMUX.stocks.get_catalog([code], "", "", "", True, 1, 0, data_version=actual_version)), None),
    )
    return result


def get_profile(code: str, data_version: str = "", dataset_version: str = "") -> StockProfileItem | None:
    actual_version = require_dataset_version("stock_reference", dataset_version, data_version)
    result, _ = get_or_build_object(f"profile-company|{actual_version}|{code}", lambda: _QUOTEMUX.stocks.get_profile(code))
    return result


def get_name_history(code: str, start_date: str, end_date: str) -> list[NameHistoryItem]:
    return _QUOTEMUX.stocks.get_name_history(code, start_date, end_date)


def get_managers(code: str) -> list[StockManagerItem]:
    return _QUOTEMUX.stocks.get_managers(code)


def get_management_rewards(code: str, start_date: str, end_date: str) -> list[ManagementRewardItem]:
    return _QUOTEMUX.stocks.get_management_rewards(code, start_date, end_date)


def get_hl_signal(code: str, trade_date: str, start_date: str, end_date: str) -> list[HLSignalItem]:
    return _QUOTEMUX.stocks.get_hl_signal(code, trade_date, start_date, end_date)


def get_limit_order_amount(trade_date: str) -> list[LimitOrderAmountItem]:
    try:
        return _QUOTEMUX.stocks.get_limit_order_amount(trade_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def run_limit_order_amount_capture(trade_date: str) -> dict[str, object]:
    try:
        return _QUOTEMUX.stocks.run_limit_order_amount_capture(trade_date)
    except ValueError as exc:
        return {"status": "failed", "trade_date": trade_date, "candidate_count": 0, "row_count": 0, "error_message": str(exc)}


def get_nine_turn(code: str, freq: str, trade_date: str, start_date: str, end_date: str) -> list[NineTurnItem]:
    return _QUOTEMUX.stocks.get_nine_turn(code, freq, trade_date, start_date, end_date)


def get_adj_factors(code: str, start_date: str, end_date: str, base_date: str) -> list[AdjFactorItem]:
    return _QUOTEMUX.stocks.get_adj_factors(require_codes(code, "")[0], start_date, end_date, base_date)


def _rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period, min_periods=period).mean()
    avg_loss = loss.rolling(period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - 100 / (1 + rs)


def get_technical_factors(code: str, trade_date: str, start_date: str, end_date: str, adjust: str) -> list[TechnicalFactorItem]:
    actual_adjust = require_adjust(adjust)
    actual_code = require_codes(code, "")[0]
    quote_items = get_quotes(actual_code, "", "1d", trade_date, start_date, end_date, "", "", None, actual_adjust, 5000, True, False, False)
    if not quote_items:
        return []
    frame = pd.DataFrame([item.model_dump() for item in quote_items])
    frame["trade_date"] = frame["trade_time"].astype(str)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["high"] = pd.to_numeric(frame["high"], errors="coerce")
    frame["low"] = pd.to_numeric(frame["low"], errors="coerce")
    frame = frame.sort_values("trade_date").reset_index(drop=True)
    frame["ma5"] = frame["close"].rolling(5, min_periods=5).mean()
    frame["ma10"] = frame["close"].rolling(10, min_periods=10).mean()
    frame["ma20"] = frame["close"].rolling(20, min_periods=20).mean()
    frame["ma60"] = frame["close"].rolling(60, min_periods=60).mean()
    frame["ema12"] = frame["close"].ewm(span=12, adjust=False).mean()
    frame["ema26"] = frame["close"].ewm(span=26, adjust=False).mean()
    frame["dif"] = frame["ema12"] - frame["ema26"]
    frame["dea"] = frame["dif"].ewm(span=9, adjust=False).mean()
    frame["macd"] = (frame["dif"] - frame["dea"]) * 2
    frame["rsi6"] = _rsi(frame["close"], 6)
    frame["rsi12"] = _rsi(frame["close"], 12)
    frame["rsi24"] = _rsi(frame["close"], 24)
    low_n = frame["low"].rolling(9, min_periods=9).min()
    high_n = frame["high"].rolling(9, min_periods=9).max()
    rsv = (frame["close"] - low_n) / (high_n - low_n).replace(0, pd.NA) * 100
    frame["kdj_k"] = rsv.ewm(com=2, adjust=False).mean()
    frame["kdj_d"] = frame["kdj_k"].ewm(com=2, adjust=False).mean()
    frame["kdj_j"] = 3 * frame["kdj_k"] - 2 * frame["kdj_d"]
    boll_mid = frame["close"].rolling(20, min_periods=20).mean()
    boll_std = frame["close"].rolling(20, min_periods=20).std()
    frame["boll_upper"] = boll_mid + 2 * boll_std
    frame["boll_mid"] = boll_mid
    frame["boll_lower"] = boll_mid - 2 * boll_std
    return [
        TechnicalFactorItem(
            code=str(row["code"]),
            trade_date=str(row["trade_date"]),
            adjust=actual_adjust,
            ma5=float(row["ma5"]) if pd.notna(row["ma5"]) else None,
            ma10=float(row["ma10"]) if pd.notna(row["ma10"]) else None,
            ma20=float(row["ma20"]) if pd.notna(row["ma20"]) else None,
            ma60=float(row["ma60"]) if pd.notna(row["ma60"]) else None,
            ema12=float(row["ema12"]) if pd.notna(row["ema12"]) else None,
            ema26=float(row["ema26"]) if pd.notna(row["ema26"]) else None,
            dif=float(row["dif"]) if pd.notna(row["dif"]) else None,
            dea=float(row["dea"]) if pd.notna(row["dea"]) else None,
            macd=float(row["macd"]) if pd.notna(row["macd"]) else None,
            rsi6=float(row["rsi6"]) if pd.notna(row["rsi6"]) else None,
            rsi12=float(row["rsi12"]) if pd.notna(row["rsi12"]) else None,
            rsi24=float(row["rsi24"]) if pd.notna(row["rsi24"]) else None,
            kdj_k=float(row["kdj_k"]) if pd.notna(row["kdj_k"]) else None,
            kdj_d=float(row["kdj_d"]) if pd.notna(row["kdj_d"]) else None,
            kdj_j=float(row["kdj_j"]) if pd.notna(row["kdj_j"]) else None,
            boll_upper=float(row["boll_upper"]) if pd.notna(row["boll_upper"]) else None,
            boll_mid=float(row["boll_mid"]) if pd.notna(row["boll_mid"]) else None,
            boll_lower=float(row["boll_lower"]) if pd.notna(row["boll_lower"]) else None,
        )
        for _, row in frame.iterrows()
    ]


def get_strategy_factor_window(start_date: str, end_date: str, codes: str, data_version: str) -> list[StockStrategyFactorItem]:
    require_market_data_version(data_version)
    actual_codes = _require_strategy_factor_codes(codes) if codes else []
    actual_codes_csv = ",".join(actual_codes)
    try:
        items = _QUOTEMUX.stocks.get_strategy_factor_window(start_date, end_date, actual_codes_csv)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    coverage = query_dataframe(_STRATEGY_WINDOW_COVERAGE_QUERY, (start_date, end_date, actual_codes_csv, actual_codes))
    if coverage.empty:
        raise HTTPException(status_code=503, detail={"code": "MARKET_DATA_INTEGRITY_UNAVAILABLE", "message": "无法校验策略因子窗口的市场事实完整性"})
    expected_count = int(coverage.iloc[0].get("expected_count", 0) or 0)
    missing_count = int(coverage.iloc[0].get("missing_count", 0) or 0)
    if expected_count != len(items) or missing_count != 0:
        raise HTTPException(
            status_code=409,
            detail={"code": "MARKET_DATA_INCOMPLETE", "message": "策略因子窗口的日线市场事实不完整", "details": f"expected={expected_count}, returned={len(items)}, missing={missing_count}"},
        )
    require_market_data_version(data_version)
    return items


def _require_strategy_factor_codes(codes: str) -> list[str]:
    """Normalize public BSE ``BJ.873690`` aliases before calling QuoteMux."""
    normalized_codes = []
    for item in codes.split(","):
        text = item.strip().upper()
        if not text:
            continue
        if text.startswith("BJ."):
            text = text[3:]
        normalized = require_codes("", text)[0]
        normalized_codes.append(normalized)
    if not normalized_codes:
        return require_codes("", "")
    return list(dict.fromkeys(normalized_codes))


def get_financial_pit_period(report_period: str) -> list[StockFinancialPitRawItem]:
    try:
        return _QUOTEMUX.stocks.get_financial_pit_period(report_period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def get_ah_comparisons(code: str, trade_date: str, start_date: str, end_date: str, limit: int, offset: int) -> list[StockAHComparisonItem]:
    return _QUOTEMUX.stocks.get_ah_comparisons(code, trade_date, start_date, end_date, limit or DEFAULT_LIMIT, offset)


def get_daily_basic(code: str, codes: str, trade_date: str, start_date: str, end_date: str) -> list[StockDailyBasicItem]:
    try:
        return _QUOTEMUX.stocks.get_daily_basic(code, codes, trade_date, start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def get_daily_valuation(code: str, codes: str, trade_date: str, start_date: str, end_date: str) -> list[StockDailyValuationItem]:
    try:
        return _QUOTEMUX.stocks.get_daily_valuation(code, codes, trade_date, start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def get_daily_market_value(code: str, codes: str, trade_date: str, start_date: str, end_date: str) -> list[StockDailyMarketValueItem]:
    try:
        return _QUOTEMUX.stocks.get_daily_market_value(code, codes, trade_date, start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def get_risk_flags(trade_date: str, start_date: str, end_date: str, flag_type: str, status: str, limit: int, offset: int) -> list[StockRiskFlagItem]:
    return _QUOTEMUX.stocks.get_risk_flags(trade_date, start_date, end_date, flag_type, status, limit or DEFAULT_LIMIT, offset)


def get_premarket(code: str, trade_date: str, start_date: str, end_date: str) -> list[StockPremarketItem]:
    return _QUOTEMUX.stocks.get_premarket(code, trade_date, start_date, end_date)


def get_chip_distribution(code: str, trade_date: str, start_date: str, end_date: str) -> list[ChipDistributionItem]:
    return _QUOTEMUX.stocks.get_chip_distribution(code, trade_date, start_date, end_date)


def get_chip_performance(code: str, trade_date: str, start_date: str, end_date: str) -> list[ChipPerformanceItem]:
    return _QUOTEMUX.stocks.get_chip_performance(code, trade_date, start_date, end_date)


def get_dividends(code: str, start_date: str, end_date: str) -> list[DividendItem]:
    return _QUOTEMUX.stocks.get_dividends(code, start_date, end_date)


def get_repurchases(code: str, start_date: str, end_date: str) -> list[RepurchaseItem]:
    return _QUOTEMUX.stocks.get_repurchases(code, start_date, end_date)


def get_rights_issues(code: str, start_date: str, end_date: str) -> list[RightsIssueItem]:
    return _QUOTEMUX.stocks.get_rights_issues(code, start_date, end_date)


def get_dividends_page(codes: str, start_date: str, end_date: str, offset: int, limit: int, data_version: str) -> DividendPage:
    version = require_market_data_version(data_version)
    page = _QUOTEMUX.stocks.get_dividends_page(codes, start_date, end_date, offset, limit)
    return _require_complete_corporate_action_page(page, version)


def get_rights_issues_page(codes: str, start_date: str, end_date: str, offset: int, limit: int, data_version: str) -> RightsIssuePage:
    version = require_market_data_version(data_version)
    page = _QUOTEMUX.stocks.get_rights_issues_page(codes, start_date, end_date, offset, limit)
    return _require_complete_corporate_action_page(page, version)


def _require_complete_corporate_action_page(page: DividendPage | RightsIssuePage, data_version: str) -> DividendPage | RightsIssuePage:
    if not page.coverage.complete:
        raise HTTPException(
            status_code=409,
            detail={"code": "MARKET_DATA_INCOMPLETE", "message": "公司行动覆盖不完整，拒绝返回部分窗口", "details": ",".join(page.coverage.missing_date_ranges)},
        )
    coverage = page.coverage.model_copy(update={"data_version": data_version})
    return page.model_copy(update={"coverage": coverage})


def get_share_changes(code: str, trade_date: str, start_date: str, end_date: str) -> list[ShareChangeItem]:
    return _QUOTEMUX.stocks.get_share_changes(code, trade_date, start_date, end_date)


def get_unlock_schedules(code: str, unlock_date: str, start_date: str, end_date: str) -> list[UnlockScheduleItem]:
    return _QUOTEMUX.stocks.get_unlock_schedules(code, unlock_date, start_date, end_date)


def get_audits(code: str, report_period: str, start_period: str, end_period: str) -> list[AuditItem]:
    return _QUOTEMUX.stocks.get_audits(code, report_period, start_period, end_period)


def get_disclosure_dates(code: str, report_period: str, start_period: str, end_period: str) -> list[DisclosureDateItem]:
    return _QUOTEMUX.stocks.get_disclosure_dates(code, report_period, start_period, end_period)


def get_express(code: str, report_period: str, start_period: str, end_period: str) -> list[ExpressItem]:
    return _QUOTEMUX.stocks.get_express(code, report_period, start_period, end_period)


def get_forecasts(code: str, report_period: str, start_period: str, end_period: str) -> list[ForecastItem]:
    return _QUOTEMUX.stocks.get_forecasts(code, report_period, start_period, end_period)


def get_main_business(code: str, report_period: str, start_period: str, end_period: str, classification: str) -> list[MainBusinessItem]:
    return _QUOTEMUX.stocks.get_main_business(code, report_period, start_period, end_period, classification)


def get_ccass_holdings(code: str, trade_date: str, start_date: str, end_date: str) -> list[CcassHoldingItem]:
    return _QUOTEMUX.stocks.get_ccass_holdings(code, trade_date, start_date, end_date)


def get_ccass_holding_details(code: str, trade_date: str, start_date: str, end_date: str) -> list[CcassHoldingDetailItem]:
    return _QUOTEMUX.stocks.get_ccass_holding_details(code, trade_date, start_date, end_date)


def get_hk_connect_holdings(code: str, trade_date: str, start_date: str, end_date: str) -> list[HKConnectHoldingItem]:
    return _QUOTEMUX.stocks.get_hk_connect_holdings(code, trade_date, start_date, end_date)


def get_pledge_stats(code: str, trade_date: str, start_date: str, end_date: str) -> list[PledgeStatItem]:
    return _QUOTEMUX.stocks.get_pledge_stats(code, trade_date, start_date, end_date)


def get_pledge_details(code: str, start_date: str, end_date: str, status: str) -> list[PledgeDetailItem]:
    return _QUOTEMUX.stocks.get_pledge_details(code, start_date, end_date, status)


def get_shareholder_count(code: str, trade_date: str, start_date: str, end_date: str) -> list[ShareholderCountItem]:
    return _QUOTEMUX.stocks.get_shareholder_count(code, trade_date, start_date, end_date)


def get_shareholder_changes(code: str, trade_date: str, start_date: str, end_date: str) -> list[ShareholderChangeItem]:
    items = _QUOTEMUX.stocks.get_shareholder_count(code, trade_date, start_date, end_date)
    rows: list[ShareholderChangeItem] = []
    previous_count: int | None = None
    for item in items:
        change_count = item.holder_count - previous_count if item.holder_count is not None and previous_count is not None else None
        change_pct = None
        if change_count is not None and previous_count not in {None, 0}:
            change_pct = change_count / previous_count * 100
        rows.append(ShareholderChangeItem(code=item.code, trade_date=item.trade_date, holder_count=item.holder_count, change_count=change_count, change_pct=change_pct))
        previous_count = item.holder_count
    return rows


def get_shareholder_top10(code: str, report_period: str, start_period: str, end_period: str) -> list[ShareholderTop10Item]:
    return _QUOTEMUX.stocks.get_shareholder_top10(code, report_period, start_period, end_period)


def get_shareholder_top10_float(code: str, report_period: str, start_period: str, end_period: str) -> list[ShareholderTop10Item]:
    return _QUOTEMUX.stocks.get_shareholder_top10_float(code, report_period, start_period, end_period)


def get_research_reports(code: str, report_date: str, start_date: str, end_date: str) -> list[ResearchReportItem]:
    return _QUOTEMUX.stocks.get_research_reports(code, report_date, start_date, end_date)


def get_surveys(code: str, survey_date: str, start_date: str, end_date: str) -> list[SurveyItem]:
    return _QUOTEMUX.stocks.get_surveys(code, survey_date, start_date, end_date)


def get_bse_code_mappings(old_code: str, new_code: str, status: str) -> list[BSECodeMappingItem]:
    return _QUOTEMUX.stocks.get_bse_code_mappings(old_code, new_code, status)


def get_hk_connect_targets(direction: str, status: str, effective_date: str) -> list[HKConnectTargetItem]:
    return _QUOTEMUX.stocks.get_hk_connect_targets(direction, status, effective_date)


def get_auctions(code: str, session: str, trade_date: str, start_date: str, end_date: str) -> list[AuctionItem]:
    return _QUOTEMUX.stocks.get_auctions(code, session, trade_date, start_date, end_date)
