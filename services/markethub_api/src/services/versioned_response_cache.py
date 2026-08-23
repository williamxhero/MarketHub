from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import os
import threading
from typing import Callable, Generic, TypeVar


_VALUE = TypeVar("_VALUE")


@dataclass(frozen=True)
class CacheValue(Generic[_VALUE]):
    value: _VALUE
    size_bytes: int


class VersionedResponseCache(Generic[_VALUE]):
    def __init__(self, *, max_bytes: int | None = None, max_entries: int | None = None) -> None:
        self.max_bytes = max_bytes if max_bytes is not None else int(os.getenv("MARKETHUB_RESPONSE_CACHE_MAX_BYTES", str(128 * 1024**2)))
        self.max_entries = max_entries if max_entries is not None else int(os.getenv("MARKETHUB_RESPONSE_CACHE_MAX_ENTRIES", "256"))
        self._entries: OrderedDict[str, CacheValue[_VALUE]] = OrderedDict()
        self._inflight: dict[str, threading.Event] = {}
        self._bytes = 0
        self._lock = threading.Lock()

    def get_or_build(self, key: str, builder: Callable[[], CacheValue[_VALUE]]) -> tuple[_VALUE, bool]:
        while True:
            with self._lock:
                cached = self._entries.get(key)
                if cached is not None:
                    self._entries.move_to_end(key)
                    return cached.value, True
                event = self._inflight.get(key)
                if event is None:
                    event = threading.Event()
                    self._inflight[key] = event
                    creator = True
                else:
                    creator = False
            if creator:
                break
            event.wait()
        try:
            built = builder()
            if built.size_bytes < 0:
                raise ValueError("cache value size must be non-negative")
            with self._lock:
                if built.size_bytes <= self.max_bytes and self.max_entries > 0:
                    existing = self._entries.pop(key, None)
                    if existing is not None:
                        self._bytes -= existing.size_bytes
                    self._entries[key] = built
                    self._bytes += built.size_bytes
                    self._evict_locked()
            return built.value, False
        finally:
            with self._lock:
                finished = self._inflight.pop(key, None)
                if finished is not None:
                    finished.set()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "bytes": self._bytes,
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
                "inflight": len(self._inflight),
            }

    def _evict_locked(self) -> None:
        while self._entries and (len(self._entries) > self.max_entries or self._bytes > self.max_bytes):
            _, evicted = self._entries.popitem(last=False)
            self._bytes -= evicted.size_bytes
