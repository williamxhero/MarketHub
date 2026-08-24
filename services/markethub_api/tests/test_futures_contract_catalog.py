from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from platform_models import FutureContractCatalogItem
from quotemux.futures import FutureContractCatalogIncompleteError
from routers import futures as futures_router
from services import admin_runtime, dataset_versions, futures


def _catalog_item() -> FutureContractCatalogItem:
    return FutureContractCatalogItem(
        provider_symbol="SHFE.rb2610",
        contract_symbol="SHFE.rb2610",
        product_code="rb",
        exchange="SHFE",
        snapshot_id="snapshot-1",
        captured_at="2026-08-24 10:00:00",
        source={"package_id": "shinny_tqsdk"},
        availability={"execution_profile_required": True},
        provenance={"execution": {"kind": "unavailable"}},
    )


def test_catalog_endpoint_pins_version_sets_header_and_uses_local_reader(monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(futures_router, "require_dataset_version", lambda *_args: "mhd-v1-catalog")
    monkeypatch.setattr(
        futures._QUOTEMUX.futures,
        "get_contract_catalog",
        lambda codes, include_expired: calls.append((codes, include_expired)) or [_catalog_item()],
    )
    monkeypatch.setattr(
        futures._QUOTEMUX.futures,
        "_tqsdk_handler",
        lambda *_args: (_ for _ in ()).throw(AssertionError("ordinary GET must not resolve a provider")),
    )

    response = Response()
    items = asyncio.run(
        futures_router.api_future_contract_catalog(
            response=response, codes="rb", include_expired=False, dataset_version="mhd-v1-catalog"
        )
    )

    assert response.headers["X-MarketHub-Dataset-Version"] == "mhd-v1-catalog"
    assert items[0].catalog_dataset_version == "mhd-v1-catalog"
    assert calls == [("rb", False)]


def test_catalog_endpoint_maps_local_snapshot_gap_to_data_incomplete(monkeypatch) -> None:
    monkeypatch.setattr(futures_router, "require_dataset_version", lambda *_args: "mhd-v1-catalog")
    monkeypatch.setattr(
        futures._QUOTEMUX.futures,
        "get_contract_catalog",
        lambda *_args: (_ for _ in ()).throw(
            FutureContractCatalogIncompleteError("missing_products", requested_codes=("rb",), missing_products=("rb",))
        ),
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            futures_router.api_future_contract_catalog(
                response=Response(), codes="rb", include_expired=False, dataset_version=""
            )
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "DATA_INCOMPLETE"
    assert error.value.detail["details"]["repair_endpoint"] == "/api/admin/data-repairs"


def test_catalog_repair_uses_canonical_empty_scope_and_finalizes_only_catalog(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object], str]] = []
    finalized: list[bool] = []

    class CaptureAdmin:
        def run_repair(self, capability_id: str, scope: dict[str, object], version: str) -> dict[str, object]:
            calls.append((capability_id, scope, version))
            return {"id": 19, "capability_id": capability_id, "status": "success"}

    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", CaptureAdmin())
    monkeypatch.setattr(admin_runtime, "require_dataset_version", lambda *_args: "mhd-v1-baseline")
    monkeypatch.setattr(admin_runtime, "run_with_memory_log", lambda _name, _detail, operation: operation())
    monkeypatch.setattr(
        admin_runtime,
        "finalize_future_contract_reference_state",
        lambda: finalized.append(True) or {
            "dataset_version": "mhd-v1-published", "snapshot_id": "snapshot-1", "row_count": 23,
            "product_count": 23, "checksum_sha256": "a" * 64, "complete": True,
        },
    )
    monkeypatch.setattr(
        admin_runtime,
        "finalize_stock_1m_daily_coverage_state",
        lambda: (_ for _ in ()).throw(AssertionError("catalog repair must not finalize future/stock bars")),
    )

    result = admin_runtime.run_data_repair("future_contract_reference", "", {})

    assert result["repair_task_id"] == 19
    assert calls == [("futures.contracts.catalog", {"codes": [], "include_expired": False}, "mhd-v1-baseline")]
    assert finalized == [True]
    assert result["dataset_version"] == "mhd-v1-published"
    assert result["publication"]["snapshot_id"] == "snapshot-1"


@pytest.mark.parametrize("scope", ({"codes": ["rb"]}, {"include_expired": "yes"}))
def test_catalog_repair_rejects_partial_or_invalid_scope(scope: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        admin_runtime._normalize_repair_scope("future_contract_reference", scope)


def test_catalog_repair_allows_expired_full_scope() -> None:
    assert admin_runtime._normalize_repair_scope(
        "future_contract_reference", {"codes": [], "include_expired": True}
    ) == {"codes": [], "include_expired": True}


def test_catalog_repair_status_reads_current_publication_evidence(monkeypatch) -> None:
    class CaptureAdmin:
        def get_repair_run(self, _task_id: int) -> dict[str, object]:
            return {"capability_id": "futures.contracts.catalog", "status": "success", "detail_json": {"publication": {"dataset_version": "mhd-v1-published", "snapshot_id": "snapshot-1", "complete": True}}}

    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", CaptureAdmin())
    result = admin_runtime.get_data_repair(19)

    assert result["dataset_version"] == "mhd-v1-published"
    assert result["publication"]["snapshot_id"] == "snapshot-1"


def test_catalog_incomplete_http_details_remain_a_json_object(monkeypatch) -> None:
    import main

    monkeypatch.setattr(futures_router, "require_dataset_version", lambda *_args: "mhd-v1-catalog")
    monkeypatch.setattr(
        futures._QUOTEMUX.futures,
        "get_contract_catalog",
        lambda *_args: (_ for _ in ()).throw(FutureContractCatalogIncompleteError("missing_products", missing_products=("rb",))),
    )
    monkeypatch.setattr(main.reader_packages, "ensure_reader_packages_ready", lambda: None)
    monkeypatch.setattr(main.adj_factor_warmup, "resume_tasks", lambda: None)
    monkeypatch.setattr(main.adj_factor_warmup, "stop_tasks", lambda: None)

    response = TestClient(main.app).get("/api/futures/contracts?codes=rb")

    assert response.status_code == 409
    assert isinstance(response.json()["details"], dict)
    assert response.json()["details"]["repair_template"]["scope"]["codes"] == []


def test_catalog_dataset_is_publication_gated_and_independent_from_future_bar() -> None:
    assert "future_contract_reference" in dataset_versions.DATASET_IDS
    assert "future_contract_reference" in dataset_versions.PUBLICATION_GATED_DATASET_IDS
    assert dataset_versions.ROUTE_DATASET_DEPENDENCIES["/api/futures/contracts"] == ("future_contract_reference",)
    assert dataset_versions.ROUTE_DATASET_DEPENDENCIES["/api/futures/coverage"] == ("future_bar_1m",)
