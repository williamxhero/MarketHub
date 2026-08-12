from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Awaitable, TypeVar

import anyio
import anyio.to_thread


DATA_ROUTE_THREAD_TOKENS = 64
QUOTE_ROUTE_THREAD_TOKENS = 6
QUOTE_ROUTE_POLL_SECONDS = 0.2
_RESULT = TypeVar("_RESULT")

# 数据接口统一走独立限流器，避免和 FastAPI/Starlette 默认线程池互相抢占。
DATA_ROUTE_LIMITER = anyio.CapacityLimiter(DATA_ROUTE_THREAD_TOKENS)
# 行情接口可能触发长区间补洞和大批量查询，必须和其它数据接口隔离。
QUOTE_ROUTE_LIMITER = anyio.CapacityLimiter(QUOTE_ROUTE_THREAD_TOKENS)
_QUOTE_TASKS: set[asyncio.Task[object]] = set()


class QuoteClientDisconnectedError(RuntimeError):
    """客户端已断开行情请求。"""


async def run_data_task(func: Callable[..., _RESULT], *args: object) -> _RESULT:
    return await anyio.to_thread.run_sync(func, *args, limiter=DATA_ROUTE_LIMITER)


async def run_quote_task(
    func: Callable[..., _RESULT],
    *args: object,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> _RESULT:
    """执行行情任务，并在客户端断开后及时结束请求协程。

    同步行情读取不能安全地强杀线程；断开后的底层读取仍占用原执行槽直到自然结束。
    新请求在执行槽上排队，避免批量调用方因瞬时满载收到 429；尚未开始的排队任务在客户端断开时取消。
    """
    async def execute() -> _RESULT:
        return await anyio.to_thread.run_sync(func, *args, limiter=QUOTE_ROUTE_LIMITER, abandon_on_cancel=True)

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
