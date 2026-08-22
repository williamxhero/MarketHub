#!/usr/bin/env bash
set -Eeuo pipefail

RUNTIME_ROOT="${MARKETHUB_RUNTIME_ROOT:-/data/markethub}"
ENV_PATH="${MARKETHUB_ENV_PATH:-$RUNTIME_ROOT/env/markethub.env}"
if [ -f "$ENV_PATH" ]; then
    set -a
    . "$ENV_PATH"
    set +a
fi

BASE_URL="${MARKETHUB_BASE_URL:-http://127.0.0.1:8803}"
PYTHON_BIN="${MARKETHUB_PYTHON:-$RUNTIME_ROOT/.venv/bin/python}"
RESULT_ROOT="${MARKETHUB_FUTURES_UPDATE_ROOT:-$RUNTIME_ROOT/futures-update}"
mkdir -p "$RESULT_ROOT"
RESULT_PATH="$RESULT_ROOT/$(date '+%Y%m%d_%H%M%S').json"

if "$PYTHON_BIN" - "$BASE_URL" <<'PY'
from __future__ import annotations

from datetime import datetime
import json
import sys
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo


def has_completed_capture(base_url: str, planned_date: str) -> bool:
    query = urlencode({
        "capability_id": "futures.quotes.main_continuous.1m",
        "limit": 50,
    })
    with urlopen(f"{base_url.rstrip('/')}/api/admin/capture-runs?{query}", timeout=30) as response:
        runs = json.loads(response.read().decode("utf-8"))
    if not isinstance(runs, list):
        raise RuntimeError("期货 capture run 响应不是列表")
    return any(
        isinstance(run, dict)
        and run.get("status") == "success"
        and str(run.get("planned_time") or "").startswith(planned_date)
        and (int(run.get("coverage_count") or 0) > 0 or int(run.get("row_count") or 0) > 0)
        for run in runs
    )


base_url = sys.argv[1]
planned_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
if has_completed_capture(base_url, planned_date):
    print(json.dumps({"status": "success", "reason": "already_completed_today", "planned_date": planned_date}, ensure_ascii=False))
    raise SystemExit(0)
raise SystemExit(1)
PY
then
    exit 0
else
    precheck_exit=$?
    if [ "$precheck_exit" -ne 1 ]; then
        exit "$precheck_exit"
    fi
fi

curl --fail --silent --show-error --max-time "${MARKETHUB_FUTURES_UPDATE_TIMEOUT_SECONDS:-7200}" \
    -X POST "$BASE_URL/api/admin/capture-runs/futures.quotes.main_continuous.1m" \
    -o "$RESULT_PATH"

"$PYTHON_BIN" - "$RESULT_PATH" "$BASE_URL" <<'PY'
from __future__ import annotations

import json
from pathlib import Path
import sys
from urllib.parse import urlencode
from urllib.request import urlopen

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
base_url = sys.argv[2].rstrip("/")
if payload.get("status") == "success":
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(0)


def has_completed_capture(planned_date: str) -> bool:
    query = urlencode({
        "capability_id": "futures.quotes.main_continuous.1m",
        "limit": 50,
    })
    try:
        with urlopen(f"{base_url}/api/admin/capture-runs?{query}", timeout=30) as response:
            runs = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"无法核验 advisory lock 对应运行: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    if not isinstance(runs, list):
        return False
    return any(
        isinstance(run, dict)
        and run.get("status") == "success"
        and str(run.get("planned_time") or "").startswith(planned_date)
        and (int(run.get("coverage_count") or 0) > 0 or int(run.get("row_count") or 0) > 0)
        for run in runs
    )


detail = payload.get("detail_json")
planned_date = str(payload.get("planned_time") or "")[:10]
if (
    payload.get("status") == "skipped"
    and isinstance(detail, dict)
    and detail.get("reason") == "advisory_lock_busy"
    and len(planned_date) == 10
    and has_completed_capture(planned_date)
):
    print(json.dumps({"status": "success", "reason": "redundant_advisory_lock_skip", "capture_run": payload}, ensure_ascii=False))
    raise SystemExit(0)

if payload.get("status") != "success":
    raise SystemExit(f"期货主力连续更新失败: {payload}")
PY
