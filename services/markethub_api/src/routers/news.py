from __future__ import annotations

from fastapi import APIRouter, Query

from data_threads import run_data_task
from services import news


router = APIRouter()


def _dump_news_events(
    trade_date: str,
    announcement_date: str,
    crawl_date: str,
    stock_code: str,
    event_type: str,
    min_importance_score: int | None,
    sort_by: str,
    limit: int,
    offset: int,
    include_sources: bool,
    include_content_text: bool,
) -> dict[str, object]:
    result = news.get_events(
        trade_date,
        announcement_date,
        crawl_date,
        stock_code,
        event_type,
        min_importance_score,
        sort_by,
        limit,
        offset,
        include_sources,
        include_content_text,
    )
    events: list[dict[str, object]] = []
    for item in result.events:
        payload = item.model_dump()
        if not include_sources:
            payload.pop("sources", None)
        if not include_content_text:
            payload.pop("content_text", None)
        events.append(payload)
    return {"events": events}


@router.get("/api/markets/events/news")
async def api_market_news_events(
    trade_date: str = Query(...),
    announcement_date: str = Query(""),
    crawl_date: str = Query(""),
    stock_code: str = Query(""),
    event_type: str = Query(""),
    min_importance_score: int | None = Query(None, ge=1, le=5),
    sort_by: str = Query("announcement_time"),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    include_sources: bool = Query(False),
    include_content_text: bool = Query(False),
) -> dict[str, object]:
    return await run_data_task(
        _dump_news_events,
        trade_date,
        announcement_date,
        crawl_date,
        stock_code,
        event_type,
        min_importance_score,
        sort_by,
        limit,
        offset,
        include_sources,
        include_content_text,
    )
