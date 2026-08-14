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
    curl --fail --silent --show-error --connect-timeout 10 --max-time "${MARKETHUB_CAPTURE_TIMEOUT_SECONDS:-18000}" \
        -X POST "$MARKETHUB_BASE_URL/api/admin/capture/run-due" \
        -o "$RESULT_PATH"
}

postprocess() {
    "$MARKETHUB_PYTHON" - "$RESULT_PATH" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(payload, list):
    raise SystemExit("到期采集接口返回值不是列表")
failures = [
    item
    for item in payload
    if isinstance(item, dict)
    and (str(item.get("status", "")) == "failed" or str(item.get("error", item.get("error_message", ""))) != "")
]
print(f"due_capture_runs={len(payload)} failed={len(failures)}")
if failures:
    raise SystemExit("到期采集存在失败任务")
PY
}

main() {
    log "预处理：检查 API 和运行环境"
    preprocess
    log "核心执行：运行到期采集"
    core_execute
    log "后处理：校验采集结果"
    postprocess
    log "完成每日数据更新 result=$RESULT_PATH"
}

main 2>&1 | tee -a "$LOG_PATH"
