from __future__ import annotations

from fastapi import HTTPException

from core.config import DEFAULT_LIMIT
from quotemux import QuoteMux
from quotemux.models import BoardCatalogItem, BoardCategoryItem, BoardMemberHistoryItem, BoardMemberItem, BoardMoneyFlowItem, BoardQuoteItem
from services.common import ensure_limit, optional_board_codes, require_board_codes, require_board_money_flow_scope, require_board_quote_freq


MAX_BOARD_MONEY_FLOW_SNAPSHOT_LIMIT = 10000
_QUOTEMUX = QuoteMux()


def get_quotes(
    board_code: str,
    board_codes: str,
    freq: str,
    trade_date: str,
    start_date: str,
    end_date: str,
    start_time: str,
    end_time: str,
    count: int | None,
    limit: int,
) -> list[BoardQuoteItem]:
    # 允许空板块代码用于市场快照查询
    actual_board_codes = optional_board_codes(board_code, board_codes)
    if not actual_board_codes and trade_date == "":
        raise HTTPException(status_code=400, detail="board_code/board_codes 和 trade_date 至少需要传一个")
    
    return _QUOTEMUX.boards.get_quotes(
        actual_board_codes,
        require_board_quote_freq(freq),
        trade_date,
        start_date,
        end_date,
        start_time,
        end_time,
        count,
        ensure_limit(limit or DEFAULT_LIMIT),
    )


def get_market_daily_snapshot(trade_date: str, limit: int, offset: int) -> list[BoardQuoteItem]:
    """获取指定交易日全市场板块快照"""
    if trade_date == "":
        raise HTTPException(status_code=400, detail="trade_date 不能为空")
    if limit < 1 or limit > 10000:
        raise HTTPException(status_code=400, detail="limit 必须在 1 到 10000 之间")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset 不能小于 0")
    
    return _QUOTEMUX.boards.get_market_daily_snapshot(trade_date, limit, offset)


def get_catalog(category: str, market: str, status: str, limit: int, offset: int) -> list[BoardCatalogItem]:
    return _QUOTEMUX.boards.get_catalog(category, market, status, limit or DEFAULT_LIMIT, offset)


def get_profile(board_code: str) -> BoardCatalogItem | None:
    return _QUOTEMUX.boards.get_profile(board_code)


def get_members(board_code: str, trade_date: str) -> list[BoardMemberItem]:
    return _QUOTEMUX.boards.get_members(board_code, trade_date)


def get_member_history(board_code: str, start_date: str, end_date: str) -> list[BoardMemberHistoryItem]:
    return _QUOTEMUX.boards.get_member_history(board_code, start_date, end_date)


def get_money_flow(board_code: str, trade_date: str, start_date: str, end_date: str, scope: str) -> list[BoardMoneyFlowItem]:
    return _QUOTEMUX.boards.get_money_flow(board_code, trade_date, start_date, end_date, require_board_money_flow_scope(scope))


def get_market_money_flow(trade_date: str, scope: str, limit: int, offset: int) -> list[BoardMoneyFlowItem]:
    actual_scope = require_board_money_flow_scope(scope)
    if trade_date == "":
        raise HTTPException(status_code=400, detail="trade_date 不能为空")
    if limit < 1 or limit > MAX_BOARD_MONEY_FLOW_SNAPSHOT_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit 必须在 1 到 {MAX_BOARD_MONEY_FLOW_SNAPSHOT_LIMIT} 之间")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset 不能小于 0")
    return _QUOTEMUX.boards.get_market_money_flow(trade_date, actual_scope, limit, offset)


def get_categories(parent_code: str, level: int | None) -> list[BoardCategoryItem]:
    return _QUOTEMUX.boards.get_categories(parent_code, level)