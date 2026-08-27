from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from threading import Event, Lock

from fastapi import HTTPException
import pytest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from data_threads import run_data_task, run_futures_partial_task
from quotemux.strict_read import is_strict_public_read, reject_in_strict_public_read
from services.dataset_versions import DATASET_IDS, ROUTE_DATASET_DEPENDENCIES


def test_public_worker_enters_strict_read_boundary_and_maps_violation() -> None:
    async def exercise() -> None:
        assert await run_data_task(is_strict_public_read) is True
        with pytest.raises(HTTPException) as error:
            await run_data_task(reject_in_strict_public_read, "provider:test")
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "DATA_INCOMPLETE"

    asyncio.run(exercise())


def test_futures_partial_worker_serializes_shared_reader_calls() -> None:
    first_started = Event()
    release = Event()
    state_lock = Lock()
    started: list[int] = []
    active = 0
    max_active = 0

    def reader(index: int) -> int:
        nonlocal active, max_active
        with state_lock:
            started.append(index)
            active += 1
            max_active = max(max_active, active)
        first_started.set()
        release.wait()
        with state_lock:
            active -= 1
        return index

    async def exercise() -> None:
        tasks = [asyncio.create_task(run_futures_partial_task(reader, index)) for index in range(3)]
        for _ in range(100):
            if first_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert first_started.is_set()
        await asyncio.sleep(0.05)
        assert started == [0]
        assert max_active == 1

        release.set()
        assert await asyncio.gather(*tasks) == [0, 1, 2]
        assert max_active == 1

    asyncio.run(exercise())


def test_futures_partial_worker_releases_token_after_exception() -> None:
    def fails() -> None:
        raise RuntimeError("reader failed")

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="reader failed"):
            await run_futures_partial_task(fails)
        assert await run_futures_partial_task(lambda: "next reader") == "next reader"

    asyncio.run(exercise())


def test_versioned_route_registry_only_references_registered_datasets() -> None:
    assert "/api/stocks/quotes/query:1m" in ROUTE_DATASET_DEPENDENCIES
    assert "/api/stocks/quotes/daily-window/query" in ROUTE_DATASET_DEPENDENCIES
    assert all(dataset in DATASET_IDS for datasets in ROUTE_DATASET_DEPENDENCIES.values() for dataset in datasets)
