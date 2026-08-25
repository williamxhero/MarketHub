from __future__ import annotations

import pytest

from services import admin_runtime


class _CaptureAdmin:
    def run_capture(self, capability_id: str) -> dict[str, object]:
        return {"capability_id": capability_id, "status": "success"}

    def run_due_captures(self) -> tuple[dict[str, object], ...]:
        return (
            {"capability_id": "stocks.quotes.daily", "status": "success"},
            {"capability_id": "stocks.quotes.intraday", "status": "success"},
        )

    def run_repair(self, capability_id: str, scope: dict[str, object], dataset_version: str) -> dict[str, object]:
        return {"id": 7, "capability_id": capability_id, "status": "success"}


def _configure(monkeypatch) -> list[bool]:
    calls: list[bool] = []
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", _CaptureAdmin())
    monkeypatch.setattr(admin_runtime, "run_with_memory_log", lambda _name, _detail, operation: operation())
    monkeypatch.setattr(admin_runtime, "finalize_stock_1m_daily_coverage_state", lambda: calls.append(True))
    return calls


def test_intraday_capture_finalizes_current_coverage_version(monkeypatch) -> None:
    calls = _configure(monkeypatch)

    admin_runtime.run_capture("stocks.quotes.intraday")

    assert calls == [True]


def test_due_capture_finalizes_intraday_once(monkeypatch) -> None:
    calls = _configure(monkeypatch)

    admin_runtime.run_due_captures()

    assert calls == [True]


def test_intraday_repair_finalizes_current_coverage_version(monkeypatch) -> None:
    calls = _configure(monkeypatch)

    result = admin_runtime.run_data_repair("stock_bar_1m", "mhd-v1-test", {"codes": ["600000"]})

    assert result["repair_task_id"] == 7
    assert calls == [True]


def test_non_intraday_capture_does_not_finalize_minute_coverage(monkeypatch) -> None:
    calls = _configure(monkeypatch)

    admin_runtime.run_capture("stocks.quotes.daily")

    assert calls == []


def test_future_1m_repair_routes_only_a_managed_registry_id(monkeypatch) -> None:
    _configure(monkeypatch)

    result = admin_runtime.run_data_repair("future_bar_1m", "mhd-v1-test", {"repair_registry_id": "ag-repair-001"})

    assert result["capability_id"] == "futures.quotes.back_adjusted_continuous.1m"
    assert result["dataset_id"] == "future_bar_1m"


@pytest.mark.parametrize("scope", ({}, {"series_type": "main_continuous"}, {"repair_registry_id": "x", "artifact_path": "/tmp/x"}, {"repair_registry_id": ""}))
def test_future_1m_repair_rejects_any_unmanaged_scope(scope: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="repair_registry_id"):
        admin_runtime.run_data_repair("future_bar_1m", "mhd-v1-test", scope)


def test_capture_admin_wires_managed_evidence_and_registry_guard(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Job:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    class Admin:
        def __init__(self, *, job) -> None:
            captured["job"] = job

    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", None)
    monkeypatch.setattr(admin_runtime, "QuoteMuxCaptureJob", Job)
    monkeypatch.setattr(admin_runtime, "QuoteMuxCaptureAdmin", Admin)
    monkeypatch.setattr(admin_runtime, "require_current", lambda _capability, version: version)

    admin_runtime._capture_admin()

    assert captured["back_adjusted_repair_evidence"].__class__.__name__ == "ManagedBackAdjustedRepairEvidenceRegistry"
    assert captured["dataset_version_guard"].require_current("futures.quotes.back_adjusted_continuous.1m", "mhd-v1-test") is not None
