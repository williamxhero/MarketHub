#!/usr/bin/env bash
set -Eeuo pipefail

# 涨跌停封单额专用采集入口：Task Center 在 15:10 和 15:35 单独调用本脚本。
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
MARKETHUB_BASE_URL="${MARKETHUB_BASE_URL:-http://$MARKETHUB_HOST:$MARKETHUB_PORT}"
RUN_ROOT="${MARKETHUB_LIMIT_ORDER_AMOUNT_ROOT:-$RUNTIME_ROOT/limit-order-amount}"
LOG_ROOT="${MARKETHUB_LOG_ROOT:-$RUNTIME_ROOT/logs}"
LOCK_PATH="$RUN_ROOT/limit-order-amount.lock"
RUN_ID="$(date '+%Y%m%d_%H%M%S')"
RESULT_DIR="$RUN_ROOT/results"
RESULT_PATH="$RESULT_DIR/$RUN_ID.json"
LOG_PATH="$LOG_ROOT/limit-order-amount-update.log"
TRADE_DATE="${MARKETHUB_LIMIT_ORDER_TRADE_DATE:-}"

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1"
}

preprocess() {
    log "预处理：准备目录并检查 MarketHub 健康状态"
    mkdir -p "$RESULT_DIR" "$LOG_ROOT"
    curl --fail --silent --show-error --connect-timeout 10 --max-time 30 "$MARKETHUB_BASE_URL/api/health" >/dev/null
}

core_execute() {
    log "核心执行：触发涨跌停封单额专用采集"
    local url="$MARKETHUB_BASE_URL/api/admin/capture/limit-order-amount/run-today"
    if [ "$TRADE_DATE" != "" ]; then
        url="$url?trade_date=$TRADE_DATE"
    fi
    curl --fail --silent --show-error --connect-timeout 10 --max-time 600 -X POST "$url" -o "$RESULT_PATH"
}

postprocess() {
    log "后处理：校验封单额采集结果"
    python3 - "$RESULT_PATH" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

result_path = Path(sys.argv[1])
payload = json.loads(result_path.read_text(encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit("封单额采集返回值不是对象")
status = str(payload.get("status", ""))
print(f"status={status} trade_date={payload.get('trade_date', '')} candidate_count={payload.get('candidate_count', 0)} row_count={payload.get('row_count', 0)}")
if status not in {"success", "skipped"}:
    raise SystemExit(str(payload.get("error_message", "封单额采集失败")))
PY
    log "后处理：结果文件 $RESULT_PATH"
}

run_once() {
    log "开始涨跌停封单额采集"
    preprocess
    core_execute
    postprocess
    log "完成涨跌停封单额采集"
}

main() {
    mkdir -p "$RUN_ROOT" "$LOG_ROOT"
    {
        flock -n 9 || { log "已有涨跌停封单额采集正在运行"; exit 1; }
        run_once
    } 9>"$LOCK_PATH" 2>&1 | tee -a "$LOG_PATH"
}

main
