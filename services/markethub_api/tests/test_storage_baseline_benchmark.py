from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "benchmark" / "benchmark_storage_baseline.py"
SPEC = importlib.util.spec_from_file_location("benchmark_storage_baseline", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_percentile_and_summary_are_deterministic() -> None:
    runs = [
        {"elapsed_ms": float(index), "wire_bytes": 10, "result_sha256": "same"}
        for index in range(1, 21)
    ]
    summary = MODULE._summarize_runs(runs)

    assert summary["request_count"] == 20
    assert summary["p50_ms"] == 10.5
    assert summary["p95_ms"] == 19.0
    assert summary["max_ms"] == 20.0
    assert summary["wire_bytes"] == 200


def test_summary_rejects_result_drift() -> None:
    runs = [
        {"elapsed_ms": 1.0, "wire_bytes": 1, "result_sha256": "a"},
        {"elapsed_ms": 1.0, "wire_bytes": 1, "result_sha256": "b"},
    ]

    try:
        MODULE._summarize_runs(runs)
    except AssertionError as exc:
        assert "drifted" in str(exc)
    else:
        raise AssertionError("expected drift detection")


def test_remote_snapshot_arguments_reject_shell_metacharacters() -> None:
    try:
        MODULE._remote_snapshot("host", "eno1;id", "/safe/path")
    except ValueError:
        pass
    else:
        raise AssertionError("expected unsafe argument rejection")


def test_admin_only_scenario_does_not_require_daily_or_minute_corpus() -> None:
    scenarios = MODULE._scenario_requests(
        "mhf-v1-test",
        [],
        [],
        minute_date="2026-07-15",
        include_admin_async=True,
        selected_names={"admin_run_due_async"},
    )

    assert [scenario["name"] for scenario in scenarios if scenario["name"] == "admin_run_due_async"] == [
        "admin_run_due_async"
    ]
