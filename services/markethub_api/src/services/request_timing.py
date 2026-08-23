from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import threading
from typing import Iterator


_ALLOWED_STAGES = frozenset(
    {
        "queue",
        "db_pool",
        "sql",
        "coverage",
        "row_mapping",
        "serialize",
        "compress",
    }
)


@dataclass
class RequestTiming:
    stages_ms: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, stage: str, duration_ms: float) -> None:
        if stage not in _ALLOWED_STAGES:
            raise ValueError(f"unsupported request timing stage: {stage}")
        with self._lock:
            self.stages_ms[stage] = self.stages_ms.get(stage, 0.0) + max(0.0, duration_ms)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self.stages_ms)


_CURRENT_TIMING: ContextVar[RequestTiming | None] = ContextVar("markethub_request_timing", default=None)


def begin_request_timing() -> tuple[RequestTiming, Token[RequestTiming | None]]:
    timing = RequestTiming()
    return timing, _CURRENT_TIMING.set(timing)


def end_request_timing(token: Token[RequestTiming | None]) -> None:
    _CURRENT_TIMING.reset(token)


def record_stage_ms(stage: str, duration_ms: float) -> None:
    timing = _CURRENT_TIMING.get()
    if timing is not None:
        timing.add(stage, duration_ms)


def current_stage_timings() -> dict[str, float]:
    timing = _CURRENT_TIMING.get()
    return timing.snapshot() if timing is not None else {}


@contextmanager
def request_timing_context() -> Iterator[RequestTiming]:
    timing, token = begin_request_timing()
    try:
        yield timing
    finally:
        end_request_timing(token)
