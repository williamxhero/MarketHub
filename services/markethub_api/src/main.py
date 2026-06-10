from __future__ import annotations

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
    from fastapi.exceptions import RequestValidationError
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
except ImportError as exc:
    raise RuntimeError("请先执行: pip install -r requirements.dev.txt") from exc

from datetime import datetime
import os
from pathlib import Path

import anyio.to_thread

from core.config import HOST, PORT, STATIC_FAVICON_PATH, STATIC_INDEX_PATH
from data_threads import get_data_thread_pool_metrics
from quotemux.config_runtime.validation import ConfigValidationError
from quotemux.models import ApiError
from quotemux.infra.db.availability import get_fact_ref_availability
from quotemux.infra.db.client import close_pool, get_pool_metrics
from quotemux.runtime_core.audit import read_fallback_summary
from quotemux.runtime_core.health import get_provider_metrics
from routers.admin import router as admin_router
from routers.boards import router as boards_router
from routers.docs_search import router as docs_router
from routers.indexes import router as indexes_router
from routers.markets import router as markets_router
from routers.news import router as news_router
from routers.rankings import router as rankings_router
from routers.stocks import router as stocks_router
from search_engine import ensure_index


SYNC_ROUTE_THREAD_TOKENS = 100
PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONSOLE_ROOT = PROJECT_ROOT / "services" / "markethub_console"
CONSOLE_DIST_ROOT = CONSOLE_ROOT / "dist"
CONSOLE_WEB_ROOT = CONSOLE_ROOT / "web"


def _console_index_path() -> Path:
    dist_index = CONSOLE_DIST_ROOT / "index.html"
    if dist_index.is_file():
        return dist_index
    return CONSOLE_WEB_ROOT / "index.html"


def _get_cors_origins() -> list[str]:
    configured = os.getenv("MARKETHUB_CORS_ORIGINS", "")
    values = ["http://127.0.0.1:8803", "http://localhost:8803"]
    if configured != "":
        values.extend(configured.split(","))
    origins: list[str] = []
    for value in values:
        origin = value.strip()
        if origin != "" and origin not in origins:
            origins.append(origin)
    return origins


app = FastAPI(
    title="整合 API 服务",
    version="0.2.0",
    docs_url="/api/openapi",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_cors_origins(),
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)
app.include_router(stocks_router)
app.include_router(boards_router)
app.include_router(indexes_router)
app.include_router(markets_router)
app.include_router(news_router)
app.include_router(rankings_router)
app.include_router(docs_router)
app.include_router(admin_router)

if (CONSOLE_DIST_ROOT / "assets").exists():
    app.mount("/admin/assets", StaticFiles(directory=str(CONSOLE_DIST_ROOT / "assets")), name="admin-assets")


def _format_validation_error_message(exc: RequestValidationError) -> tuple[str, str]:
    # 统一把 FastAPI 的字段校验错误压平成可直接返回给下游的文本。
    messages: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", []) if part != "body")
        message = str(error.get("msg", "参数不合法"))
        if location == "":
            messages.append(message)
            continue
        messages.append(f"{location}: {message}")
    if messages == []:
        return "请求参数校验失败", ""
    return messages[0], " | ".join(messages[1:])


def get_sync_thread_pool_metrics() -> dict[str, int]:
    limiter = anyio.to_thread.current_default_thread_limiter()
    return {
        "total_tokens": limiter.total_tokens,
        "borrowed_tokens": limiter.borrowed_tokens,
        "available_tokens": limiter.available_tokens,
    }


@app.on_event("startup")
async def on_startup() -> None:
    # MarketHub 绝大多数接口仍是同步路由，需要给共享线程池留出余量，避免探活和文档入口被一起饿死。
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = SYNC_ROUTE_THREAD_TOKENS
    ensure_index()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    close_pool()


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        code = str(exc.detail.get("code", f"HTTP_{exc.status_code}"))
        message = str(exc.detail.get("message", "请求失败"))
        details = str(exc.detail.get("details", ""))
        payload = ApiError(code=code, message=message, details=details)
        return JSONResponse(status_code=exc.status_code, content=payload.model_dump())
    detail = exc.detail if isinstance(exc.detail, str) else "请求失败"
    payload = ApiError(code=f"HTTP_{exc.status_code}", message=detail)
    return JSONResponse(status_code=exc.status_code, content=payload.model_dump())


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    message, details = _format_validation_error_message(exc)
    payload = ApiError(code="HTTP_422", message=message, details=details)
    return JSONResponse(status_code=422, content=payload.model_dump())


@app.exception_handler(ConfigValidationError)
async def config_validation_exception_handler(_: Request, exc: ConfigValidationError) -> JSONResponse:
    details = " | ".join(f"{issue.field}: {issue.message}" for issue in exc.issues)
    payload = ApiError(code="VALIDATION_FAILED", message="运行时配置校验失败", details=details)
    return JSONResponse(status_code=422, content=payload.model_dump())


@app.exception_handler(ValueError)
async def value_error_exception_handler(_: Request, exc: ValueError) -> JSONResponse:
    payload = ApiError(code="VALIDATION_FAILED", message=str(exc))
    return JSONResponse(status_code=422, content=payload.model_dump())


@app.exception_handler(KeyError)
async def key_error_exception_handler(_: Request, exc: KeyError) -> JSONResponse:
    payload = ApiError(code="UNKNOWN_RESOURCE", message=str(exc).strip("'"))
    return JSONResponse(status_code=404, content=payload.model_dump())


@app.exception_handler(Exception)
async def unexpected_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    payload = ApiError(code="INTERNAL_ERROR", message="服务内部错误", details=str(exc))
    return JSONResponse(status_code=500, content=payload.model_dump())


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(STATIC_INDEX_PATH)


@app.get("/favicon.ico")
async def favicon() -> FileResponse:
    return FileResponse(STATIC_FAVICON_PATH, media_type="image/svg+xml")


@app.get("/console")
async def console() -> RedirectResponse:
    return RedirectResponse("/admin")


@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/", response_class=HTMLResponse)
async def admin_console() -> HTMLResponse:
    return HTMLResponse(_console_index_path().read_text(encoding="utf-8"))


@app.get("/admin-console", response_class=HTMLResponse)
@app.get("/admin-console/", response_class=HTMLResponse)
async def admin_console_alias() -> HTMLResponse:
    return await admin_console()


@app.get("/api/console/config")
async def console_config() -> dict[str, str]:
    return {
        "admin_api_base_url": os.getenv("MARKETHUB_ADMIN_API_BASE_URL", ""),
    }


@app.get("/api/health")
async def health() -> dict[str, str]:
    # 健康检查只做轻量存活探针，避免把索引构建耗时耦合进监控。
    return {
        "service": "integration_api",
        "status": "ok",
        "version": app.version,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/diagnostics/connections")
async def connection_diagnostics() -> dict[str, object]:
    return {
        "provider_runtime": get_provider_metrics(),
        "fallback_runtime": read_fallback_summary(),
        "store_db_pool": get_pool_metrics(),
        "data_thread_pool": get_data_thread_pool_metrics(),
        "sync_thread_pool": get_sync_thread_pool_metrics(),
    }


@app.get("/api/diagnostics/fact-ref")
async def fact_ref_diagnostics() -> dict[str, object]:
    return get_fact_ref_availability()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)


