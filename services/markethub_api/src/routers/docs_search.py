from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse


router = APIRouter(include_in_schema=False)


def _redirect_to_openapi() -> RedirectResponse:
    return RedirectResponse("/api/openapi", status_code=307)


@router.get("/docs")
@router.get("/docs/")
@router.get("/docs/search")
@router.get("/docs/{doc_path:path}")
@router.get("/doc-view")
@router.get("/doc-view/")
@router.get("/doc-view/search")
@router.get("/doc-view/search/")
@router.get("/doc-view/{doc_path:path}")
async def legacy_docs_redirect() -> RedirectResponse:
    return _redirect_to_openapi()
