from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import json

import pytest


SCRIPT_DIR = Path(__file__).resolve().parent


def test_global_update_retains_opt_in_async_due_endpoint() -> None:
    source = (SCRIPT_DIR / "global-data-update.sh").read_text(encoding="utf-8")

    assert 'MARKETHUB_CAPTURE_ENDPOINT="${MARKETHUB_CAPTURE_ENDPOINT:-/api/admin/capture/run-due-async}"' in source
    assert '-X POST "$MARKETHUB_BASE_URL$MARKETHUB_CAPTURE_ENDPOINT"' in source


def test_health_gated_update_waits_for_due_capture_and_serializes_runs() -> None:
    source = (SCRIPT_DIR / "global-data-update-with-health.sh").read_text(encoding="utf-8")

    assert 'MARKETHUB_HEALTH_CAPTURE_ENDPOINT="${MARKETHUB_HEALTH_CAPTURE_ENDPOINT:-/api/admin/capture/run-due-async}"' in source
    assert 'MARKETHUB_GLOBAL_UPDATE_REQUIRED_CAPABILITIES="${MARKETHUB_GLOBAL_UPDATE_REQUIRED_CAPABILITIES:-stocks.quotes.daily_snapshot}"' in source
    assert 'MARKETHUB_REQUIRED_CAPTURE_CAPABILITIES="$MARKETHUB_GLOBAL_UPDATE_REQUIRED_CAPABILITIES"' in source
    assert 'flock -w "$MARKETHUB_GLOBAL_UPDATE_LOCK_TIMEOUT_SECONDS"' in source
    assert "未启动重复采集或发布" in source
    assert "global_update_outcome=skipped reason=lock_busy retry_semantics=next_timer" in source
    assert "wait_for_intraday_capture_quiescence" not in source
    assert "MARKETHUB_INTRADAY_CAPTURE_QUIESCE_TIMEOUT_SECONDS" not in source


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None or shutil.which("flock") is None, reason="requires a native bash and flock")
def test_health_gated_update_does_not_wait_on_an_intraday_run(tmp_path: Path) -> None:
    environment, log_path, _ = _pipeline_harness(tmp_path, stock_daily_status="healthy", health_exit_code=0)
    fake_bin = Path(environment["PATH"].split(os.pathsep)[0])
    (fake_bin / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "url=\"\"; output=\"\"\n"
        "while [ $# -gt 0 ]; do case \"$1\" in -o) output=\"$2\"; shift 2;; -*) shift;; *) url=\"$1\"; shift;; esac; done\n"
        "if [[ \"$url\" == */api/health ]]; then printf '{}\\n'; exit 0; fi\n"
        "if [[ \"$url\" == *capability_id=stocks.quotes.intraday* ]]; then printf '%s\\n' '[{\"status\":\"running\"}]'; exit 0; fi\n"
        "if [[ \"$url\" == */api/admin/capture-runs* ]]; then printf '%s\\n' '[]'; exit 0; fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    (fake_bin / "curl").chmod(0o755)

    completed = subprocess.run(
        ["bash", str(SCRIPT_DIR / "global-data-update-with-health.sh")],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0, (completed.stdout or "") + (completed.stderr or "")
    assert log_path.read_text(encoding="utf-8").splitlines() == ["capture", "health", "publish"]


def test_intraday_script_repairs_calendar_before_acquiring_global_lock() -> None:
    source = (SCRIPT_DIR / "stock-intraday-capture-with-health.sh").read_text(encoding="utf-8")

    assert "calendar_preflight=started" in source
    assert "/api/admin/capture-runs/$MARKETHUB_CALENDAR_CAPTURE_CAPABILITY" in source
    assert source.index("calendar_preflight=started") < source.index('exec 9>"$MARKETHUB_LOCK_PATH"')


def test_stock_intraday_task_contract_targets_capture_script_after_close() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "reconcile_task_center.py"), "--task", "intraday", "--print"],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["task_id"] == "markethub_stock_intraday_capture"
    assert payload["schedule_type"] == "cron"
    assert payload["schedule_value"] == "15 20 * * 1-5"
    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["script_path"] == "/data/markethub/scripts/stock-intraday-capture-with-health.sh"


def test_health_alert_is_observable_without_failing_unrelated_capture() -> None:
    source = (SCRIPT_DIR / "global-data-update-with-health.sh").read_text(encoding="utf-8")

    assert 'MARKETHUB_DATA_HEALTH_FAILURE_POLICY="${MARKETHUB_DATA_HEALTH_FAILURE_POLICY:-warn}"' in source
    assert 'warn|fail)' in source
    assert 'global_update_health_outcome=alert policy=$MARKETHUB_DATA_HEALTH_FAILURE_POLICY' in source
    assert 'global_update_publication_outcome=deferred reason=declared_dependency_unhealthy' in source
    assert '[ "$MARKETHUB_DATA_HEALTH_FAILURE_POLICY" = "fail" ]' in source


def test_publication_is_gated_on_declared_dependencies_not_platform_aggregate() -> None:
    source = (SCRIPT_DIR / "global-data-update-with-health.sh").read_text(encoding="utf-8")

    assert 'PUBLICATION_HEALTH_GATE_SCRIPT="${MARKETHUB_PUBLICATION_HEALTH_GATE_SCRIPT:-$SCRIPT_DIR/publication_health_gate.py}"' in source
    assert 'MARKETHUB_PUBLICATION_HEALTH_DEPENDENCIES="${MARKETHUB_PUBLICATION_HEALTH_DEPENDENCIES:-' in source
    assert 'core_dataset_freshness:fact.stock_daily_1d' in source
    assert '--not-before "$health_started_at"' in source
    assert 'if ! evaluate_publication_health; then' in source
    assert 'global_update_publication_outcome=deferred reason=publication_health_gate_unusable' in source
    # The platform-wide alert must stay observable but must no longer gate the
    # publication, otherwise an unrelated durable alert defers it forever.
    assert "health_alert" not in source


def test_global_update_bounds_due_enqueue_and_waits_only_for_declared_dependencies() -> None:
    source = (SCRIPT_DIR / "global-data-update.sh").read_text(encoding="utf-8")

    assert 'MARKETHUB_REQUIRED_CAPTURE_CAPABILITIES="${MARKETHUB_REQUIRED_CAPTURE_CAPABILITIES:-}"' in source
    assert 'MARKETHUB_ENABLE_ASYNC_DUE_CAPTURE="${MARKETHUB_ENABLE_ASYNC_DUE_CAPTURE:-0}"' in source
    assert 'MARKETHUB_REQUIRED_CAPTURE_TIMEOUT_SECONDS:-3600' in source
    assert 'MARKETHUB_CAPTURE_TIMEOUT_SECONDS:-60' in source
    assert 'capture_event=required_started capability_id=$capability_id' in source
    assert 'capture_event=required_failed capability_id=$capability_id reason=$reason' in source
    assert 'capture_event=due_enqueue_started endpoint=$MARKETHUB_CAPTURE_ENDPOINT' in source
    assert 'capture_event=due_enqueue_failed endpoint=$MARKETHUB_CAPTURE_ENDPOINT reason=$reason' in source
    assert 'capture_event=due_enqueue_skipped reason=declared_dependencies_completed' in source
    assert '[ "$status" -eq 28 ] && reason="timeout"' in source
    assert 'MARKETHUB_REQUIRED_CAPTURE_RETRIES="${MARKETHUB_REQUIRED_CAPTURE_RETRIES:-12}"' in source
    assert 'MARKETHUB_REQUIRED_CAPTURE_RETRY_DELAY_SECONDS="${MARKETHUB_REQUIRED_CAPTURE_RETRY_DELAY_SECONDS:-300}"' in source
    assert "capture_event=required_retry_scheduled" in source


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("flock") is None, reason="requires bash and flock")
def test_required_capture_retries_after_a_failed_snapshot(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "output=''\n"
        "url=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -o) output=\"$2\"; shift 2;;\n"
        "    -*) shift;;\n"
        "    *) url=\"$1\"; shift;;\n"
        "  esac\n"
        "done\n"
        "if [[ \"$url\" == */api/health ]]; then printf '{}\\n'; exit 0; fi\n"
        "if [[ \"$url\" == */api/admin/capture-runs* ]]; then printf '%s\\n' '[]'; exit 0; fi\n"
        "count_file=\"$FAKE_CURL_COUNT\"\n"
        "count=0; [ -f \"$count_file\" ] && count=$(cat \"$count_file\")\n"
        "count=$((count + 1)); printf '%s' \"$count\" > \"$count_file\"\n"
        "if [ \"$count\" -lt 3 ]; then printf '%s\\n' '{\"status\":\"failed\",\"error_message\":\"provider temporarily unavailable\"}' > \"$output\"; else printf '%s\\n' '{\"status\":\"success\",\"id\":123}' > \"$output\"; fi\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    health = tmp_path / "health.sh"
    health.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    health.chmod(0o755)
    environment = dict(os.environ)
    environment.update({
        "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
        "FAKE_CURL_COUNT": str(tmp_path / "curl-count"),
        "MARKETHUB_BASE_URL": "http://fake",
        "MARKETHUB_RUNTIME_ROOT": str(tmp_path),
        "MARKETHUB_PYTHON": sys.executable,
        "MARKETHUB_REQUIRED_CAPTURE_CAPABILITIES": "stocks.quotes.daily_snapshot",
        "MARKETHUB_REQUIRED_CAPTURE_RETRIES": "3",
        "MARKETHUB_REQUIRED_CAPTURE_RETRY_DELAY_SECONDS": "0",
        "MARKETHUB_DATA_HEALTH_SCRIPT": str(health),
        "MARKETHUB_ENABLE_DAILY_PARQUET_PUBLISH": "0",
        "MARKETHUB_GLOBAL_UPDATE_LOCK_PATH": str(tmp_path / "global-update.lock"),
    })

    completed = subprocess.run(
        ["bash", str(SCRIPT_DIR / "global-data-update-with-health.sh")],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "capture_event=required_retry_scheduled" in completed.stdout
    assert "capture_event=required_completed capability_id=stocks.quotes.daily_snapshot attempt=3/3" in completed.stdout


def test_task_center_contract_schedules_final_snapshot_after_source_ready() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "reconcile_task_center.py"), "--print"],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["task_id"] == "markethub_global_data_update"
    assert payload["schedule_type"] == "cron"
    assert payload["schedule_value"] == "5 16 * * *"
    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["script_path"] == "/data/markethub/scripts/global-data-update-with-health.sh"
    assert "16:05" in payload["description"]


def _pipeline_harness(tmp_path: Path, *, stock_daily_status: str, health_exit_code: int) -> tuple[dict[str, str], Path, Path]:
    """Build a pipeline whose data-health report mirrors the production shape.

    ``stock_daily_status`` drives the publication's own declared dependency;
    the report always carries an unrelated unhealthy dataset so the platform
    aggregate stays unhealthy, which is the live 2026-09-17 failure mode.
    """
    log_path = tmp_path / "pipeline.log"
    capture_started = tmp_path / "capture-started"
    capture = tmp_path / "capture.sh"
    health = tmp_path / "health.sh"
    publisher = tmp_path / "publisher.py"
    payload_path = tmp_path / "latest.json"
    report = {
        "status": "unhealthy",
        "checked_at": "__CHECKED_AT__",
        "dependencies": {
            "core_dataset_freshness": {
                "status": "unhealthy",
                "checks": [
                    {"check_id": "core_dataset_freshness:fact.stock_daily_1d", "status": stock_daily_status},
                    {"check_id": "core_dataset_freshness:fact.board_daily_1d", "status": "unhealthy"},
                ],
            }
        },
        "capabilities": [{"capability_id": "concepts.indicators.money_flow", "status": "unhealthy", "checks": []}],
    }
    serialized = json.dumps(report, ensure_ascii=False).replace("__CHECKED_AT__", "'\"$(date '+%F %T')\"'")
    capture.write_text(f'#!/usr/bin/env bash\necho capture >> "{log_path}"\ntouch "{capture_started}"\nsleep 1\n', encoding="utf-8")
    health.write_text(
        "#!/usr/bin/env bash\n"
        f'echo health >> "{log_path}"\n'
        f"printf '%s\\n' '{serialized}' > \"{payload_path}\"\n"
        f"exit {health_exit_code}\n",
        encoding="utf-8",
    )
    publisher.write_text(f'from pathlib import Path\nPath(r"{log_path}").open("a").write("publish\\n")\n', encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "url=\"\"\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -*) shift;;\n"
        "    *) url=\"$1\"; shift;;\n"
        "  esac\n"
        "done\n"
        "if [[ \"$url\" == */api/admin/capture-runs* ]]; then printf '%s\\n' '[]'; exit 0; fi\n"
        "if [[ \"$url\" == */api/health ]]; then printf '{}\\n'; exit 0; fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
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
        "MARKETHUB_DATA_HEALTH_PAYLOAD": str(payload_path),
        "MARKETHUB_PUBLICATION_HEALTH_DEPENDENCIES": "core_dataset_freshness:fact.stock_daily_1d",
        "MARKETHUB_GLOBAL_UPDATE_LOCK_PATH": str(tmp_path / "global-update.lock"),
        "MARKETHUB_GLOBAL_UPDATE_LOCK_TIMEOUT_SECONDS": "0",
        "MARKETHUB_BASE_URL": "http://fake",
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    })
    return environment, log_path, capture_started


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("flock") is None, reason="requires bash and flock")
def test_unrelated_health_alert_no_longer_blocks_the_publication(tmp_path: Path) -> None:
    environment, log_path, _ = _pipeline_harness(tmp_path, stock_daily_status="healthy", health_exit_code=1)

    completed = subprocess.run(
        ["bash", str(SCRIPT_DIR / "global-data-update-with-health.sh")],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    assert "global_update_health_outcome=alert" in completed.stdout
    assert "publication_health_gate=passed" in completed.stdout
    assert log_path.read_text(encoding="utf-8").splitlines() == ["capture", "health", "publish"]


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("flock") is None, reason="requires bash and flock")
def test_unhealthy_declared_dependency_still_blocks_the_publication(tmp_path: Path) -> None:
    environment, log_path, _ = _pipeline_harness(tmp_path, stock_daily_status="unhealthy", health_exit_code=1)

    completed = subprocess.run(
        ["bash", str(SCRIPT_DIR / "global-data-update-with-health.sh")],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    assert "global_update_publication_outcome=deferred reason=declared_dependency_unhealthy" in completed.stdout
    assert log_path.read_text(encoding="utf-8").splitlines() == ["capture", "health"]


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("flock") is None, reason="requires bash and flock")
def test_health_gated_update_does_not_run_duplicate_pipeline(tmp_path: Path) -> None:
    environment, log_path, capture_started = _pipeline_harness(tmp_path, stock_daily_status="healthy", health_exit_code=0)
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
