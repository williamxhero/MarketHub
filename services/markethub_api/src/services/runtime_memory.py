from __future__ import annotations

from collections.abc import Callable
import logging
import os
import time
from typing import TypeVar


_RESULT = TypeVar("_RESULT")
_LOGGER = logging.getLogger("uvicorn.error")


def process_rss_mb() -> float:
    page_count = _read_process_rss_pages()
    if page_count == 0:
        return 0.0
    return page_count * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024


def run_with_memory_log(label: str, detail: dict[str, object], action: Callable[[], _RESULT]) -> _RESULT:
    started_at = time.monotonic()
    start_rss_mb = process_rss_mb()
    _LOGGER.info("memory_start label=%s rss_mb=%.1f %s", label, start_rss_mb, _format_detail(detail))
    try:
        return action()
    finally:
        finish_rss_mb = process_rss_mb()
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        _LOGGER.info(
            "memory_finish label=%s rss_mb=%.1f rss_delta_mb=%.1f elapsed_ms=%s %s",
            label,
            finish_rss_mb,
            finish_rss_mb - start_rss_mb,
            elapsed_ms,
            _format_detail(detail),
        )


def _read_process_rss_pages() -> int:
    statm_path = "/proc/self/statm"
    if not os.path.exists(statm_path):
        return 0
    values = open(statm_path, encoding="utf-8").read().split()
    if len(values) < 2:
        return 0
    return int(values[1])


def _format_detail(detail: dict[str, object]) -> str:
    return " ".join(f"{key}={value}" for key, value in detail.items())
