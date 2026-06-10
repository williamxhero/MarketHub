from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Query

from data_threads import run_data_task
from services import stocks
from services.common import filter_response_fields
from services.runtime_memory import run_with_memory_log


router = APIRouter()

STOCK_QUOTE_FIELDS = {"code", "trade_time", "freq", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "volume", "amount", "adjust"}
TECHNICAL_FIELDS = {
    "code",
    "trade_date",
    "adjust",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "ema12",
    "ema26",
    "dif",
    "dea",
    "macd",
    "rsi6",
    "rsi12",
    "rsi24",
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "boll_upper",
    "boll_mid",
    "boll_lower",
}


def _dump_item_list(loader: Callable[..., list[object]], args: tuple[object, ...]) -> list[dict[str, object]]:
    return [item.model_dump() for item in loader(*args)]


def _dump_optional_item(loader: Callable[..., object], args: tuple[object, ...]) -> dict[str, object]:
    item = loader(*args)
    return item.model_dump() if item is not None else {}


def _filter_items(loader: Callable[..., list[object]], args: tuple[object, ...], fields: str, allowed_fields: set[str]) -> list[dict[str, object]]:
    items = loader(*args)
    return filter_response_fields(items, fields, allowed_fields)


def _filter_quote_query_result(loader: Callable[..., object], args: tuple[object, ...], fields: str, allowed_fields: set[str]) -> dict[str, object]:
    result = loader(*args)
    payload = result.model_dump()
    if fields == "":
        return payload
    filtered_items = filter_response_fields(result.items, fields, allowed_fields)
    return {"items": filtered_items, "meta": payload["meta"]}


def _quote_request_detail(codes: str, freq: str, start_date: str, end_date: str, limit: int | None) -> dict[str, object]:
    code_count = len([item for item in codes.split(",") if item != ""])
    return {"freq": freq, "code_count": code_count, "start_date": start_date, "end_date": end_date, "limit": limit or 0}


def _filter_stock_quote_items(loader: Callable[..., list[object]], args: tuple[object, ...], fields: str, allowed_fields: set[str], detail: dict[str, object]) -> list[dict[str, object]]:
    return run_with_memory_log("stocks.quotes", detail, lambda: _filter_items(loader, args, fields, allowed_fields))


def _filter_stock_quote_query_result(loader: Callable[..., object], args: tuple[object, ...], fields: str, allowed_fields: set[str], detail: dict[str, object]) -> dict[str, object]:
    return run_with_memory_log("stocks.quotes.query", detail, lambda: _filter_quote_query_result(loader, args, fields, allowed_fields))


@router.get("/api/stocks/quotes")
async def api_stock_quotes(
    code: str = Query(""),
    codes: str = Query(""),
    freq: str = Query("1d"),
    trade_date: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
    start_time: str = Query(""),
    end_time: str = Query(""),
    count: int | None = Query(None, ge=1),
    adjust: str = Query("none"),
    fields: str = Query(""),
    limit: int | None = Query(None, ge=1),
    skip_suspended: bool = Query(True),
    fill_missing: bool = Query(False),
) -> list[dict[str, object]]:
    actual_codes = codes if codes != "" else code
    detail = _quote_request_detail(actual_codes, freq, start_date, end_date, limit)
    args = (code, codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust, limit, skip_suspended, fill_missing)
    return await run_data_task(_filter_stock_quote_items, stocks.get_quotes, args, fields, STOCK_QUOTE_FIELDS, detail)


@router.get("/api/stocks/quotes/query")
async def api_stock_quotes_query(
    code: str = Query(""),
    codes: str = Query(""),
    freq: str = Query("1d"),
    trade_date: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
    start_time: str = Query(""),
    end_time: str = Query(""),
    count: int | None = Query(None, ge=1),
    adjust: str = Query("none"),
    fields: str = Query(""),
    limit: int | None = Query(None, ge=1),
    skip_suspended: bool = Query(True),
    fill_missing: bool = Query(False),
) -> dict[str, object]:
    actual_codes = codes if codes != "" else code
    detail = _quote_request_detail(actual_codes, freq, start_date, end_date, limit)
    args = (code, codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust, limit, skip_suspended, fill_missing)
    return await run_data_task(_filter_stock_quote_query_result, stocks.get_quotes_query_result, args, fields, STOCK_QUOTE_FIELDS, detail)


@router.get("/api/stocks/quotes/daily-snapshot")
async def api_stock_daily_snapshot(
    trade_date: str = Query(...),
    fields: str = Query(""),
    limit: int = Query(10000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, object]]:
    args = (trade_date, limit, offset)
    return await run_data_task(_filter_items, stocks.get_market_daily_snapshot, args, fields, STOCK_QUOTE_FIELDS)


@router.get("/api/stocks/catalog")
async def api_stock_catalog(
    codes: str = Query(""),
    name: str = Query(""),
    market: str = Query(""),
    exchange: str = Query(""),
    list_status: str = Query(""),
    is_hs: str = Query(""),
    include_delisted: bool = Query(False),
    limit: int = Query(5000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, object]]:
    del market
    del is_hs
    args = (codes, name, exchange, list_status, include_delisted, limit, offset)
    return await run_data_task(_dump_item_list, stocks.get_catalog, args)


@router.get("/api/stocks/catalog/archive")
async def api_stock_catalog_archive(
    trade_date: str = Query(""),
    code: str = Query(""),
    name: str = Query(""),
    industry: str = Query(""),
    area: str = Query(""),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, object]]:
    args = (trade_date, code, name, industry, area, limit, offset)
    return await run_data_task(_dump_item_list, stocks.get_archive, args)


@router.get("/api/stocks/{code}/profile/basic")
async def api_stock_profile_basic(code: str) -> dict[str, object]:
    return await run_data_task(_dump_optional_item, stocks.get_basic, (code,))


@router.get("/api/stocks/{code}/profile")
async def api_stock_profile(code: str) -> dict[str, object]:
    return await run_data_task(_dump_optional_item, stocks.get_profile, (code,))


@router.get("/api/stocks/{code}/profile/name-history")
async def api_stock_name_history(code: str, start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_name_history, (code, start_date, end_date))


@router.get("/api/stocks/{code}/profile/managers")
async def api_stock_managers(code: str) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_managers, (code,))


@router.get("/api/stocks/{code}/profile/management-rewards")
async def api_stock_management_rewards(code: str, start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_management_rewards, (code, start_date, end_date))


@router.get("/api/stocks/{code}/signals/hl")
async def api_stock_hl_signal(code: str, trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_hl_signal, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/signals/nine-turn")
async def api_stock_nine_turn(code: str, freq: str = Query("daily"), trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_nine_turn, (code, freq, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/factors/adj")
async def api_stock_adj_factors(code: str, start_date: str = Query(""), end_date: str = Query(""), base_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_adj_factors, (code, start_date, end_date, base_date))


@router.get("/api/stocks/{code}/factors/technical")
async def api_stock_technical_factors(
    code: str,
    trade_date: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
    adjust: str = Query("none"),
    fields: str = Query(""),
) -> list[dict[str, object]]:
    args = (code, trade_date, start_date, end_date, adjust)
    return await run_data_task(_filter_items, stocks.get_technical_factors, args, fields, TECHNICAL_FIELDS)


@router.get("/api/stocks/{code}/indicators/money-flow")
async def api_stock_money_flow(code: str, trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query(""), view: str = Query("summary")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_money_flow, (code, trade_date, start_date, end_date, view))


@router.get("/api/stocks/indicators/ah-comparisons")
async def api_stock_ah_comparisons(
    code: str = Query(""),
    trade_date: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_ah_comparisons, (code, trade_date, start_date, end_date, limit, offset))


@router.get("/api/stocks/indicators/daily-basic")
async def api_stock_daily_basic(code: str = Query(""), codes: str = Query(""), trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_daily_basic, (code, codes, trade_date, start_date, end_date))


@router.get("/api/stocks/indicators/daily-valuation")
async def api_stock_daily_valuation(code: str = Query(""), codes: str = Query(""), trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_daily_valuation, (code, codes, trade_date, start_date, end_date))


@router.get("/api/stocks/indicators/daily-market-value")
async def api_stock_daily_market_value(code: str = Query(""), codes: str = Query(""), trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_daily_market_value, (code, codes, trade_date, start_date, end_date))


@router.get("/api/stocks/indicators/risk-flags")
async def api_stock_risk_flags(
    trade_date: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
    flag_type: str = Query(""),
    status: str = Query(""),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_risk_flags, (trade_date, start_date, end_date, flag_type, status, limit, offset))


@router.get("/api/stocks/{code}/indicators/premarket")
async def api_stock_premarket(code: str, trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_premarket, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/indicators/chip-distribution")
async def api_stock_chip_distribution(code: str, trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_chip_distribution, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/indicators/chip-performance")
async def api_stock_chip_performance(code: str, trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_chip_performance, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/finance/statements")
async def api_stock_financial_statements(code: str = Query(""), codes: str = Query(""), report_period: str = Query(""), start_period: str = Query(""), end_period: str = Query(""), report_type: str = Query("income_statement")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_financial_statements, (code, codes, report_period, start_period, end_period, report_type))


@router.get("/api/stocks/finance/indicators")
async def api_stock_finance_indicators(code: str = Query(""), codes: str = Query(""), report_period: str = Query(""), start_period: str = Query(""), end_period: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_finance_indicators, (code, codes, report_period, start_period, end_period))


@router.get("/api/stocks/{code}/finance/audits")
async def api_stock_audits(code: str, report_period: str = Query(""), start_period: str = Query(""), end_period: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_audits, (code, report_period, start_period, end_period))


@router.get("/api/stocks/{code}/finance/disclosure-dates")
async def api_stock_disclosure_dates(code: str, report_period: str = Query(""), start_period: str = Query(""), end_period: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_disclosure_dates, (code, report_period, start_period, end_period))


@router.get("/api/stocks/{code}/finance/express")
async def api_stock_express(code: str, report_period: str = Query(""), start_period: str = Query(""), end_period: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_express, (code, report_period, start_period, end_period))


@router.get("/api/stocks/{code}/finance/forecasts")
async def api_stock_forecasts(code: str, report_period: str = Query(""), start_period: str = Query(""), end_period: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_forecasts, (code, report_period, start_period, end_period))


@router.get("/api/stocks/{code}/finance/main-business")
async def api_stock_main_business(code: str, report_period: str = Query(""), start_period: str = Query(""), end_period: str = Query(""), classification: str = Query("industry")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_main_business, (code, report_period, start_period, end_period, classification))


@router.get("/api/stocks/{code}/corporate-actions/dividends")
async def api_stock_dividends(code: str, start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_dividends, (code, start_date, end_date))


@router.get("/api/stocks/{code}/corporate-actions/repurchases")
async def api_stock_repurchases(code: str, start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_repurchases, (code, start_date, end_date))


@router.get("/api/stocks/{code}/corporate-actions/rights-issues")
async def api_stock_rights_issues(code: str, start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_rights_issues, (code, start_date, end_date))


@router.get("/api/stocks/{code}/corporate-actions/share-changes")
async def api_stock_share_changes(code: str, trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_share_changes, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/corporate-actions/unlock-schedules")
async def api_stock_unlock_schedules(code: str, unlock_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_unlock_schedules, (code, unlock_date, start_date, end_date))


@router.get("/api/stocks/{code}/ownership/ccass-holdings")
async def api_stock_ccass_holdings(code: str, trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_ccass_holdings, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/ownership/ccass-holding-details")
async def api_stock_ccass_holding_details(code: str, trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_ccass_holding_details, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/ownership/hk-connect-holdings")
async def api_stock_hk_connect_holdings(code: str, trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_hk_connect_holdings, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/ownership/pledges/stats")
async def api_stock_pledge_stats(code: str, trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_pledge_stats, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/ownership/pledges/details")
async def api_stock_pledge_details(code: str, start_date: str = Query(""), end_date: str = Query(""), status: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_pledge_details, (code, start_date, end_date, status))


@router.get("/api/stocks/{code}/ownership/shareholders/count")
async def api_stock_shareholder_count(code: str, trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_shareholder_count, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/ownership/shareholders/changes")
async def api_stock_shareholder_changes(code: str, trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_shareholder_changes, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/ownership/shareholders/top10")
async def api_stock_shareholder_top10(code: str, report_period: str = Query(""), start_period: str = Query(""), end_period: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_shareholder_top10, (code, report_period, start_period, end_period))


@router.get("/api/stocks/{code}/ownership/shareholders/top10-float")
async def api_stock_shareholder_top10_float(code: str, report_period: str = Query(""), start_period: str = Query(""), end_period: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_shareholder_top10_float, (code, report_period, start_period, end_period))


@router.get("/api/stocks/{code}/research/reports")
async def api_stock_research_reports(code: str, report_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_research_reports, (code, report_date, start_date, end_date))


@router.get("/api/stocks/{code}/research/surveys")
async def api_stock_surveys(code: str, survey_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_surveys, (code, survey_date, start_date, end_date))


@router.get("/api/stocks/reference/bse-code-mappings")
async def api_stock_bse_code_mappings(old_code: str = Query(""), new_code: str = Query(""), status: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_bse_code_mappings, (old_code, new_code, status))


@router.get("/api/stocks/reference/hk-connect-targets")
async def api_stock_hk_connect_targets(direction: str = Query(""), status: str = Query(""), effective_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_hk_connect_targets, (direction, status, effective_date))


@router.get("/api/stocks/{code}/quotes/auctions")
async def api_stock_auctions(code: str, session: str = Query("open"), trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_auctions, (code, session, trade_date, start_date, end_date))
