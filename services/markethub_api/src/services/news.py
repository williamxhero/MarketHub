from __future__ import annotations

from fastapi import HTTPException

from core.config import DEFAULT_LIMIT
from quotemux import QuoteMux
from quotemux.models import NewsEventQueryResult
from quotemux.utils import format_date_value, normalize_stock_code
from services.common import ensure_limit


_QUOTEMUX = QuoteMux()


def get_events(
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
) -> NewsEventQueryResult:
    actual_trade_date = format_date_value(trade_date)
    if actual_trade_date == "":
        raise HTTPException(status_code=400, detail="trade_date 不能为空，且必须是单个交易日")
    actual_announcement_date = format_date_value(announcement_date) if announcement_date != "" else ""
    if announcement_date != "" and actual_announcement_date == "":
        raise HTTPException(status_code=400, detail="announcement_date 必须是 YYYY-MM-DD")
    actual_crawl_date = format_date_value(crawl_date) if crawl_date != "" else ""
    if crawl_date != "" and actual_crawl_date == "":
        raise HTTPException(status_code=400, detail="crawl_date 必须是 YYYY-MM-DD")
    if sort_by not in {"announcement_time", "crawl_time"}:
        raise HTTPException(status_code=400, detail="sort_by 只支持 announcement_time 或 crawl_time")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset 不能小于 0")
    return _QUOTEMUX.news.get_events(
        actual_trade_date,
        actual_announcement_date,
        actual_crawl_date,
        normalize_stock_code(stock_code) if stock_code != "" else "",
        event_type,
        min_importance_score,
        sort_by,
        ensure_limit(limit or DEFAULT_LIMIT),
        offset,
        include_sources,
        include_content_text,
    )
