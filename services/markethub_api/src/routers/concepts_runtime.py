from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Query

from data_threads import run_data_task
from services import concepts_runtime
from services.common import filter_response_fields


router = APIRouter()

CONCEPT_QUOTE_FIELDS = {"concept_id", "concept_name", "trade_time", "freq", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "volume", "amount"}
CONCEPT_MONEY_FLOW_FIELDS = {"concept_id", "trade_date", "scope", "inflow", "outflow", "net_inflow"}


def _dump_item_list(loader: Callable[..., list[object]], args: tuple[object, ...]) -> list[dict[str, object]]:
    return [item.model_dump() for item in loader(*args)]


def _dump_optional_item(loader: Callable[..., object], args: tuple[object, ...]) -> dict[str, object]:
    item = loader(*args)
    return item.model_dump() if item is not None else {}


def _filter_items(loader: Callable[..., list[object]], args: tuple[object, ...], fields: str, allowed_fields: set[str]) -> list[dict[str, object]]:
    items = loader(*args)
    return filter_response_fields(items, fields, allowed_fields)


@router.get("/api/concepts/quotes", summary="查询题材概念行情")
async def api_concept_quotes(
    concept_id: str = Query("", description="系统 Concept ID，例如 C231。"),
    concept_ids: str = Query("", description="多个系统 Concept ID，逗号分隔。"),
    freq: str = Query("1d", description="行情频率。"),
    trade_date: str = Query("", description="交易日。"),
    start_date: str = Query("", description="开始日期。"),
    end_date: str = Query("", description="结束日期。"),
    start_time: str = Query("", description="开始时间。"),
    end_time: str = Query("", description="结束时间。"),
    count: int | None = Query(None, ge=1, description="每个 Concept ID 最多返回的最近记录数。"),
    fields: str = Query("", description="返回字段白名单，逗号分隔。"),
    limit: int = Query(200, ge=1, le=5000, description="返回记录上限。"),
) -> list[dict[str, object]]:
    args = (concept_id, concept_ids, freq, trade_date, start_date, end_date, start_time, end_time, count, limit)
    return await run_data_task(_filter_items, concepts_runtime.get_quotes, args, fields, CONCEPT_QUOTE_FIELDS)


@router.get("/api/concepts/quotes/daily-snapshot", summary="查询题材概念日行情快照")
async def api_concept_daily_snapshot(
    trade_date: str = Query(..., description="交易日。"),
    fields: str = Query("", description="返回字段白名单，逗号分隔。"),
    limit: int = Query(10000, ge=1, le=10000, description="返回记录上限。"),
    offset: int = Query(0, ge=0, description="结果偏移量。"),
) -> list[dict[str, object]]:
    args = (trade_date, limit, offset)
    return await run_data_task(_filter_items, concepts_runtime.get_market_daily_snapshot, args, fields, CONCEPT_QUOTE_FIELDS)


@router.get("/api/concepts/catalog", summary="查询题材概念目录")
async def api_concept_catalog(
    category: str = Query("", description="分类。"),
    market: str = Query("", description="市场。"),
    status: str = Query("", description="状态。"),
    limit: int = Query(200, ge=1, le=5000, description="返回记录上限。"),
    offset: int = Query(0, ge=0, description="结果偏移量。"),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, concepts_runtime.get_catalog, (category, market, status, limit, offset))


@router.get("/api/concepts/{concept_id}/profile", summary="查询题材概念资料")
async def api_concept_profile(concept_id: str) -> dict[str, object]:
    return await run_data_task(_dump_optional_item, concepts_runtime.get_profile, (concept_id,))


@router.get("/api/concepts/{concept_id}/members", summary="查询题材概念成分")
async def api_concept_members(
    concept_id: str,
    trade_date: str = Query("", description="交易日。"),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, concepts_runtime.get_members, (concept_id, trade_date))


@router.get("/api/concepts/{concept_id}/members/history", summary="查询题材概念成分历史")
async def api_concept_members_history(
    concept_id: str,
    start_date: str = Query("", description="开始日期。"),
    end_date: str = Query("", description="结束日期。"),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, concepts_runtime.get_member_history, (concept_id, start_date, end_date))


@router.get("/api/concepts/{concept_id}/indicators/money-flow", summary="查询题材概念资金流")
async def api_concept_money_flow(
    concept_id: str,
    trade_date: str = Query("", description="交易日。"),
    start_date: str = Query("", description="开始日期。"),
    end_date: str = Query("", description="结束日期。"),
    scope: str = Query("concept", description="资金流范围，当前仅支持 concept。"),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, concepts_runtime.get_money_flow, (concept_id, trade_date, start_date, end_date, scope))


@router.get("/api/concepts/indicators/money-flow", summary="查询题材概念资金流快照")
async def api_concept_money_flow_daily_snapshot(
    trade_date: str = Query(..., description="交易日。"),
    scope: str = Query("concept", description="资金流范围，当前仅支持 concept。"),
    fields: str = Query("", description="返回字段白名单，逗号分隔。"),
    limit: int = Query(10000, ge=1, le=10000, description="返回记录上限。"),
    offset: int = Query(0, ge=0, description="结果偏移量。"),
) -> list[dict[str, object]]:
    args = (trade_date, scope, limit, offset)
    return await run_data_task(_filter_items, concepts_runtime.get_market_money_flow, args, fields, CONCEPT_MONEY_FLOW_FIELDS)


@router.get("/api/concepts/reference/categories", summary="查询题材概念分类")
async def api_concept_categories(
    parent_code: str = Query("", description="父分类代码。"),
    level: int | None = Query(None, ge=1, description="分类层级。"),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, concepts_runtime.get_categories, (parent_code, level))
