from __future__ import annotations

from typing import Annotated, TypeAlias

from fastapi import APIRouter, Body, HTTPException

from data_threads import run_data_task
from platform_models.migration_contracts import EtfProfileRequest, ExpressEventsRequest, ForecastEventsRequest, IndexMembersAuditRequest
from platform_models.p0_fundamentals import CapitalP0Request, CompanyP0Request, ReportDisclosuresP0Request, StatementsP0Request
from platform_models.provider_contracts import AuditedPage
from quotemux.p0_fundamentals.errors import P0QueryError
from services import p0_fundamentals


router = APIRouter()

QuoteMuxQueryRequest: TypeAlias = Annotated[
    CompanyP0Request | CapitalP0Request | ReportDisclosuresP0Request | StatementsP0Request | ForecastEventsRequest
    | ExpressEventsRequest | EtfProfileRequest | IndexMembersAuditRequest,
    Body(discriminator="capability_id"),
]

_HTTP_STATUS_BY_ERROR = {"rate_limit_error": 429, "permission_error": 403, "contract_error": 422, "cache_error": 500, "database_error": 500}


@router.post("/api/quotemux/p0/query", summary="查询 QuoteMux 强类型 Provider contract")
async def api_p0_query(request: QuoteMuxQueryRequest) -> AuditedPage:
    try:
        return await run_data_task(p0_fundamentals.query, request)
    except P0QueryError as exc:
        raise HTTPException(
            status_code=_HTTP_STATUS_BY_ERROR.get(exc.kind, 502),
            detail={"code": exc.kind, "message": str(exc), "details": ""},
        ) from exc
