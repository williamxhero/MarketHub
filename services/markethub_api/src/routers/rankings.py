from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Query

from data_threads import run_data_task
from services import rankings


router = APIRouter()


def _dump_item_list(loader: Callable[..., list[object]], args: tuple[object, ...]) -> list[dict[str, object]]:
    return [item.model_dump() for item in loader(*args)]


@router.get("/api/rankings/research/reports")
async def api_ranking_research_reports(trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query(""), limit: int = Query(200, ge=1, le=5000)) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, rankings.get_research_reports, (trade_date, start_date, end_date, limit))


@router.get("/api/rankings/research/broker-monthly-picks")
async def api_ranking_broker_monthly_picks(trade_month: str = Query(""), limit: int = Query(200, ge=1, le=5000)) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, rankings.get_broker_monthly_picks, (trade_month, limit))
