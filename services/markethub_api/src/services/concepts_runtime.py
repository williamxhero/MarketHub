from __future__ import annotations

from fastapi import HTTPException

from core.config import DEFAULT_LIMIT
from quotemux import QuoteMux
from quotemux.models import ConceptCatalogItem, ConceptCategoryItem, ConceptMemberHistoryItem, ConceptMemberItem, ConceptMoneyFlowItem, ConceptQuoteItem
from services.common import ensure_limit, optional_concept_ids, require_concept_money_flow_scope, require_concept_quote_freq


MAX_CONCEPT_MONEY_FLOW_SNAPSHOT_LIMIT = 10000
_QUOTEMUX = QuoteMux()


def get_quotes(
    concept_id: str,
    concept_ids: str,
    freq: str,
    trade_date: str,
    start_date: str,
    end_date: str,
    start_time: str,
    end_time: str,
    count: int | None,
    limit: int,
) -> list[ConceptQuoteItem]:
    actual_concept_ids = optional_concept_ids(concept_id, concept_ids)
    if actual_concept_ids == [] and trade_date == "":
        raise HTTPException(status_code=400, detail="concept_id/concept_ids 和 trade_date 至少需要传一个")
    return _QUOTEMUX.concepts.get_quotes(
        actual_concept_ids,
        require_concept_quote_freq(freq),
        trade_date,
        start_date,
        end_date,
        start_time,
        end_time,
        count,
        ensure_limit(limit or DEFAULT_LIMIT),
    )


def get_market_daily_snapshot(trade_date: str, limit: int, offset: int) -> list[ConceptQuoteItem]:
    if trade_date == "":
        raise HTTPException(status_code=400, detail="trade_date 必填")
    if limit < 1 or limit > 10000:
        raise HTTPException(status_code=400, detail="limit 必须在 1 到 10000 之间")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset 必须大于等于 0")
    return _QUOTEMUX.concepts.get_market_daily_snapshot(trade_date, limit, offset)


def get_catalog(category: str, market: str, status: str, limit: int, offset: int) -> list[ConceptCatalogItem]:
    return _QUOTEMUX.concepts.get_catalog(category, market, status, limit or DEFAULT_LIMIT, offset)


def get_profile(concept_id: str) -> ConceptCatalogItem | None:
    return _QUOTEMUX.concepts.get_profile(concept_id)


def get_members(concept_id: str, trade_date: str) -> list[ConceptMemberItem]:
    return _QUOTEMUX.concepts.get_members(concept_id, trade_date)


def get_member_history(concept_id: str, start_date: str, end_date: str) -> list[ConceptMemberHistoryItem]:
    return _QUOTEMUX.concepts.get_member_history(concept_id, start_date, end_date)


def get_money_flow(concept_id: str, trade_date: str, start_date: str, end_date: str, scope: str) -> list[ConceptMoneyFlowItem]:
    return _QUOTEMUX.concepts.get_money_flow(concept_id, trade_date, start_date, end_date, require_concept_money_flow_scope(scope))


def get_market_money_flow(trade_date: str, scope: str, limit: int, offset: int) -> list[ConceptMoneyFlowItem]:
    actual_scope = require_concept_money_flow_scope(scope)
    if trade_date == "":
        raise HTTPException(status_code=400, detail="trade_date 必填")
    if limit < 1 or limit > MAX_CONCEPT_MONEY_FLOW_SNAPSHOT_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit 必须在 1 到 {MAX_CONCEPT_MONEY_FLOW_SNAPSHOT_LIMIT} 之间")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset 必须大于等于 0")
    return _QUOTEMUX.concepts.get_market_money_flow(trade_date, actual_scope, limit, offset)


def get_categories(parent_code: str, level: int | None) -> list[ConceptCategoryItem]:
    return _QUOTEMUX.concepts.get_categories(parent_code, level)
