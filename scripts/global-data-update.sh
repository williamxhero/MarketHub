#!/usr/bin/env bash
set -Eeuo pipefail

# 全局数据更新入口：Task Center 只调用这个脚本，实际到期判断由 QuoteMux capture policy 决定。
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
RUN_ROOT="${MARKETHUB_DATA_UPDATE_ROOT:-$RUNTIME_ROOT/data-update}"
LOG_ROOT="${MARKETHUB_LOG_ROOT:-$RUNTIME_ROOT/logs}"
LOCK_PATH="$RUN_ROOT/global-data-update.lock"
RUN_ID="$(date '+%Y%m%d_%H%M%S')"
RESULT_DIR="$RUN_ROOT/results"
RESULT_PATH="$RESULT_DIR/$RUN_ID.json"
LOG_PATH="$LOG_ROOT/global-data-update.log"
DB_HOST="${MARKETHUB_DB_HOST:-127.0.0.1}"
DB_PORT="${MARKETHUB_DB_PORT:-55432}"
DB_NAME="${MARKETHUB_DB_NAME:-markethub_dev}"
DB_USER="${MARKETHUB_DB_USER:-markethub}"
DB_PASSWORD="${MARKETHUB_DB_PASSWORD:-markethub_dev_password}"

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1"
}

preprocess() {
    log "预处理：准备目录并检查 MarketHub"
    mkdir -p "$RESULT_DIR" "$LOG_ROOT"
    curl --fail --silent --show-error --connect-timeout 10 --max-time 30 "$MARKETHUB_BASE_URL/api/health" >/dev/null
    curl --fail --silent --show-error --connect-timeout 10 --max-time 60 "$MARKETHUB_BASE_URL/api/admin/capture-policies" >/dev/null
    # Task Center 是外部触发 capture run 的唯一入口；任何残留 running 行（无论新旧）
    # 都会导致本次调度直接拒绝。MarketHub 内部 auto-supersede 仅在同 capability 再次启动
    # 时生效，无法覆盖本脚本这里先到的检查，因此在这里直接清掉所有 running 行。
    PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -A -c "update capability_capture_runs set status='failed', finished_at=now(), error_message='Task Center 启动前清理：残留 running 行强制作废' where status = 'running'" >/dev/null
    active_runs=$(PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -A -c "select count(*) from capability_capture_runs where status = 'running'")
    if [ "$active_runs" != "0" ]; then
        log "已有 capability 数据更新正在运行：$active_runs"
        exit 1
    fi
}

core_execute() {
    log "核心执行：提交后台 capability 数据更新"
    curl --fail --silent --show-error --connect-timeout 10 --max-time 60 -X POST "$MARKETHUB_BASE_URL/api/admin/capture/run-due-async" -o "$RESULT_PATH"
}

postprocess() {
    log "后处理：校验更新结果"
    python3 - "$RESULT_PATH" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

result_path = Path(sys.argv[1])
payload = json.loads(result_path.read_text(encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit("capture 后台提交返回值不是对象")

accepted = payload.get("accepted", False)
print(f"accepted={accepted}")
if accepted is not True:
    raise SystemExit("capture 后台提交未被接受")
PY
    log "后处理：结果文件 $RESULT_PATH"
}

run_once() {
    log "开始 MarketHub / QuoteMux 全局数据更新"
    preprocess
    core_execute
    postprocess
    log "完成 MarketHub / QuoteMux 全局数据更新"
}

main() {
    mkdir -p "$RUN_ROOT" "$LOG_ROOT"
    {
        flock -n 9 || { log "已有全局数据更新脚本正在运行"; exit 1; }
        run_once
    } 9>"$LOCK_PATH" 2>&1 | tee -a "$LOG_PATH"
}

main
