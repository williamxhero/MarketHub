from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Query

from data_threads import run_data_task
from services import markets


router = APIRouter()


def _dump_item_list(loader: Callable[..., list[object]], args: tuple[object, ...]) -> list[dict[str, object]]:
    return [item.model_dump() for item in loader(*args)]


@router.get("/api/markets/calendar/trading")
async def api_market_trading_calendar(exchange: str = Query("SSE"), start_date: str = Query(""), end_date: str = Query(""), is_open: bool | None = Query(None)) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_trading_calendar, (exchange, start_date, end_date, is_open))


@router.get("/api/markets/calendar/trading/previous")
async def api_market_previous_trading_days(exchange: str = Query("SSE"), trade_date: str = Query(""), n: int = Query(1, ge=1, le=5000)) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_previous_trading_days, (exchange, trade_date, n))


@router.get("/api/markets/calendar/trading/next")
async def api_market_next_trading_days(exchange: str = Query("SSE"), trade_date: str = Query(""), n: int = Query(1, ge=1, le=5000)) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_next_trading_days, (exchange, trade_date, n))


@router.get("/api/markets/calendar/trading/yearly")
async def api_market_yearly_trading_calendar(exchange: str = Query("SSE"), start_year: int = Query(2024, ge=1990, le=2100), end_year: int = Query(2026, ge=1990, le=2100)) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_yearly_trading_calendar, (exchange, start_year, end_year))


@router.get("/api/markets/indicators/main-capital-flow")
async def api_market_main_capital_flow(trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_main_capital_flow, (trade_date, start_date, end_date))


@router.get("/api/markets/connect/capital-flow")
async def api_market_connect_capital_flow(trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_connect_capital_flow, (trade_date, start_date, end_date))


@router.get("/api/markets/connect/quotas")
async def api_market_connect_quotas(trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query(""), type: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_connect_quotas, (trade_date, start_date, end_date, type))


@router.get("/api/markets/connect/active-top10")
async def api_market_connect_active_top10(trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query(""), type: str = Query(""), limit: int = Query(200, ge=1, le=5000)) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_connect_active_top10, (trade_date, start_date, end_date, type, limit))


@router.get("/api/markets/events/block-trades")
async def api_market_block_trades(trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query(""), code: str = Query(""), limit: int = Query(200, ge=1, le=5000)) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_block_trades, (trade_date, start_date, end_date, code, limit))


@router.get("/api/markets/participants/dragon-tiger")
async def api_market_dragon_tiger(trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query(""), code: str = Query(""), limit: int = Query(200, ge=1, le=5000)) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_dragon_tiger, (trade_date, start_date, end_date, code, limit))


@router.get("/api/markets/participants/dragon-tiger/institutions")
async def api_market_dragon_tiger_institutions(trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query(""), code: str = Query(""), limit: int = Query(200, ge=1, le=5000)) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_dragon_tiger_institutions, (trade_date, start_date, end_date, code, limit))


@router.get("/api/markets/participants/hot-money")
async def api_market_hot_money(name: str = Query(""), tag: str = Query(""), limit: int = Query(200, ge=1, le=5000), offset: int = Query(0, ge=0)) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_hot_money, (name, tag, limit, offset))


@router.get("/api/markets/participants/hot-money/details")
async def api_market_hot_money_details(trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query(""), name: str = Query(""), limit: int = Query(200, ge=1, le=5000), offset: int = Query(0, ge=0)) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_hot_money_details, (trade_date, start_date, end_date, name, limit, offset))


@router.get("/api/markets/trading/open-auctions")
async def api_market_open_auctions(codes: str = Query(""), trade_date: str = Query(""), instrument_type: str = Query("stock")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_open_auctions, (codes, trade_date, instrument_type))


@router.get("/api/markets/trading/sessions")
async def api_market_sessions(codes: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_sessions, (codes,))
