from __future__ import annotations

from fastapi import HTTPException

from quotemux import EtfDailyQuotesRequest, QuoteMux
from quotemux.models import EtfCatalogItem, EtfDailyQuotesQueryResult
from quotemux.requests.etfs import normalize_etf_ts_code


_QUOTEMUX = QuoteMux()


def _parse_ts_codes(value: str) -> list[str]:
    raw_items = [item.strip() for item in value.split(",") if item.strip() != ""]
    items = [normalize_etf_ts_code(item) for item in raw_items]
    if any(item == "" for item in items):
        raise HTTPException(status_code=422, detail="ETF 代码必须是带交易所后缀的 ts_code，例如 510300.SH 或 159915.SZ")
    return list(dict.fromkeys(items))


def get_catalog(ts_codes: str, name: str, include_delisted: bool, limit: int, offset: int) -> list[EtfCatalogItem]:
    return _QUOTEMUX.etfs.get_catalog(_parse_ts_codes(ts_codes), name, include_delisted, limit, offset)


def get_daily_quotes(ts_codes: str, trade_date: str, start_date: str, end_date: str, limit: int | None, meta_detail: str) -> EtfDailyQuotesQueryResult:
    actual_codes = _parse_ts_codes(ts_codes)
    if actual_codes == []:
        raise HTTPException(status_code=422, detail="ts_codes 至少包含一个 ETF ts_code")
    if meta_detail not in {"summary", "full"}:
        raise HTTPException(status_code=422, detail="meta_detail 只能是 summary 或 full")
    return _QUOTEMUX.etfs.get_daily_quotes(
        EtfDailyQuotesRequest(
            ts_codes=actual_codes,
            trade_date=trade_date,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            meta_detail=meta_detail,
        )
    )
