#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="${MARKETHUB_RUNTIME_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_PATH="${MARKETHUB_ENV_PATH:-$RUNTIME_ROOT/env/markethub.env}"
if [ -f "$ENV_PATH" ]; then
    set -a
    . "$ENV_PATH"
    set +a
fi

MARKETHUB_HOST="${MARKETHUB_HOST:-127.0.0.1}"
MARKETHUB_PORT="${MARKETHUB_PORT:-8803}"
MARKETHUB_BASE_URL="${MARKETHUB_BASE_URL:-http://${MARKETHUB_HOST/0.0.0.0/127.0.0.1}:$MARKETHUB_PORT}"
MARKETHUB_PYTHON="${MARKETHUB_PYTHON:-$RUNTIME_ROOT/.venv/bin/python}"
MARKETHUB_LOCK_PATH="${MARKETHUB_GLOBAL_UPDATE_LOCK_PATH:-$RUNTIME_ROOT/locks/global-data-update.lock}"
MARKETHUB_LOCK_TIMEOUT_SECONDS="${MARKETHUB_INTRADAY_CAPTURE_LOCK_TIMEOUT_SECONDS:-21600}"
MARKETHUB_CAPTURE_TIMEOUT_SECONDS="${MARKETHUB_INTRADAY_CAPTURE_TIMEOUT_SECONDS:-21600}"
LOG_ROOT="${MARKETHUB_LOG_ROOT:-$RUNTIME_ROOT/logs}"
RESULT_ROOT="${MARKETHUB_DATA_UPDATE_ROOT:-$RUNTIME_ROOT/data-update}/intraday"
RUN_ID="$(date '+%Y%m%d_%H%M%S')"
RESULT_PATH="$RESULT_ROOT/$RUN_ID.json"
LOG_PATH="$LOG_ROOT/stock-intraday-capture.log"

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1"
}

if ! [[ "$MARKETHUB_LOCK_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || ! [[ "$MARKETHUB_CAPTURE_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]]; then
    log "分钟线参数无效 lock_timeout=$MARKETHUB_LOCK_TIMEOUT_SECONDS capture_timeout=$MARKETHUB_CAPTURE_TIMEOUT_SECONDS"
    exit 64
fi

mkdir -p "$RESULT_ROOT" "$LOG_ROOT" "$(dirname "$MARKETHUB_LOCK_PATH")"
test -x "$MARKETHUB_PYTHON"
exec 9>"$MARKETHUB_LOCK_PATH"
if ! flock -w "$MARKETHUB_LOCK_TIMEOUT_SECONDS" 9; then
    log "intraday_capture=failed reason=global_update_lock_timeout timeout_seconds=$MARKETHUB_LOCK_TIMEOUT_SECONDS"
    exit 75
fi
trap 'flock -u 9 || true' EXIT

curl --fail --silent --show-error --connect-timeout 10 --max-time 30 \
    "$MARKETHUB_BASE_URL/api/health" >/dev/null
deadline=$((SECONDS + MARKETHUB_LOCK_TIMEOUT_SECONDS))
while (( SECONDS <= deadline )); do
    running_payload="$(curl --fail --silent --show-error --connect-timeout 10 --max-time 30 \
        "$MARKETHUB_BASE_URL/api/admin/capture-runs?capability_id=stocks.quotes.intraday&status=running&limit=20")" || {
        log "intraday_capture=failed reason=running_check_api_error"
        exit 1
    }
    running_count="$("$MARKETHUB_PYTHON" -c 'import json,sys; value=json.load(sys.stdin); print(len(value) if isinstance(value,list) else -1)' <<<"$running_payload")"
    if [ "$running_count" = "0" ]; then
        break
    fi
    if [ "$running_count" = "-1" ]; then
        log "intraday_capture=failed reason=running_check_invalid_response"
        exit 1
    fi
    log "intraday_capture=waiting_for_existing_run count=$running_count"
    sleep 10
done
if [ "${running_count:-}" != "0" ]; then
    log "intraday_capture=failed reason=existing_run_wait_timeout timeout_seconds=$MARKETHUB_LOCK_TIMEOUT_SECONDS"
    exit 75
fi
log "intraday_capture=started capability_id=stocks.quotes.intraday"
if curl --fail --silent --show-error --connect-timeout 10 --max-time "$MARKETHUB_CAPTURE_TIMEOUT_SECONDS" \
    -X POST "$MARKETHUB_BASE_URL/api/admin/capture-runs/stocks.quotes.intraday" \
    -o "$RESULT_PATH"; then
    :
else
    status=$?
    log "intraday_capture=failed reason=curl_exit_$status result=$RESULT_PATH"
    exit "$status"
fi

if ! "$MARKETHUB_PYTHON" - "$RESULT_PATH" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(payload, dict) or payload.get("status") != "success":
    raise SystemExit(f"intraday capture incomplete: {payload}")
print(f"capture_run_id={payload.get('id', '')} row_count={payload.get('row_count', 0)} coverage_count={payload.get('coverage_count', 0)}")
PY
then
    log "intraday_capture=failed reason=incomplete_contract result=$RESULT_PATH"
    exit 1
fi
log "intraday_capture=completed result=$RESULT_PATH"
