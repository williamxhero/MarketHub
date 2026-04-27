from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Query

from data_threads import run_data_task
from services import boards
from services.common import filter_response_fields


router = APIRouter()

BOARD_QUOTE_FIELDS = {"board_code", "trade_time", "freq", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "volume", "amount"}
BOARD_MONEY_FLOW_FIELDS = {"board_code", "trade_date", "scope", "inflow", "outflow", "net_inflow"}


def _dump_item_list(loader: Callable[..., list[object]], args: tuple[object, ...]) -> list[dict[str, object]]:
    return [item.model_dump() for item in loader(*args)]


def _dump_optional_item(loader: Callable[..., object], args: tuple[object, ...]) -> dict[str, object]:
    item = loader(*args)
    return item.model_dump() if item is not None else {}


def _filter_items(loader: Callable[..., list[object]], args: tuple[object, ...], fields: str, allowed_fields: set[str]) -> list[dict[str, object]]:
    items = loader(*args)
    return filter_response_fields(items, fields, allowed_fields)


@router.get("/api/boards/quotes")
async def api_board_quotes(
    board_code: str = Query(""),
    board_codes: str = Query(""),
    freq: str = Query("1d"),
    trade_date: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
    start_time: str = Query(""),
    end_time: str = Query(""),
    count: int | None = Query(None, ge=1),
    fields: str = Query(""),
    limit: int = Query(200, ge=1, le=5000),
) -> list[dict[str, object]]:
    args = (board_code, board_codes, freq, trade_date, start_date, end_date, start_time, end_time, count, limit)
    return await run_data_task(_filter_items, boards.get_quotes, args, fields, BOARD_QUOTE_FIELDS)


@router.get("/api/boards/catalog")
async def api_board_catalog(
    category: str = Query(""),
    market: str = Query(""),
    status: str = Query(""),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, boards.get_catalog, (category, market, status, limit, offset))


@router.get("/api/boards/{board_code}/profile")
async def api_board_profile(board_code: str) -> dict[str, object]:
    return await run_data_task(_dump_optional_item, boards.get_profile, (board_code,))


@router.get("/api/boards/{board_code}/members")
async def api_board_members(
    board_code: str,
    trade_date: str = Query(""),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, boards.get_members, (board_code, trade_date))


@router.get("/api/boards/{board_code}/members/history")
async def api_board_members_history(
    board_code: str,
    start_date: str = Query(""),
    end_date: str = Query(""),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, boards.get_member_history, (board_code, start_date, end_date))


@router.get("/api/boards/{board_code}/indicators/money-flow")
async def api_board_money_flow(
    board_code: str,
    trade_date: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
    scope: str = Query("board"),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, boards.get_money_flow, (board_code, trade_date, start_date, end_date, scope))


@router.get("/api/boards/indicators/money-flow")
async def api_board_money_flow_daily_snapshot(
    trade_date: str = Query(...),
    scope: str = Query("board"),
    fields: str = Query(""),
    limit: int = Query(10000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, object]]:
    args = (trade_date, scope, limit, offset)
    return await run_data_task(_filter_items, boards.get_market_money_flow, args, fields, BOARD_MONEY_FLOW_FIELDS)


@router.get("/api/boards/reference/categories")
async def api_board_categories(
    parent_code: str = Query(""),
    level: int | None = Query(None, ge=1),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, boards.get_categories, (parent_code, level))
