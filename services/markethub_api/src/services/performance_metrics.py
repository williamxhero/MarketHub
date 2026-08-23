from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import threading
import time
from typing import Awaitable, Callable, MutableMapping

from services.runtime_memory import process_rss_mb
from services.request_timing import begin_request_timing, end_request_timing


_MAX_ROUTES = 512
_MAX_SAMPLES_PER_ROUTE = 2_048


@dataclass
class _RouteMetrics:
    request_count: int = 0
    error_count: int = 0
    in_flight: int = 0
    wire_bytes: int = 0
    streaming_count: int = 0
    response_complete_ms: deque[float] = field(default_factory=lambda: deque(maxlen=_MAX_SAMPLES_PER_ROUTE))
    app_return_ms: deque[float] = field(default_factory=lambda: deque(maxlen=_MAX_SAMPLES_PER_ROUTE))
    first_body_ms: deque[float] = field(default_factory=lambda: deque(maxlen=_MAX_SAMPLES_PER_ROUTE))
    stages_ms: dict[str, deque[float]] = field(default_factory=dict)


class PerformanceMetrics:
    def __init__(self, max_routes: int = _MAX_ROUTES) -> None:
        self._max_routes = max_routes
        self._routes: dict[tuple[str, str], _RouteMetrics] = {}
        self._lock = threading.Lock()

    def start(self, method: str, route: str) -> None:
        with self._lock:
            metrics = self._get_or_create(method, route)
            metrics.request_count += 1
            metrics.in_flight += 1

    def finish(
        self,
        method: str,
        route: str,
        *,
        status_code: int,
        wire_bytes: int,
        response_complete_ms: float,
        app_return_ms: float,
        first_body_ms: float | None,
        streaming: bool,
        stages_ms: dict[str, float] | None = None,
    ) -> None:
        with self._lock:
            metrics = self._get_or_create(method, route)
            metrics.in_flight = max(0, metrics.in_flight - 1)
            metrics.wire_bytes += max(0, wire_bytes)
            metrics.response_complete_ms.append(max(0.0, response_complete_ms))
            metrics.app_return_ms.append(max(0.0, app_return_ms))
            if first_body_ms is not None:
                metrics.first_body_ms.append(max(0.0, first_body_ms))
            if status_code >= 400:
                metrics.error_count += 1
            if streaming:
                metrics.streaming_count += 1
            for stage, duration in (stages_ms or {}).items():
                samples = metrics.stages_ms.setdefault(stage, deque(maxlen=_MAX_SAMPLES_PER_ROUTE))
                samples.append(max(0.0, duration))

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            routes = [
                _route_snapshot(method, route, metrics)
                for (method, route), metrics in sorted(self._routes.items())
            ]
        return {
            "route_count": len(routes),
            "sample_limit_per_route": _MAX_SAMPLES_PER_ROUTE,
            "rss_mb": round(process_rss_mb(), 3),
            "routes": routes,
        }

    def _get_or_create(self, method: str, route: str) -> _RouteMetrics:
        key = (method, route)
        metrics = self._routes.get(key)
        if metrics is not None:
            return metrics
        if len(self._routes) >= self._max_routes:
            key = ("__OTHER__", "__overflow__")
        return self._routes.setdefault(key, _RouteMetrics())


def _route_snapshot(method: str, route: str, metrics: _RouteMetrics) -> dict[str, object]:
    durations = sorted(metrics.response_complete_ms)
    app_return = sorted(metrics.app_return_ms)
    first_body = sorted(metrics.first_body_ms)
    return {
        "method": method,
        "route": route,
        "request_count": metrics.request_count,
        "error_count": metrics.error_count,
        "in_flight": metrics.in_flight,
        "wire_bytes": metrics.wire_bytes,
        "streaming_count": metrics.streaming_count,
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "p99_ms": _percentile(durations, 0.99),
        "max_ms": round(durations[-1], 3) if durations else 0.0,
        "app_return_p50_ms": _percentile(app_return, 0.50),
        "app_return_p95_ms": _percentile(app_return, 0.95),
        "app_return_p99_ms": _percentile(app_return, 0.99),
        "first_body_p50_ms": _percentile(first_body, 0.50),
        "first_body_p95_ms": _percentile(first_body, 0.95),
        "first_body_p99_ms": _percentile(first_body, 0.99),
        "stages": {
            stage: _sample_snapshot(samples)
            for stage, samples in sorted(metrics.stages_ms.items())
        },
    }


def _sample_snapshot(samples: deque[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "p50_ms": _percentile(ordered, 0.50),
        "p95_ms": _percentile(ordered, 0.95),
        "p99_ms": _percentile(ordered, 0.99),
        "max_ms": round(ordered[-1], 3) if ordered else 0.0,
    }


def _percentile(samples: list[float], quantile: float) -> float:
    if not samples:
        return 0.0
    index = max(0, math.ceil(len(samples) * quantile) - 1)
    return round(samples[index], 3)


class PerformanceMetricsMiddleware:
    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        metrics: PerformanceMetrics,
        route_resolver: Callable[[MutableMapping[str, object]], str],
    ) -> None:
        self._app = app
        self._metrics = metrics
        self._route_resolver = route_resolver

    async def __call__(self, scope: MutableMapping[str, object], receive: Callable[..., object], send: Callable[..., object]) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        route = self._route_resolver(scope)
        started_at = time.monotonic()
        status_code = 500
        wire_bytes = 0
        first_body_ms: float | None = None
        streaming = False
        completed = False
        response_completed_at: float | None = None
        self._metrics.start(method, route)
        request_timing, timing_token = begin_request_timing()

        async def metrics_send(message: MutableMapping[str, object]) -> None:
            nonlocal status_code, wire_bytes, first_body_ms, streaming, completed, response_completed_at
            message_type = message.get("type")
            if message_type == "http.response.start":
                status_code = int(message.get("status", 500))
                headers = list(message.get("headers", []))
                app_duration_ms = (time.monotonic() - started_at) * 1_000
                headers = _append_server_timing(headers, app_duration_ms, request_timing.snapshot())
                message["headers"] = headers
            elif message_type == "http.response.body":
                body = message.get("body", b"")
                if isinstance(body, bytes):
                    wire_bytes += len(body)
                    if first_body_ms is None and body:
                        first_body_ms = (time.monotonic() - started_at) * 1_000
                more_body = bool(message.get("more_body", False))
                streaming = streaming or more_body
                if not more_body:
                    completed = True
                    response_completed_at = time.monotonic()
            await send(message)

        try:
            await self._app(scope, receive, metrics_send)
        finally:
            app_returned_at = time.monotonic()
            response_complete_ms = ((response_completed_at or app_returned_at) - started_at) * 1_000
            self._metrics.finish(
                method,
                route,
                status_code=status_code,
                wire_bytes=wire_bytes,
                response_complete_ms=response_complete_ms,
                app_return_ms=(app_returned_at - started_at) * 1_000,
                first_body_ms=first_body_ms,
                streaming=streaming or not completed,
                stages_ms=request_timing.snapshot(),
            )
            end_request_timing(timing_token)


def _append_server_timing(
    headers: list[tuple[bytes, bytes]],
    duration_ms: float,
    stages_ms: dict[str, float],
) -> list[tuple[bytes, bytes]]:
    values = [f"app;dur={duration_ms:.3f}"]
    values.extend(f"{stage};dur={value:.3f}" for stage, value in sorted(stages_ms.items()))
    timing_value = ", ".join(values).encode("ascii")
    for index, (name, value) in enumerate(headers):
        if name.lower() == b"server-timing":
            headers[index] = (name, value + b", " + timing_value)
            break
    else:
        headers.append((b"server-timing", timing_value))
    return headers
