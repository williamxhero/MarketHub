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


@pytest.mark.parametrize(
    ("series_type", "expected_capability"),
    (
        ("back_adjusted_continuous", "futures.quotes.back_adjusted_continuous.1m"),
        ("main_continuous", "futures.quotes.main_continuous.1m"),
    ),
)
def test_future_1m_repair_routes_by_explicit_series_type(monkeypatch, series_type: str, expected_capability: str) -> None:
    _configure(monkeypatch)

    result = admin_runtime.run_data_repair("future_bar_1m", "mhd-v1-test", {"series_type": series_type, "codes": ["ag"]})

    assert result["capability_id"] == expected_capability
    assert result["dataset_id"] == "future_bar_1m"


@pytest.mark.parametrize("scope", ({}, {"series_type": "continuous"}, {"series_type": None}))
def test_future_1m_repair_rejects_missing_or_unknown_series_type(scope: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="scope.series_type"):
        admin_runtime.run_data_repair("future_bar_1m", "mhd-v1-test", scope)
