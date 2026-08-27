from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Awaitable, TypeVar

import anyio
import anyio.to_thread
from fastapi import HTTPException
from quotemux import StrictReadViolation, strict_public_read_boundary
import time
import os

from services.request_timing import record_stage_ms


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


DATA_ROUTE_THREAD_TOKENS = _positive_int_env("MHK_DATA_ROUTE_TOKENS", 64)
QUOTE_ROUTE_THREAD_TOKENS = _positive_int_env("MHK_QUOTE_ROUTE_TOKENS", 6)
FUTURES_PARTIAL_ROUTE_THREAD_TOKENS = _positive_int_env("MHK_FUTURES_PARTIAL_ROUTE_TOKENS", 1)
QUOTE_ROUTE_POLL_SECONDS = 0.2
_RESULT = TypeVar("_RESULT")

# 数据接口统一走独立限流器，避免和 FastAPI/Starlette 默认线程池互相抢占。
DATA_ROUTE_LIMITER = anyio.CapacityLimiter(DATA_ROUTE_THREAD_TOKENS)
# 行情接口可能触发长区间补洞和大批量查询，必须和其它数据接口隔离。
QUOTE_ROUTE_LIMITER = anyio.CapacityLimiter(QUOTE_ROUTE_THREAD_TOKENS)
# Partial bars 和 coverage 共用 QuoteMux public reader/DB pool。同步 psycopg
# 查询不能在 HTTP 客户端断开后可靠取消，因此超额请求必须排队而不能开始新的读取。
FUTURES_PARTIAL_ROUTE_LIMITER = anyio.CapacityLimiter(FUTURES_PARTIAL_ROUTE_THREAD_TOKENS)
_QUOTE_TASKS: set[asyncio.Task[object]] = set()


class QuoteClientDisconnectedError(RuntimeError):
    """客户端已断开行情请求。"""


def _strict_public_call(func: Callable[..., _RESULT], args: tuple[object, ...]) -> _RESULT:
    try:
        with strict_public_read_boundary():
            return func(*args)
    except StrictReadViolation as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DATA_INCOMPLETE",
                "message": "本地数据不足；普通查询禁止调用 provider、安装依赖或写库",
                "details": {"blocked_operation": exc.operation, "repair_endpoint": "/api/admin/data-repairs"},
            },
        ) from exc


async def run_data_task(func: Callable[..., _RESULT], *args: object) -> _RESULT:
    queued_at = time.monotonic()

    def execute() -> _RESULT:
        record_stage_ms("queue", (time.monotonic() - queued_at) * 1_000)
        return _strict_public_call(func, args)

    return await anyio.to_thread.run_sync(execute, limiter=DATA_ROUTE_LIMITER)


async def run_futures_partial_task(func: Callable[..., _RESULT], *args: object) -> _RESULT:
    """Run a partial bars or coverage read within the shared DB-read budget."""
    queued_at = time.monotonic()

    def execute() -> _RESULT:
        record_stage_ms("queue", (time.monotonic() - queued_at) * 1_000)
        return _strict_public_call(func, args)

    return await anyio.to_thread.run_sync(execute, limiter=FUTURES_PARTIAL_ROUTE_LIMITER)


async def run_quote_task(
    func: Callable[..., _RESULT],
    *args: object,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> _RESULT:
    """执行行情任务，并在客户端断开后及时结束请求协程。

    同步行情读取不能安全地强杀线程；断开后的底层读取仍占用原执行槽直到自然结束。
    新请求在执行槽上排队，避免批量调用方因瞬时满载收到 429；尚未开始的排队任务在客户端断开时取消。
    """
    queued_at = time.monotonic()

    async def execute() -> _RESULT:
        def invoke() -> _RESULT:
            record_stage_ms("queue", (time.monotonic() - queued_at) * 1_000)
            return _strict_public_call(func, args)

        return await anyio.to_thread.run_sync(invoke, limiter=QUOTE_ROUTE_LIMITER, abandon_on_cancel=True)

    task: asyncio.Task[_RESULT] = asyncio.create_task(execute())
    _QUOTE_TASKS.add(task)
    task.add_done_callback(_QUOTE_TASKS.discard)
    while not task.done():
        done, _ = await asyncio.wait((task,), timeout=QUOTE_ROUTE_POLL_SECONDS)
        if task in done:
            break
        if is_disconnected is not None and await is_disconnected():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise QuoteClientDisconnectedError("客户端已断开行情查询")
    return task.result()


def _limiter_metrics(limiter: anyio.CapacityLimiter) -> dict[str, int]:
    return {
        "total_tokens": limiter.total_tokens,
        "borrowed_tokens": limiter.borrowed_tokens,
        "available_tokens": limiter.available_tokens,
    }


def get_data_thread_pool_metrics() -> dict[str, int]:
    return _limiter_metrics(DATA_ROUTE_LIMITER)


def get_quote_thread_pool_metrics() -> dict[str, int]:
    return {**_limiter_metrics(QUOTE_ROUTE_LIMITER), "active_tasks": len(_QUOTE_TASKS)}
