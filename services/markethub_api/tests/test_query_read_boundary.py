from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from fastapi import HTTPException
import pytest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from data_threads import run_data_task
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


def test_versioned_route_registry_only_references_registered_datasets() -> None:
    assert "/api/stocks/quotes/query:1m" in ROUTE_DATASET_DEPENDENCIES
    assert "/api/stocks/quotes/daily-window/query" in ROUTE_DATASET_DEPENDENCIES
    assert all(dataset in DATASET_IDS for datasets in ROUTE_DATASET_DEPENDENCIES.values() for dataset in datasets)
