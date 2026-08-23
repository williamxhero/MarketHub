from __future__ import annotations

from platform_models import (
    FutureBar1mItem,
    FutureContractCatalogItem,
    FutureContractRealtimeQuoteItem,
    FutureMainContractMappingItem,
    FutureRealtimeQuoteItem,
)
from quotemux import QuoteMux, QuoteMuxPublicReader


_QUOTEMUX = QuoteMux()
_PUBLIC_READER = QuoteMuxPublicReader()


def get_quotes_1m(codes: str, series_type: str, start_time: str, end_time: str, limit: int) -> list[FutureBar1mItem]:
    return _QUOTEMUX.futures.get_quotes_1m(codes, series_type, start_time, end_time, limit)


def get_main_continuous_realtime(codes: str) -> list[FutureRealtimeQuoteItem]:
    return _QUOTEMUX.futures.get_main_continuous_realtime(codes)


def get_contract_catalog(codes: str, include_expired: bool) -> list[FutureContractCatalogItem]:
    return _QUOTEMUX.futures.get_contract_catalog(codes, include_expired)


def get_main_contract_mappings(codes: str) -> list[FutureMainContractMappingItem]:
    return _QUOTEMUX.futures.get_main_contract_mappings(codes)


def get_contract_realtime(symbols: str) -> list[FutureContractRealtimeQuoteItem]:
    return _QUOTEMUX.futures.get_contract_realtime(symbols)


def list_coverage(series_type: str) -> list[dict[str, object]]:
    return list(_PUBLIC_READER.list_futures_coverage_batch(series_type).as_dicts())


def update_main_continuous(overlap_days: int) -> dict[str, object]:
    return _QUOTEMUX.futures.update_main_continuous(overlap_days)
