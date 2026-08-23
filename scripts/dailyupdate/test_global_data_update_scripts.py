from __future__ import annotations

from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


def test_global_update_keeps_async_default_for_non_health_schedules() -> None:
    source = (SCRIPT_DIR / "global-data-update.sh").read_text(encoding="utf-8")

    assert 'MARKETHUB_CAPTURE_ENDPOINT="${MARKETHUB_CAPTURE_ENDPOINT:-/api/admin/capture/run-due-async}"' in source
    assert '-X POST "$MARKETHUB_BASE_URL$MARKETHUB_CAPTURE_ENDPOINT"' in source


def test_health_gated_update_queues_due_capture_before_health_check() -> None:
    source = (SCRIPT_DIR / "global-data-update-with-health.sh").read_text(encoding="utf-8")

    assert 'MARKETHUB_HEALTH_CAPTURE_ENDPOINT="${MARKETHUB_HEALTH_CAPTURE_ENDPOINT:-/api/admin/capture/run-due-async}"' in source
    assert 'MARKETHUB_CAPTURE_ENDPOINT="$MARKETHUB_HEALTH_CAPTURE_ENDPOINT" "$GLOBAL_DATA_UPDATE_SCRIPT"' in source
