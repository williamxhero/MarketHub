from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from services.performance_metrics import PerformanceMetrics, PerformanceMetricsMiddleware
from services.request_timing import record_stage_ms


def _build_app(*, max_routes: int = 512) -> tuple[FastAPI, PerformanceMetrics]:
    app = FastAPI()
    metrics = PerformanceMetrics(max_routes=max_routes)

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        record_stage_ms("sql", 1.25)
        return {"item_id": item_id}

    @app.get("/failure")
    async def failure() -> None:
        raise HTTPException(status_code=409, detail="failed")

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def chunks():
            yield b"first"
            await asyncio.sleep(0)
            yield b"second"

        return StreamingResponse(chunks())

    def resolve(scope: dict[str, object]) -> str:
        path = str(scope.get("path", ""))
        if path.startswith("/items/"):
            return "/items/{item_id}"
        return path

    app.add_middleware(PerformanceMetricsMiddleware, metrics=metrics, route_resolver=resolve)
    return app, metrics


def test_metrics_normalize_routes_and_append_server_timing() -> None:
    app, metrics = _build_app()
    client = TestClient(app)

    first = client.get("/items/one")
    second = client.get("/items/two")

    assert first.status_code == 200
    assert "app;dur=" in first.headers["Server-Timing"]
    route = metrics.snapshot()["routes"][0]
    assert route["route"] == "/items/{item_id}"
    assert route["request_count"] == 2
    assert route["in_flight"] == 0
    assert route["wire_bytes"] > 0
    assert route["p95_ms"] >= route["p50_ms"]
    assert route["p99_ms"] >= route["p95_ms"]
    assert route["app_return_p95_ms"] >= route["p95_ms"]
    assert route["stages"]["sql"]["p50_ms"] == 1.25
    assert "sql;dur=1.250" in first.headers["Server-Timing"]


def test_metrics_count_error_and_stream_completion() -> None:
    app, metrics = _build_app()
    client = TestClient(app)

    assert client.get("/failure").status_code == 409
    streamed = client.get("/stream")

    snapshot = {entry["route"]: entry for entry in metrics.snapshot()["routes"]}
    assert snapshot["/failure"]["error_count"] == 1
    assert snapshot["/stream"]["streaming_count"] == 1
    assert snapshot["/stream"]["wire_bytes"] == len(b"firstsecond")
    assert snapshot["/stream"]["first_body_p50_ms"] > 0
    assert snapshot["/stream"]["p99_ms"] >= snapshot["/stream"]["first_body_p99_ms"]
    assert "app;dur=" in streamed.headers["Server-Timing"]


def test_metrics_bound_unknown_route_cardinality() -> None:
    app, metrics = _build_app(max_routes=1)
    client = TestClient(app)

    assert client.get("/items/one").status_code == 200
    assert client.get("/failure").status_code == 409

    snapshot = {entry["route"]: entry for entry in metrics.snapshot()["routes"]}
    assert set(snapshot) == {"/items/{item_id}", "__overflow__"}


def test_deploy_script_preserves_systemd_memory_guards() -> None:
    script = Path(__file__).resolve().parents[3] / "scripts" / "local" / "deploy_yosef_server.ps1"
    content = script.read_text(encoding="utf-8")

    for setting in ("MemoryHigh=12G", "MemoryMax=18G", "MemorySwapMax=2G", "OOMPolicy=stop"):
        assert setting in content
