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
# This script is also used by schedules that do not gate a publication.  Those
# schedules must enqueue due work instead of holding their systemd unit open
# while unrelated historical backlog is replayed.
MARKETHUB_CAPTURE_ENDPOINT="${MARKETHUB_CAPTURE_ENDPOINT:-/api/admin/capture/run-due-async}"
MARKETHUB_REQUIRED_CAPTURE_CAPABILITIES="${MARKETHUB_REQUIRED_CAPTURE_CAPABILITIES:-}"
RUN_ROOT="${MARKETHUB_DATA_UPDATE_ROOT:-$RUNTIME_ROOT/data-update}"
LOG_ROOT="${MARKETHUB_LOG_ROOT:-$RUNTIME_ROOT/logs}"
RUN_ID="$(date '+%Y%m%d_%H%M%S')"
RESULT_DIR="$RUN_ROOT/results"
RESULT_PATH="$RESULT_DIR/$RUN_ID.json"
LOG_PATH="$LOG_ROOT/global-data-update.log"

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1"
}

preprocess() {
    mkdir -p "$RESULT_DIR" "$LOG_ROOT"
    test -x "$MARKETHUB_PYTHON"
    curl --fail --silent --show-error --connect-timeout 10 --max-time 30 "$MARKETHUB_BASE_URL/api/health" >/dev/null
}

core_execute() {
    local capability_id
    local capture_path
    local safe_capability_id

    IFS=',' read -r -a required_capabilities <<< "$MARKETHUB_REQUIRED_CAPTURE_CAPABILITIES"
    for capability_id in "${required_capabilities[@]}"; do
        capability_id="$(printf '%s' "$capability_id" | xargs)"
        [ -n "$capability_id" ] || continue
        safe_capability_id="${capability_id//[^A-Za-z0-9_.-]/_}"
        capture_path="$RESULT_DIR/$RUN_ID.required-$safe_capability_id.json"
        log "capture_event=required_started capability_id=$capability_id timeout_seconds=${MARKETHUB_REQUIRED_CAPTURE_TIMEOUT_SECONDS:-1200}"
        curl --fail --silent --show-error --connect-timeout 10 --max-time "${MARKETHUB_REQUIRED_CAPTURE_TIMEOUT_SECONDS:-1200}" \
            -X POST "$MARKETHUB_BASE_URL/api/admin/capture-runs/$capability_id" \
            -o "$capture_path" || {
                local status=$?
                local reason="curl_exit_$status"
                [ "$status" -eq 28 ] && reason="timeout"
                log "capture_event=required_failed capability_id=$capability_id reason=$reason"
                return "$status"
            }
        "$MARKETHUB_PYTHON" - "$capture_path" "$capability_id" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
capability_id = sys.argv[2]
if not isinstance(payload, dict) or payload.get("status") != "success":
    raise SystemExit(f"required capture failed capability_id={capability_id} payload={payload}")
print(f"capture_event=required_completed capability_id={capability_id} capture_run_id={payload.get('id', '')}")
PY
    done

    log "capture_event=due_enqueue_started endpoint=$MARKETHUB_CAPTURE_ENDPOINT timeout_seconds=${MARKETHUB_CAPTURE_TIMEOUT_SECONDS:-60}"
    curl --fail --silent --show-error --connect-timeout 10 --max-time "${MARKETHUB_CAPTURE_TIMEOUT_SECONDS:-60}" \
        -X POST "$MARKETHUB_BASE_URL$MARKETHUB_CAPTURE_ENDPOINT" \
        -o "$RESULT_PATH" || {
            local status=$?
            local reason="curl_exit_$status"
            [ "$status" -eq 28 ] && reason="timeout"
            log "capture_event=due_enqueue_failed endpoint=$MARKETHUB_CAPTURE_ENDPOINT reason=$reason"
            return "$status"
        }
}

postprocess() {
    "$MARKETHUB_PYTHON" - "$RESULT_PATH" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if isinstance(payload, dict):
    if payload.get("accepted") is not True:
        raise SystemExit("到期采集异步请求未被接受")
    print(f"capture_event=due_enqueued accepted=true detail={json.dumps(payload, ensure_ascii=False, sort_keys=True)}")
elif isinstance(payload, list):
    failed = [str(item.get("capability_id", "")) for item in payload if isinstance(item, dict) and str(item.get("status", "")) == "failed"]
    if failed:
        raise SystemExit(f"到期采集失败: {','.join(failed)}")
    print(f"due_capture_completed=true runs={len(payload)}")
else:
    raise SystemExit("到期采集接口返回值无效")
PY
}

main() {
    log "预处理：检查 API 和运行环境"
    preprocess
    log "核心执行：运行当前发布依赖并入队到期采集"
    core_execute
    log "后处理：校验采集结果"
    postprocess
    log "完成每日数据更新 result=$RESULT_PATH"
}

main 2>&1 | tee -a "$LOG_PATH"
