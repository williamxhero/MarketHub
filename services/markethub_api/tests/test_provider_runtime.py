from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from runtime_paths import configure_python_path


configure_python_path()

from quotemux.infra.provider_runtime import core as provider_runtime


def _wait_until(checker, timeout_seconds: float = 1.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if checker():
            return
        time.sleep(0.01)
    raise AssertionError("等待条件超时")


def test_queued_gm_request_does_not_hold_global_provider_gate(monkeypatch) -> None:
    gm_gate = provider_runtime.ProviderGate("gm", provider_runtime.ProviderPolicy(1, 0.0, 0, 0.3))
    db_gate = provider_runtime.ProviderGate("datalake_db", provider_runtime.ProviderPolicy(1, 0.0, 0, 0.3))
    gm_release = threading.Event()
    gm_started = threading.Event()
    queued_finished = threading.Event()
    monkeypatch.setattr(provider_runtime, "_GLOBAL_GATE", threading.BoundedSemaphore(2))
    monkeypatch.setattr(provider_runtime, "_GATES", {"gm": gm_gate, "datalake_db": db_gate})

    def _blocking_gm() -> str:
        gm_started.set()
        assert gm_release.wait(timeout=1.0)
        return "gm"

    def _run_blocking_gm() -> None:
        provider_runtime.call_provider_api("gm", "history", _blocking_gm)

    def _run_queued_gm() -> None:
        try:
            provider_runtime.call_provider_api("gm", "history", lambda: "queued")
        except TimeoutError:
            pass
        finally:
            queued_finished.set()

    blocking_thread = threading.Thread(target=_run_blocking_gm)
    queued_thread = threading.Thread(target=_run_queued_gm)
    blocking_thread.start()
    assert gm_started.wait(timeout=1.0)
    queued_thread.start()
    _wait_until(lambda: gm_gate.snapshot()["queued"] == 1)

    start = time.monotonic()
    result = provider_runtime.call_provider_api("datalake_db", "query_dataframe", lambda: "db")
    elapsed = time.monotonic() - start

    gm_release.set()
    blocking_thread.join(timeout=1.0)
    queued_thread.join(timeout=1.0)

    assert result == "db"
    assert elapsed < 0.15
    assert queued_finished.is_set()


def test_busy_gm_slot_fails_fast_without_holding_data_path(monkeypatch) -> None:
    gm_gate = provider_runtime.ProviderGate("gm", provider_runtime.ProviderPolicy(1, 0.0, 0, 0.0))
    gm_release = threading.Event()
    gm_started = threading.Event()
    monkeypatch.setattr(provider_runtime, "_GLOBAL_GATE", threading.BoundedSemaphore(1))
    monkeypatch.setattr(provider_runtime, "_GATES", {"gm": gm_gate})

    def _blocking_gm() -> str:
        gm_started.set()
        assert gm_release.wait(timeout=1.0)
        return "gm"

    blocking_thread = threading.Thread(target=lambda: provider_runtime.call_provider_api("gm", "history", _blocking_gm))
    blocking_thread.start()
    assert gm_started.wait(timeout=1.0)

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        provider_runtime.call_provider_api("gm", "history", lambda: "queued")
    elapsed = time.monotonic() - start

    gm_release.set()
    blocking_thread.join(timeout=1.0)

    assert elapsed < 0.05
