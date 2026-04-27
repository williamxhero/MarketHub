from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Query

from data_threads import run_data_task
from services import indexes
from services.common import filter_response_fields


router = APIRouter()

INDEX_QUOTE_FIELDS = {"index_code", "trade_time", "freq", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "volume", "amount"}


def _dump_item_list(loader: Callable[..., list[object]], args: tuple[object, ...]) -> list[dict[str, object]]:
    return [item.model_dump() for item in loader(*args)]


def _dump_optional_item(loader: Callable[..., object], args: tuple[object, ...]) -> dict[str, object]:
    item = loader(*args)
    return item.model_dump() if item is not None else {}


def _filter_items(loader: Callable[..., list[object]], args: tuple[object, ...], fields: str, allowed_fields: set[str]) -> list[dict[str, object]]:
    items = loader(*args)
    return filter_response_fields(items, fields, allowed_fields)


@router.get("/api/indexes/catalog")
async def api_index_catalog(
    category: str = Query(""),
    market: str = Query(""),
    publisher: str = Query(""),
    status: str = Query(""),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, indexes.get_catalog, (category, market, publisher, status, limit, offset))


@router.get("/api/indexes/{index_code}/profile")
async def api_index_profile(index_code: str) -> dict[str, object]:
    return await run_data_task(_dump_optional_item, indexes.get_profile, (index_code,))


@router.get("/api/indexes/quotes")
async def api_index_quotes(
    index_code: str = Query(""),
    index_codes: str = Query(""),
    freq: str = Query("1d"),
    trade_date: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
    count: int | None = Query(None, ge=1),
    fields: str = Query(""),
    limit: int = Query(200, ge=1, le=5000),
) -> list[dict[str, object]]:
    args = (index_code, index_codes, freq, trade_date, start_date, end_date, count, limit)
    return await run_data_task(_filter_items, indexes.get_quotes, args, fields, INDEX_QUOTE_FIELDS)


@router.get("/api/indexes/{index_code}/members")
async def api_index_members(index_code: str, trade_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, indexes.get_members, (index_code, trade_date))
