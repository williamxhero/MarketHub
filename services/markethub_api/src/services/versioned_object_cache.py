from __future__ import annotations

import json
from typing import Callable, TypeVar

from services.versioned_response_cache import CacheValue, VersionedResponseCache


_VALUE = TypeVar("_VALUE")
_CACHE: VersionedResponseCache[object] = VersionedResponseCache()


def get_or_build(key: str, builder: Callable[[], _VALUE]) -> tuple[_VALUE, bool]:
    def build_value() -> CacheValue[object]:
        value = builder()
        if hasattr(value, "model_dump"):
            serializable = value.model_dump()
        elif isinstance(value, (list, tuple)):
            serializable = [item.model_dump() if hasattr(item, "model_dump") else item for item in value]
        else:
            serializable = value
        size = len(json.dumps(serializable, ensure_ascii=False, default=str, separators=(",", ":")).encode())
        return CacheValue(value=value, size_bytes=size)

    value, hit = _CACHE.get_or_build(key, build_value)
    return value, hit  # type: ignore[return-value]


def snapshot() -> dict[str, int]:
    return _CACHE.snapshot()


def clear() -> None:
    _CACHE.clear()
