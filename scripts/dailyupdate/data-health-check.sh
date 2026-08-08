#!/usr/bin/env bash
set -Eeuo pipefail

# 数据健康检查入口：只读本地 API 和数据库，不触发外部 provider。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="${MARKETHUB_RUNTIME_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_PATH="${MARKETHUB_ENV_PATH:-$RUNTIME_ROOT/env/markethub.env}"
if [ ! -f "$ENV_PATH" ] && [ -f "/data/markethub/env/markethub.env" ]; then
    ENV_PATH="/data/markethub/env/markethub.env"
fi
if [ -f "$ENV_PATH" ]; then
    set -a
    . "$ENV_PATH"
    set +a
fi

MARKETHUB_HOST="${MARKETHUB_HOST:-127.0.0.1}"
MARKETHUB_PORT="${MARKETHUB_PORT:-8803}"
# 服务监听通配地址不能作为本机客户端目标；显式配置仍优先。
MARKETHUB_BASE_URL="${MARKETHUB_BASE_URL:-http://${MARKETHUB_HOST/0.0.0.0/127.0.0.1}:$MARKETHUB_PORT}"
WORKSPACE_ROOT="${MARKETHUB_PROJECT_ROOT:-$(cd "$RUNTIME_ROOT/.." && pwd)}"
if [ ! -d "$WORKSPACE_ROOT/QuoteMux" ] && [ -d "/data/MarketHub2/current/QuoteMux" ]; then
    WORKSPACE_ROOT="/data/MarketHub2/current"
fi
MARKETHUB_PYTHON="${MARKETHUB_PYTHON:-$WORKSPACE_ROOT/.venv/bin/python}"
if [ ! -x "$MARKETHUB_PYTHON" ] && [ -x "/data/MarketHub2/current/.venv/bin/python" ]; then
    MARKETHUB_PYTHON="/data/MarketHub2/current/.venv/bin/python"
fi

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
    log "预处理：准备数据健康检查目录"
    mkdir -p "$RESULT_DIR" "$LOG_ROOT"
    curl --fail --silent --show-error --connect-timeout 10 --max-time 30 "$MARKETHUB_BASE_URL/api/health" >/dev/null
}

core_execute() {
    log "核心执行：读取 MarketHub 数据健康"
    curl --fail --silent --show-error --connect-timeout 10 --max-time "${MARKETHUB_DATA_HEALTH_TIMEOUT_SECONDS:-600}" \
        -X POST \
        "$MARKETHUB_BASE_URL/api/data-health/run" \
        -o "$RESULT_PATH"
}

postprocess() {
    log "后处理：校验数据健康结果"
    local health_status
    health_status="$(
        "$MARKETHUB_PYTHON" - "$RESULT_PATH" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

result_path = Path(sys.argv[1])
payload = json.loads(result_path.read_text(encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit("数据健康接口返回值不是对象")
status = str(payload.get("status", ""))
summary = payload.get("summary", {})
if not isinstance(summary, dict):
    summary = {}
summary_text = (
    "status={status} total={total} healthy={healthy} warning={warning} unhealthy={unhealthy}".format(
        status=status,
        total=summary.get("total", ""),
        healthy=summary.get("healthy", ""),
        warning=summary.get("warning", ""),
        unhealthy=summary.get("unhealthy", ""),
    )
)
if status == "":
    raise SystemExit("数据健康接口缺少 status")
print(summary_text, file=sys.stderr)
print(status)
PY
    )"
    if [ ! -f "$LATEST_PATH" ]; then
        local latest_tmp
        latest_tmp="$LATEST_PATH.tmp"
        cp "$RESULT_PATH" "$latest_tmp"
        mv "$latest_tmp" "$LATEST_PATH"
    fi
    log "后处理：数据健康结果 $RESULT_PATH"
    if [ "$health_status" = "unhealthy" ]; then
        log "后处理：数据健康检查异常"
        return 1
    fi
}

run_once() {
    log "开始 MarketHub 数据健康检查"
    preprocess
    core_execute
    postprocess
    log "完成 MarketHub 数据健康检查"
}

main() {
    run_once 2>&1 | tee -a "$LOG_PATH"
}

main
