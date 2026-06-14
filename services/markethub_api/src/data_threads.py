from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import anyio
import anyio.to_thread


DATA_ROUTE_THREAD_TOKENS = 64
QUOTE_ROUTE_THREAD_TOKENS = 6
_RESULT = TypeVar("_RESULT")

# 数据接口统一走独立限流器，避免和 FastAPI/Starlette 默认线程池互相抢占。
DATA_ROUTE_LIMITER = anyio.CapacityLimiter(DATA_ROUTE_THREAD_TOKENS)
# 行情接口可能触发长区间补洞和大批量查询，必须和其它数据接口隔离。
QUOTE_ROUTE_LIMITER = anyio.CapacityLimiter(QUOTE_ROUTE_THREAD_TOKENS)


async def run_data_task(func: Callable[..., _RESULT], *args: object) -> _RESULT:
    return await anyio.to_thread.run_sync(func, *args, limiter=DATA_ROUTE_LIMITER)


async def run_quote_task(func: Callable[..., _RESULT], *args: object) -> _RESULT:
    return await anyio.to_thread.run_sync(func, *args, limiter=QUOTE_ROUTE_LIMITER)


def _limiter_metrics(limiter: anyio.CapacityLimiter) -> dict[str, int]:
    return {
        "total_tokens": limiter.total_tokens,
        "borrowed_tokens": limiter.borrowed_tokens,
        "available_tokens": limiter.available_tokens,
    }


def get_data_thread_pool_metrics() -> dict[str, int]:
    return _limiter_metrics(DATA_ROUTE_LIMITER)


def get_quote_thread_pool_metrics() -> dict[str, int]:
    return _limiter_metrics(QUOTE_ROUTE_LIMITER)
