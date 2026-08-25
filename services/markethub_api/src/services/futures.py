from __future__ import annotations

from platform_models import (
    FutureBar1mItem,
    FutureContractCatalogItem,
    FutureContractRealtimeQuoteItem,
    FutureMainContractMappingItem,
    FutureRealtimeQuoteItem,
)
from fastapi import HTTPException
from quotemux import QuoteMux, QuoteMuxPublicReader
from services.futures_1m_completeness import Futures1mCompletenessEvidence, validate_published_futures_1m_completeness
from services.futures_partial_publication import (
    PartialPublicationEvidence,
    PartialPublicationQueryError,
    PartialPublicationStaleError,
    read_futures_1m_partial_coverage_page,
    read_futures_1m_partial_page,
    validate_futures_1m_partial_publication,
)


_QUOTEMUX = QuoteMux()
_PUBLIC_READER = QuoteMuxPublicReader()


def get_quotes_1m_with_evidence(
    codes: str, series_type: str, start_time: str, end_time: str, limit: int,
    dataset_version: str = "", completeness_revision: str = "",
) -> tuple[list[FutureBar1mItem], Futures1mCompletenessEvidence]:
    evidence = validate_published_futures_1m_completeness(
        codes, series_type, start_time, end_time, dataset_version, completeness_revision,
    )
    items = [
        FutureBar1mItem.model_validate({
            **row,
            "bar_time": row["bar_time"].strftime("%Y-%m-%d %H:%M:%S")
            if hasattr(row["bar_time"], "strftime")
            else str(row["bar_time"]),
        })
        for row in _PUBLIC_READER.get_futures_quotes_1m_batch(
            codes,
            series_type,
            start_time,
            end_time,
            limit=limit,
        ).as_dicts()
    ]
    return items, evidence


def get_quotes_1m(
    codes: str, series_type: str, start_time: str, end_time: str, limit: int,
    dataset_version: str = "", completeness_revision: str = "",
) -> list[FutureBar1mItem]:
    return get_quotes_1m_with_evidence(
        codes, series_type, start_time, end_time, limit, dataset_version, completeness_revision,
    )[0]


def get_quotes_1m_partial(
    dataset_id: str, dataset_version: str, partial_completeness_revision: str, generation_pin: str,
    codes: str, start_time: str, end_time: str, limit: int, cursor: str = "",
) -> tuple[list[dict[str, object]], PartialPublicationEvidence, str]:
    """Source-specific partial read; distinct from the strict legacy endpoint."""
    try:
        evidence = validate_futures_1m_partial_publication(
            dataset_id, dataset_version, partial_completeness_revision, generation_pin, codes, start_time, end_time,
            include_intervals=False,
        )
        items, next_cursor = read_futures_1m_partial_page(evidence, codes, start_time, end_time, limit, cursor)
    except PartialPublicationQueryError as exc:
        raise HTTPException(status_code=400, detail={"code": "PARTIAL_PUBLICATION_BAD_QUERY", "message": "期货 partial publication 查询或分页游标无效", "details": {"reason": str(exc)}}) from exc
    except PartialPublicationStaleError as exc:
        raise HTTPException(status_code=409, detail={"code": "PARTIAL_PUBLICATION_STALE_OR_INVALID", "message": "期货 partial publication 身份或分页游标无效", "details": {"reason": str(exc)}}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "PARTIAL_PUBLICATION_BAD_QUERY", "message": "期货 partial publication 查询无效", "details": {"reason": str(exc)}}) from exc
    # A publication may never be attributed to a source generation that changed
    # while its page was being read.
    try:
        validate_futures_1m_partial_publication(
            dataset_id, dataset_version, partial_completeness_revision, generation_pin, codes, start_time, end_time,
            include_intervals=False,
        )
    except PartialPublicationStaleError as exc:
        raise HTTPException(status_code=409, detail={"code": "PARTIAL_PUBLICATION_STALE_OR_INVALID", "message": "期货 partial publication 已变化", "details": {"reason": str(exc)}}) from exc
    return items, evidence, next_cursor


def get_quotes_1m_partial_coverage(
    dataset_id: str, dataset_version: str, partial_completeness_revision: str, generation_pin: str,
    codes: str, start_time: str, end_time: str, limit: int, cursor: str = "",
) -> tuple[list[dict[str, object]], PartialPublicationEvidence, str, dict[str, object]]:
    try:
        evidence = validate_futures_1m_partial_publication(
            dataset_id, dataset_version, partial_completeness_revision, generation_pin, codes, start_time, end_time,
            include_intervals=False,
        )
        page, next_cursor, summary = read_futures_1m_partial_coverage_page(evidence, codes, start_time, end_time, limit, cursor)
    except PartialPublicationQueryError as exc:
        raise HTTPException(status_code=400, detail={"code": "PARTIAL_COVERAGE_BAD_QUERY", "message": "partial coverage 查询或游标无效", "details": {"reason": str(exc)}}) from exc
    except PartialPublicationStaleError as exc:
        raise HTTPException(status_code=409, detail={"code": "PARTIAL_PUBLICATION_STALE", "message": "partial publication identity 已过期", "details": {"reason": str(exc)}}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "PARTIAL_COVERAGE_BAD_QUERY", "message": "partial coverage 查询无效", "details": {"reason": str(exc)}}) from exc
    return page, evidence, next_cursor, summary


def get_main_continuous_realtime(codes: str) -> list[FutureRealtimeQuoteItem]:
    return _QUOTEMUX.futures.get_main_continuous_realtime(codes)


def get_contract_catalog(codes: str, include_expired: bool, dataset_version: str) -> list[FutureContractCatalogItem]:
    """Read the immutable QuoteMux catalog snapshot; this path has no provider or writer."""
    items = _QUOTEMUX.futures.get_contract_catalog(codes, include_expired)
    return [item.model_copy(update={"catalog_dataset_version": dataset_version}) for item in items]


def get_main_contract_mappings(codes: str) -> list[FutureMainContractMappingItem]:
    return _QUOTEMUX.futures.get_main_contract_mappings(codes)


def get_contract_realtime(symbols: str) -> list[FutureContractRealtimeQuoteItem]:
    return _QUOTEMUX.futures.get_contract_realtime(symbols)


def list_coverage(series_type: str) -> list[dict[str, object]]:
    return list(_PUBLIC_READER.list_futures_coverage_batch(series_type).as_dicts())


def update_main_continuous(overlap_days: int) -> dict[str, object]:
    return _QUOTEMUX.futures.update_main_continuous(overlap_days)
