from __future__ import annotations

from collections.abc import Sequence

from fastapi import HTTPException
from pydantic import BaseModel

from core.config import DEFAULT_LIMIT, MAX_LIMIT
from quotemux.models import BoardQuoteItem, BoardRankingItem, IndexQuoteItem, StockQuoteItem
from quotemux.utils import normalize_index_code, normalize_stock_code, split_csv


def ensure_limit(limit: int) -> int:
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit 必须大于 0")
    return min(limit, MAX_LIMIT)


def require_codes(code: str, codes: str) -> list[str]:
    items = []
    if code:
        items.append(normalize_stock_code(code))
    items.extend(normalize_stock_code(item) for item in split_csv(codes))
    items = [item for item in items if item]
    if not items:
        raise HTTPException(status_code=400, detail="code 和 codes 至少需要传一个")
    return list(dict.fromkeys(items))


def require_board_codes(board_code: str, board_codes: str) -> list[str]:
    items = []
    if board_code:
        items.append(board_code.strip())
    items.extend(item.strip() for item in split_csv(board_codes))
    items = [item for item in items if item]
    if not items:
        raise HTTPException(status_code=400, detail="board_code 和 board_codes 至少需要传一个")
    return list(dict.fromkeys(items))


def require_index_codes(index_code: str, index_codes: str) -> list[str]:
    items = []
    if index_code:
        items.append(normalize_index_code(index_code))
    items.extend(normalize_index_code(item) for item in split_csv(index_codes))
    items = [item for item in items if item]
    if not items:
        raise HTTPException(status_code=400, detail="index_code 和 index_codes 至少需要传一个")
    return list(dict.fromkeys(items))


def require_report_type(report_type: str) -> str:
    actual = report_type or "income_statement"
    if actual not in {"income_statement", "balance_sheet", "cash_flow_statement"}:
        raise HTTPException(status_code=400, detail="report_type 不合法")
    return actual


def require_quote_freq(freq: str) -> str:
    actual = freq or "1d"
    if actual not in {"tick", "1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mo"}:
        raise HTTPException(status_code=400, detail="freq 不合法")
    return actual


def require_board_quote_freq(freq: str) -> str:
    actual = freq or "1d"
    if actual not in {"1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mo"}:
        raise HTTPException(status_code=400, detail="freq 不合法")
    return actual


def require_index_quote_freq(freq: str) -> str:
    actual = freq or "1d"
    if actual not in {"1d", "1w", "1mo"}:
        raise HTTPException(status_code=400, detail="freq 不合法")
    return actual


def require_adjust(adjust: str) -> str:
    actual = adjust or "none"
    if actual not in {"none", "qfq", "hfq"}:
        raise HTTPException(status_code=400, detail="adjust 不合法")
    return actual


def require_money_flow_view(view: str) -> str:
    actual = view or "summary"
    if actual not in {"summary", "trend", "breakdown"}:
        raise HTTPException(status_code=400, detail="view 不合法")
    return actual


def require_board_rank_type(rank_type: str) -> str:
    actual = rank_type or "change_pct"
    if actual not in {"change_pct", "turnover_rate", "net_inflow"}:
        raise HTTPException(status_code=400, detail="rank_type 不合法")
    return actual


def require_board_money_flow_scope(scope: str) -> str:
    actual = scope or "board"
    if actual not in {"board", "industry"}:
        raise HTTPException(status_code=400, detail="scope 涓嶅悎娉?")
    return actual


def require_limit_type(limit_type: str) -> str:
    actual = limit_type or "limit_up"
    if actual not in {"limit_up", "limit_down", "opened_limit"}:
        raise HTTPException(status_code=400, detail="limit_type 涓嶅悎娉?")
    return actual


def require_exchange(exchange: str) -> str:
    actual = exchange or "SSE"
    if actual not in {"SSE", "SZSE", "BSE", "HKEX"}:
        raise HTTPException(status_code=400, detail="exchange 涓嶅悎娉?")
    return actual


def merge_model_lists[T: BaseModel](high_priority: Sequence[T], low_priority: Sequence[T], key_fields: tuple[str, ...]) -> list[T]:
    merged: list[T] = []
    index_map: dict[tuple[object, ...], int] = {}
    for item in high_priority:
        key = tuple(getattr(item, field) for field in key_fields)
        index_map[key] = len(merged)
        merged.append(item.model_copy(deep=True))
    for item in low_priority:
        key = tuple(getattr(item, field) for field in key_fields)
        if key not in index_map:
            index_map[key] = len(merged)
            merged.append(item.model_copy(deep=True))
            continue
        current = merged[index_map[key]]
        payload = current.model_dump()
        for field_name, value in item.model_dump().items():
            if field_name in key_fields:
                continue
            if payload[field_name] in {None, ""} and value not in {None, ""}:
                payload[field_name] = value
        merged[index_map[key]] = type(current)(**payload)
    return merged


def sort_stock_quotes(items: list[StockQuoteItem]) -> list[StockQuoteItem]:
    return sorted(items, key=lambda item: (item.code, item.trade_time))


def sort_board_quotes(items: list[BoardQuoteItem]) -> list[BoardQuoteItem]:
    return sorted(items, key=lambda item: (item.board_code, item.trade_time))


def sort_index_quotes(items: list[IndexQuoteItem]) -> list[IndexQuoteItem]:
    return sorted(items, key=lambda item: (item.index_code, item.trade_time))


def sort_board_rankings(items: list[BoardRankingItem]) -> list[BoardRankingItem]:
    return sorted(items, key=lambda item: (item.rank or 10**9, item.board_code))


def trim_stock_quote_count(items: list[StockQuoteItem], count: int | None) -> list[StockQuoteItem]:
    if not count:
        return items
    grouped: dict[str, list[StockQuoteItem]] = {}
    for item in items:
        grouped.setdefault(item.code, []).append(item)
    trimmed: list[StockQuoteItem] = []
    for code, group_items in grouped.items():
        trimmed.extend(sorted(group_items, key=lambda item: item.trade_time)[-count:])
    return trimmed


def trim_board_quote_count(items: list[BoardQuoteItem], count: int | None) -> list[BoardQuoteItem]:
    if not count:
        return items
    grouped: dict[str, list[BoardQuoteItem]] = {}
    for item in items:
        grouped.setdefault(item.board_code, []).append(item)
    trimmed: list[BoardQuoteItem] = []
    for board_code, group_items in grouped.items():
        trimmed.extend(sorted(group_items, key=lambda item: item.trade_time)[-count:])
    return trimmed


def trim_index_quote_count(items: list[IndexQuoteItem], count: int | None) -> list[IndexQuoteItem]:
    if not count:
        return items
    grouped: dict[str, list[IndexQuoteItem]] = {}
    for item in items:
        grouped.setdefault(item.index_code, []).append(item)
    trimmed: list[IndexQuoteItem] = []
    for index_code, group_items in grouped.items():
        trimmed.extend(sorted(group_items, key=lambda item: item.trade_time)[-count:])
    return trimmed


def build_missing_expected_date_ranges(expected_dates: list[str], existing_dates: set[str]) -> list[tuple[str, str]]:
    if expected_dates == []:
        return []
    missing_ranges: list[tuple[str, str]] = []
    current_start = ""
    current_end = ""
    for expected_date in expected_dates:
        if expected_date in existing_dates:
            if current_start != "":
                missing_ranges.append((current_start, current_end))
                current_start = ""
                current_end = ""
            continue
        if current_start == "":
            current_start = expected_date
        current_end = expected_date
    if current_start != "":
        missing_ranges.append((current_start, current_end))
    return missing_ranges


def has_enough_stock_quote_rows(items: list[StockQuoteItem], codes: list[str], count: int | None) -> bool:
    if not count:
        return False
    counter = {code: 0 for code in codes}
    for item in items:
        counter[item.code] = counter.get(item.code, 0) + 1
    return all(value >= count for value in counter.values())


def filter_response_fields[T: BaseModel](items: Sequence[T], fields: str, allowed_fields: set[str]) -> list[dict[str, object]]:
    selected = split_csv(fields)
    if not selected:
        return [item.model_dump() for item in items]
    invalid_fields = [field for field in selected if field not in allowed_fields]
    if invalid_fields:
        raise HTTPException(status_code=400, detail=f"fields 含有不支持的字段: {', '.join(invalid_fields)}")
    return [{field: getattr(item, field) for field in selected} for item in items]

