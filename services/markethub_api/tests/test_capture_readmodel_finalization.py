from __future__ import annotations

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
