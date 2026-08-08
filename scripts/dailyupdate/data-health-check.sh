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
MARKETHUB_PYTHON="${MARKETHUB_PYTHON:-$(cd "$RUNTIME_ROOT/.." && pwd)/.venv/bin/python}"
RUN_ROOT="${MARKETHUB_DATA_HEALTH_ROOT:-$RUNTIME_ROOT/data-health}"
LOG_ROOT="${MARKETHUB_LOG_ROOT:-$RUNTIME_ROOT/logs}"
RUN_ID="$(date '+%Y%m%d_%H%M%S')"
RESULT_DIR="$RUN_ROOT/results"
RESULT_PATH="$RESULT_DIR/$RUN_ID.json"
LATEST_PATH="$RUN_ROOT/latest.json"
LOG_PATH="$LOG_ROOT/data-health-check.log"

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1"
}

preprocess() {
    mkdir -p "$RESULT_DIR" "$LOG_ROOT"
    test -x "$MARKETHUB_PYTHON"
    curl --fail --silent --show-error --connect-timeout 10 --max-time 30 "$MARKETHUB_BASE_URL/api/health" >/dev/null
}

core_execute() {
    curl --fail --silent --show-error --connect-timeout 10 --max-time "${MARKETHUB_DATA_HEALTH_TIMEOUT_SECONDS:-600}" \
        -X POST "$MARKETHUB_BASE_URL/api/data-health/run" \
        -o "$RESULT_PATH"
}

postprocess() {
    "$MARKETHUB_PYTHON" - "$RESULT_PATH" "$LATEST_PATH" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

result_path = Path(sys.argv[1])
latest_path = Path(sys.argv[2])
payload = json.loads(result_path.read_text(encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit("数据健康接口返回值不是对象")
status = str(payload.get("status", ""))
if status == "":
    raise SystemExit("数据健康接口缺少 status")
latest_path.parent.mkdir(parents=True, exist_ok=True)
latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"data_health_status={status}")
if status == "unhealthy":
    raise SystemExit("数据健康检查不通过")
PY
}

main() {
    log "预处理：检查 API 和运行环境"
    preprocess
    log "核心执行：运行数据健康检查"
    core_execute
    log "后处理：校验健康结果"
    postprocess
    log "完成数据健康检查 result=$RESULT_PATH"
}

main 2>&1 | tee -a "$LOG_PATH"
