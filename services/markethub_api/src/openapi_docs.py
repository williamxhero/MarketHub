from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


TAG_BY_PREFIX = {
    "/api/stocks": "股票",
    "/api/boards": "板块",
    "/api/concepts": "概念",
    "/api/indexes": "指数",
    "/api/markets": "市场",
    "/api/rankings": "排行",
    "/api/admin": "管理",
    "/api/diagnostics": "诊断",
    "/api/console": "管理",
    "/api/health": "系统",
}


def install_openapi_schema(app: FastAPI) -> None:
    cached_schema: dict[str, Any] = {}

    def custom_openapi() -> dict[str, Any]:
        if cached_schema:
            return cached_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        _enrich_schema(schema)
        cached_schema.update(schema)
        return cached_schema

    app.openapi = custom_openapi


def _enrich_schema(schema: dict[str, Any]) -> None:
    paths = schema.get("paths", {})
    if not isinstance(paths, dict):
        return
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        tag = _tag_for_path(path)
        for method_name, operation in methods.items():
            if method_name not in {"get", "post", "put", "delete", "patch"} or not isinstance(operation, dict):
                continue
            operation["tags"] = [tag]
            if str(operation.get("summary", "")) == "":
                operation["summary"] = _build_summary(method_name, path)


def _tag_for_path(path: str) -> str:
    for prefix, tag in TAG_BY_PREFIX.items():
        if path.startswith(prefix):
            return tag
    return "接口"


def _build_summary(method_name: str, path: str) -> str:
    action = {
        "get": "查询",
        "post": "提交",
        "put": "更新",
        "delete": "删除",
        "patch": "修改",
    }.get(method_name, "调用")
    resource = path.removeprefix("/api/").replace("{", "").replace("}", "").replace("/", " ")
    return f"{action} {resource}"
