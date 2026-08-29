from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPT_DIR = Path(__file__).resolve().parent


def test_global_update_keeps_async_default_for_non_health_schedules() -> None:
    source = (SCRIPT_DIR / "global-data-update.sh").read_text(encoding="utf-8")

    assert 'MARKETHUB_CAPTURE_ENDPOINT="${MARKETHUB_CAPTURE_ENDPOINT:-/api/admin/capture/run-due-async}"' in source
    assert '-X POST "$MARKETHUB_BASE_URL$MARKETHUB_CAPTURE_ENDPOINT"' in source


def test_health_gated_update_waits_for_due_capture_and_serializes_runs() -> None:
    source = (SCRIPT_DIR / "global-data-update-with-health.sh").read_text(encoding="utf-8")

    assert 'MARKETHUB_HEALTH_CAPTURE_ENDPOINT="${MARKETHUB_HEALTH_CAPTURE_ENDPOINT:-/api/admin/capture/run-due-async}"' in source
    assert 'MARKETHUB_GLOBAL_UPDATE_REQUIRED_CAPABILITIES="${MARKETHUB_GLOBAL_UPDATE_REQUIRED_CAPABILITIES:-stocks.quotes.daily}"' in source
    assert 'MARKETHUB_REQUIRED_CAPTURE_CAPABILITIES="$MARKETHUB_GLOBAL_UPDATE_REQUIRED_CAPABILITIES"' in source
    assert 'flock -w "$MARKETHUB_GLOBAL_UPDATE_LOCK_TIMEOUT_SECONDS"' in source
    assert "未启动重复采集或发布" in source
    assert "global_update_outcome=skipped reason=lock_busy retry_semantics=next_timer" in source


def test_global_update_bounds_due_enqueue_and_waits_only_for_declared_dependencies() -> None:
    source = (SCRIPT_DIR / "global-data-update.sh").read_text(encoding="utf-8")

    assert 'MARKETHUB_REQUIRED_CAPTURE_CAPABILITIES="${MARKETHUB_REQUIRED_CAPTURE_CAPABILITIES:-}"' in source
    assert 'MARKETHUB_REQUIRED_CAPTURE_TIMEOUT_SECONDS:-3600' in source
    assert 'MARKETHUB_CAPTURE_TIMEOUT_SECONDS:-60' in source
    assert 'capture_event=required_started capability_id=$capability_id' in source
    assert 'capture_event=required_failed capability_id=$capability_id reason=$reason' in source
    assert 'capture_event=due_enqueue_started endpoint=$MARKETHUB_CAPTURE_ENDPOINT' in source
    assert 'capture_event=due_enqueue_failed endpoint=$MARKETHUB_CAPTURE_ENDPOINT reason=$reason' in source
    assert '[ "$status" -eq 28 ] && reason="timeout"' in source


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("flock") is None, reason="requires bash and flock")
def test_health_gated_update_does_not_run_duplicate_pipeline(tmp_path: Path) -> None:
    log_path = tmp_path / "pipeline.log"
    capture_started = tmp_path / "capture-started"
    capture = tmp_path / "capture.sh"
    health = tmp_path / "health.sh"
    publisher = tmp_path / "publisher.py"
    capture.write_text(f'#!/usr/bin/env bash\necho capture >> "{log_path}"\ntouch "{capture_started}"\nsleep 1\n', encoding="utf-8")
    health.write_text(f'#!/usr/bin/env bash\necho health >> "{log_path}"\n', encoding="utf-8")
    publisher.write_text(f'from pathlib import Path\nPath(r"{log_path}").open("a").write("publish\\n")\n', encoding="utf-8")
    for script in (capture, health):
        script.chmod(0o755)
    environment = dict(os.environ)
    environment.update({
        "MARKETHUB_GLOBAL_DATA_UPDATE_SCRIPT": str(capture),
        "MARKETHUB_DATA_HEALTH_SCRIPT": str(health),
        "MARKETHUB_PARQUET_PUBLISHER_SCRIPT": str(publisher),
        "MARKETHUB_PYTHON": sys.executable,
        "MARKETHUB_CODE_ROOT": str(tmp_path),
        "MARKETHUB_ENABLE_DAILY_PARQUET_PUBLISH": "1",
        "MARKETHUB_GLOBAL_UPDATE_LOCK_PATH": str(tmp_path / "global-update.lock"),
        "MARKETHUB_GLOBAL_UPDATE_LOCK_TIMEOUT_SECONDS": "0",
    })
    first = subprocess.Popen(["bash", str(SCRIPT_DIR / "global-data-update-with-health.sh")], env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        if capture_started.exists():
            break
        __import__("time").sleep(0.01)
    assert capture_started.exists()
    second = subprocess.run(["bash", str(SCRIPT_DIR / "global-data-update-with-health.sh")], env=environment, capture_output=True, text=True, check=False)
    first_output, _ = first.communicate(timeout=10)
    assert first.returncode == 0, first_output
    assert second.returncode == 0
    assert "未启动重复采集或发布" in second.stdout
    assert "global_update_outcome=skipped reason=lock_busy retry_semantics=next_timer" in second.stdout
    assert log_path.read_text(encoding="utf-8").splitlines() == ["capture", "health", "publish"]
