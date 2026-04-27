from __future__ import annotations

from quotemux import IndexMembersRequest, IndexQuotesRequest, QuoteMux
from quotemux.models import IndexCatalogItem, IndexMemberItem, IndexQuoteItem
from services.common import ensure_limit, require_index_codes, require_index_quote_freq


_QUOTEMUX = QuoteMux()


def get_catalog(category: str, market: str, publisher: str, status: str, limit: int, offset: int) -> list[IndexCatalogItem]:
    return _QUOTEMUX.indexes.get_catalog(category, market, publisher, status, ensure_limit(limit), offset)


def get_profile(index_code: str) -> IndexCatalogItem | None:
    return _QUOTEMUX.indexes.get_profile(index_code)


def get_quotes(
    index_code: str,
    index_codes: str,
    freq: str,
    trade_date: str,
    start_date: str,
    end_date: str,
    count: int | None,
    limit: int,
) -> list[IndexQuoteItem]:
    return _QUOTEMUX.indexes.get_quotes(
        IndexQuotesRequest(
            index_codes=require_index_codes(index_code, index_codes),
            freq=require_index_quote_freq(freq),
            trade_date=trade_date,
            start_date=start_date,
            end_date=end_date,
            count=count,
            limit=ensure_limit(limit),
        )
    )


def get_members(index_code: str, trade_date: str) -> list[IndexMemberItem]:
    return _QUOTEMUX.indexes.get_members(IndexMembersRequest(index_code=index_code, trade_date=trade_date))
