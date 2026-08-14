from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from quotemux.models import ConceptCatalogItem, ConceptMemberHistoryItem, ConceptMemberItem, ConceptMoneyFlowItem, ConceptQuoteItem

from data_threads import run_data_task
from routers.concept_research_models import (
    ConceptCatalogEnvelope,
    ConceptDailyBarsEnvelope,
    ConceptMemberHistoryEnvelope,
    ConceptMembershipEnvelope,
    ConceptMoneyFlowEnvelope,
)
from services import concept_research, concepts_runtime
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


@router.get("/api/concepts/quotes", summary="查询题材概念行情", response_model=list[ConceptQuoteItem] | ConceptDailyBarsEnvelope)
async def api_concept_quotes(
    pit_mode: Literal["strict", "approx-historical"] = Query("strict"),
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
    offset: int = Query(0, ge=0, description="research-v1 结果偏移量。"),
    include_stats: bool = Query(False, description="research-v1 同时返回同版本 concept_daily_stats。"),
    contract: Literal["legacy", "research-v1"] = Query("legacy", description="默认 legacy 裸数组；research-v1 返回冻结版本 envelope。"),
    data_version: str = Query("", description="research-v1 必须携带 /api/health 返回的 data_version。"),
) -> list[dict[str, object]] | dict[str, object]:
    if contract == "research-v1":
        if freq != "1d":
            raise HTTPException(status_code=409, detail={"code": "CONCEPT_QUOTE_FREQ_UNAVAILABLE", "message": "research-v1 当前仅发布本地 fact.concept_daily_1d"})
        if count is not None or start_time or end_time or fields:
            raise HTTPException(
                status_code=400,
                detail={"code": "CONCEPT_RESEARCH_PARAMETER_UNSUPPORTED", "message": "research-v1 不支持 count/start_time/end_time/fields 裁剪"},
            )
        ids = [item.strip() for item in ([concept_id] + concept_ids.split(",")) if item.strip()]
        result = await run_data_task(
            concept_research.get_daily_bars_research,
            list(dict.fromkeys(ids)), trade_date, start_date, end_date, limit, offset, include_stats, data_version, pit_mode,
        )
        return result.model_dump()
    args = (concept_id, concept_ids, freq, trade_date, start_date, end_date, start_time, end_time, count, limit)
    return await run_data_task(_filter_items, concepts_runtime.get_quotes, args, fields, CONCEPT_QUOTE_FIELDS)


@router.get("/api/concepts/quotes/daily-snapshot", summary="查询题材概念日行情快照", response_model=list[ConceptQuoteItem] | ConceptDailyBarsEnvelope)
async def api_concept_daily_snapshot(
    pit_mode: Literal["strict", "approx-historical"] = Query("strict"),
    trade_date: str = Query(..., description="交易日。"),
    fields: str = Query("", description="返回字段白名单，逗号分隔。"),
    limit: int = Query(10000, ge=1, le=10000, description="返回记录上限。"),
    offset: int = Query(0, ge=0, description="结果偏移量。"),
    include_stats: bool = Query(False, description="research-v1 同时返回同版本 concept_daily_stats。"),
    contract: Literal["legacy", "research-v1"] = Query("legacy", description="默认 legacy 裸数组；research-v1 返回冻结版本 envelope。"),
    data_version: str = Query("", description="research-v1 必须携带 /api/health 返回的 data_version。"),
) -> list[dict[str, object]] | dict[str, object]:
    if contract == "research-v1":
        if fields:
            raise HTTPException(
                status_code=400,
                detail={"code": "CONCEPT_RESEARCH_PARAMETER_UNSUPPORTED", "message": "research-v1 不支持 fields 裁剪"},
            )
        result = await run_data_task(concept_research.get_daily_bars_research, [], trade_date, "", "", limit, offset, include_stats, data_version, pit_mode)
        return result.model_dump()
    args = (trade_date, limit, offset)
    return await run_data_task(_filter_items, concepts_runtime.get_market_daily_snapshot, args, fields, CONCEPT_QUOTE_FIELDS)


@router.get("/api/concepts/catalog", summary="查询题材概念目录", response_model=list[ConceptCatalogItem] | ConceptCatalogEnvelope)
async def api_concept_catalog(
    category: str = Query("", description="分类。"),
    market: str = Query("", description="市场。"),
    status: str = Query("", description="状态。"),
    limit: int = Query(200, ge=1, le=5000, description="返回记录上限。"),
    offset: int = Query(0, ge=0, description="结果偏移量。"),
    contract: Literal["legacy", "research-v1"] = Query("legacy", description="默认 legacy 裸数组；research-v1 返回冻结版本 envelope。"),
    data_version: str = Query("", description="research-v1 必须携带 /api/health 返回的 data_version。"),
) -> list[dict[str, object]] | dict[str, object]:
    if contract == "research-v1":
        result = await run_data_task(concept_research.get_catalog_research, category, market, status, limit, offset, data_version)
        return result.model_dump()
    return await run_data_task(_dump_item_list, concepts_runtime.get_catalog, (category, market, status, limit, offset))


@router.get("/api/concepts/{concept_id}/profile", summary="查询题材概念资料")
async def api_concept_profile(concept_id: str) -> dict[str, object]:
    return await run_data_task(_dump_optional_item, concepts_runtime.get_profile, (concept_id,))


@router.get("/api/concepts/{concept_id}/members", summary="查询题材概念成分", response_model=list[ConceptMemberItem] | ConceptMembershipEnvelope)
async def api_concept_members(
    concept_id: str,
    pit_mode: Literal["strict", "approx-historical"] = Query("strict"),
    trade_date: str = Query("", description="交易日。"),
    limit: int = Query(10000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    contract: Literal["legacy", "research-v1"] = Query("legacy"),
    data_version: str = Query("", description="research-v1 必须携带 /api/health 返回的 data_version。"),
) -> list[dict[str, object]] | dict[str, object]:
    if contract == "research-v1":
        result = await run_data_task(concept_research.get_members_research, concept_id, trade_date, limit, offset, data_version, pit_mode)
        return result.model_dump()
    return await run_data_task(_dump_item_list, concepts_runtime.get_members, (concept_id, trade_date))


@router.get("/api/concepts/{concept_id}/members/history", summary="查询题材概念成分历史", response_model=list[ConceptMemberHistoryItem] | ConceptMemberHistoryEnvelope)
async def api_concept_members_history(
    concept_id: str,
    pit_mode: Literal["strict", "approx-historical"] = Query("strict"),
    start_date: str = Query("", description="开始日期。"),
    end_date: str = Query("", description="结束日期。"),
    limit: int = Query(10000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    contract: Literal["legacy", "research-v1"] = Query("legacy"),
    data_version: str = Query("", description="research-v1 必须携带 /api/health 返回的 data_version。"),
) -> list[dict[str, object]] | dict[str, object]:
    if contract == "research-v1":
        result = await run_data_task(
            concept_research.get_member_history_research, concept_id, start_date, end_date, limit, offset, data_version, pit_mode
        )
        return result.model_dump()
    return await run_data_task(_dump_item_list, concepts_runtime.get_member_history, (concept_id, start_date, end_date))


@router.get("/api/concepts/{concept_id}/indicators/money-flow", summary="查询题材概念资金流", response_model=list[ConceptMoneyFlowItem] | ConceptMoneyFlowEnvelope)
async def api_concept_money_flow(
    concept_id: str,
    pit_mode: Literal["strict", "approx-historical"] = Query("strict"),
    trade_date: str = Query("", description="交易日。"),
    start_date: str = Query("", description="开始日期。"),
    end_date: str = Query("", description="结束日期。"),
    scope: str = Query("concept", description="资金流范围，当前仅支持 concept。"),
    limit: int = Query(10000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    contract: Literal["legacy", "research-v1"] = Query("legacy"),
    data_version: str = Query("", description="research-v1 必须携带 /api/health 返回的 data_version。"),
) -> list[dict[str, object]] | dict[str, object]:
    if contract == "research-v1":
        if scope != "concept":
            raise HTTPException(status_code=400, detail={"code": "CONCEPT_MONEY_FLOW_SCOPE_INVALID", "message": "research-v1 仅支持 concept scope"})
        result = await run_data_task(
            concept_research.get_money_flow_research, concept_id, trade_date, start_date, end_date, limit, offset, data_version, pit_mode
        )
        return result.model_dump()
    return await run_data_task(_dump_item_list, concepts_runtime.get_money_flow, (concept_id, trade_date, start_date, end_date, scope))


@router.get("/api/concepts/indicators/money-flow", summary="查询题材概念资金流快照", response_model=list[ConceptMoneyFlowItem] | ConceptMoneyFlowEnvelope)
async def api_concept_money_flow_daily_snapshot(
    pit_mode: Literal["strict", "approx-historical"] = Query("strict"),
    trade_date: str = Query(..., description="交易日。"),
    scope: str = Query("concept", description="资金流范围，当前仅支持 concept。"),
    fields: str = Query("", description="返回字段白名单，逗号分隔。"),
    limit: int = Query(10000, ge=1, le=10000, description="返回记录上限。"),
    offset: int = Query(0, ge=0, description="结果偏移量。"),
    contract: Literal["legacy", "research-v1"] = Query("legacy"),
    data_version: str = Query("", description="research-v1 必须携带 /api/health 返回的 data_version。"),
) -> list[dict[str, object]] | dict[str, object]:
    if contract == "research-v1":
        if scope != "concept":
            raise HTTPException(status_code=400, detail={"code": "CONCEPT_MONEY_FLOW_SCOPE_INVALID", "message": "research-v1 仅支持 concept scope"})
        if fields:
            raise HTTPException(
                status_code=400,
                detail={"code": "CONCEPT_RESEARCH_PARAMETER_UNSUPPORTED", "message": "research-v1 不支持 fields 裁剪"},
            )
        result = await run_data_task(concept_research.get_money_flow_research, "", trade_date, "", "", limit, offset, data_version, pit_mode)
        return result.model_dump()
    args = (trade_date, scope, limit, offset)
    return await run_data_task(_filter_items, concepts_runtime.get_market_money_flow, args, fields, CONCEPT_MONEY_FLOW_FIELDS)


@router.get("/api/concepts/reference/categories", summary="查询题材概念分类")
async def api_concept_categories(
    parent_code: str = Query("", description="父分类代码。"),
    level: int | None = Query(None, ge=1, description="分类层级。"),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, concepts_runtime.get_categories, (parent_code, level))
