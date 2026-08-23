from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from services import exports


router = APIRouter()


@router.get("/api/exports/stock_daily_1d/resolve/{market_data_version}")
async def resolve_stock_daily_export(market_data_version: str) -> dict[str, str]:
    return exports.resolve_market_version(market_data_version)


@router.get("/api/exports/stock_daily_1d/{dataset_version}/manifest")
async def stock_daily_export_manifest(dataset_version: str, request: Request) -> Response:
    return exports.manifest_response(dataset_version, request)


@router.get("/api/exports/stock_daily_1d/{dataset_version}/files/{relative_path:path}")
async def stock_daily_export_file(dataset_version: str, relative_path: str, request: Request) -> Response:
    return exports.file_response(dataset_version, relative_path, request)
