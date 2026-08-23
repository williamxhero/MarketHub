from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

from services.versioned_response_cache import CacheValue, VersionedResponseCache


def test_cache_is_byte_and_entry_bounded_lru() -> None:
    cache: VersionedResponseCache[bytes] = VersionedResponseCache(max_bytes=5, max_entries=2)
    cache.get_or_build("a", lambda: CacheValue(b"aa", 2))
    cache.get_or_build("b", lambda: CacheValue(b"bb", 2))
    assert cache.get_or_build("a", lambda: CacheValue(b"bad", 3)) == (b"aa", True)
    cache.get_or_build("c", lambda: CacheValue(b"ccc", 3))
    assert cache.snapshot() == {"entries": 2, "bytes": 5, "max_entries": 2, "max_bytes": 5, "inflight": 0}
    assert cache.get_or_build("b", lambda: CacheValue(b"new", 3)) == (b"new", False)


def test_cache_singleflights_same_key_without_holding_global_build_lock() -> None:
    cache: VersionedResponseCache[bytes] = VersionedResponseCache(max_bytes=100, max_entries=10)
    started = threading.Event()
    release = threading.Event()
    builds = 0

    def build() -> CacheValue[bytes]:
        nonlocal builds
        builds += 1
        started.set()
        assert release.wait(2)
        return CacheValue(b"value", 5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(cache.get_or_build, "same", build)
        assert started.wait(1)
        second = executor.submit(cache.get_or_build, "same", build)
        release.set()
        assert first.result()[0] == second.result()[0] == b"value"
    assert builds == 1


def test_failed_build_wakes_waiters_and_is_not_cached() -> None:
    cache: VersionedResponseCache[bytes] = VersionedResponseCache(max_bytes=100, max_entries=10)
    try:
        cache.get_or_build("bad", lambda: (_ for _ in ()).throw(RuntimeError("failed")))
    except RuntimeError:
        pass
    assert cache.snapshot()["inflight"] == 0
    assert cache.get_or_build("bad", lambda: CacheValue(b"ok", 2)) == (b"ok", False)
