from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import anyio
import anyio.to_thread


DATA_ROUTE_THREAD_TOKENS = 64
_RESULT = TypeVar("_RESULT")

# 数据接口统一走独立限流器，避免和 FastAPI/Starlette 默认线程池互相抢占。
DATA_ROUTE_LIMITER = anyio.CapacityLimiter(DATA_ROUTE_THREAD_TOKENS)


async def run_data_task(func: Callable[..., _RESULT], *args: object) -> _RESULT:
    return await anyio.to_thread.run_sync(func, *args, limiter=DATA_ROUTE_LIMITER)


def get_data_thread_pool_metrics() -> dict[str, int]:
    return {
        "total_tokens": DATA_ROUTE_LIMITER.total_tokens,
        "borrowed_tokens": DATA_ROUTE_LIMITER.borrowed_tokens,
        "available_tokens": DATA_ROUTE_LIMITER.available_tokens,
    }
