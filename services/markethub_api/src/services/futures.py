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


def _partial_error(status: int, code: str, message: str, exc: ValueError) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "details": {"reason": str(exc)}})


def _partial_query_error(exc: ValueError, *, coverage: bool) -> HTTPException:
    """Translate QuoteMux's typed public-read errors without owning their semantics."""
    # Import lazily so ordinary MarketHub imports remain compatible until the
    # co-deployed QuoteMux release supplies the partial-publication reader.
    from quotemux.public_reader import FuturesPartialPublicationStaleError

    if isinstance(exc, FuturesPartialPublicationStaleError):
        return _partial_error(409, "PARTIAL_PUBLICATION_STALE_OR_INVALID", "期货 partial publication 身份已过期或不可用", exc)
    return _partial_error(
        400,
        "PARTIAL_COVERAGE_BAD_QUERY" if coverage else "PARTIAL_PUBLICATION_BAD_QUERY",
        "partial coverage 查询或分页游标无效" if coverage else "期货 partial publication 查询或分页游标无效",
        exc,
    )


def get_quotes_1m_partial(
    dataset_id: str, dataset_version: str, partial_completeness_revision: str, generation_pin: str,
    codes: str, start_time: str, end_time: str, limit: int, cursor: str = "",
) -> tuple[list[dict[str, object]], str]:
    """Delegate the entire partial contract to QuoteMux's public reader."""
    if dataset_id != "future_1m_partial_s000012_quotemux":
        raise _partial_error(409, "PARTIAL_PUBLICATION_STALE_OR_INVALID", "期货 partial publication 身份已过期或不可用", ValueError("unknown dataset_id"))
    try:
        batch, next_cursor = _PUBLIC_READER.read_futures_1m_partial_page(
            codes, start_time, end_time,
            qmp_id=dataset_version,
            qmc_id=partial_completeness_revision,
            qmg_id=generation_pin,
            cursor=cursor or None,
            limit=limit,
        )
    except ValueError as exc:
        raise _partial_query_error(exc, coverage=False) from exc
    return list(batch.as_dicts()), next_cursor or ""


def get_quotes_1m_partial_coverage(
    dataset_id: str, dataset_version: str, partial_completeness_revision: str, generation_pin: str,
    codes: str, start_time: str, end_time: str, limit: int, cursor: str = "",
) -> tuple[list[dict[str, object]], str]:
    """QuoteMux owns interval identity, clipping, cursor order, and residuals."""
    if dataset_id != "future_1m_partial_s000012_quotemux":
        raise _partial_error(409, "PARTIAL_PUBLICATION_STALE_OR_INVALID", "期货 partial publication 身份已过期或不可用", ValueError("unknown dataset_id"))
    try:
        batch, next_cursor = _PUBLIC_READER.read_futures_1m_partial_coverage_page(
            codes, start_time, end_time,
            qmp_id=dataset_version,
            qmc_id=partial_completeness_revision,
            qmg_id=generation_pin,
            cursor=cursor or None,
            limit=limit,
        )
    except ValueError as exc:
        raise _partial_query_error(exc, coverage=True) from exc
    return list(batch.as_dicts()), next_cursor or ""


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
