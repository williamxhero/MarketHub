#!/usr/bin/env bash
set -Eeuo pipefail

# 涨跌停封单额专用采集入口：Task Center 每天 18:00 调用本脚本，脚本只在开盘日执行采集。
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
MARKETHUB_PYTHON="${MARKETHUB_PYTHON:-$RUNTIME_ROOT/.venv/bin/python}"
MARKETHUB_CLIENT_HOST="$MARKETHUB_HOST"
if [ "$MARKETHUB_CLIENT_HOST" = "0.0.0.0" ]; then
    MARKETHUB_CLIENT_HOST="127.0.0.1"
fi
MARKETHUB_BASE_URL="${MARKETHUB_BASE_URL:-http://$MARKETHUB_CLIENT_HOST:$MARKETHUB_PORT}"
RUN_ROOT="${MARKETHUB_LIMIT_ORDER_AMOUNT_ROOT:-$RUNTIME_ROOT/limit-order-amount}"
LOG_ROOT="${MARKETHUB_LOG_ROOT:-$RUNTIME_ROOT/logs}"
LOCK_PATH="$RUN_ROOT/limit-order-amount.lock"
RUN_ID="$(date '+%Y%m%d_%H%M%S')"
RESULT_DIR="$RUN_ROOT/results"
RESULT_PATH="$RESULT_DIR/$RUN_ID.json"
LOG_PATH="$LOG_ROOT/limit-order-amount-update.log"
REQUEST_TRADE_DATE="${MARKETHUB_LIMIT_ORDER_TRADE_DATE:-${1:-}}"
TRADE_DATE=""
WORKSPACE_ROOT="${MARKETHUB_PROJECT_ROOT:-$(cd "$RUNTIME_ROOT/.." && pwd)}"
QUOTEMUX_RUNTIME_ROOT="${QUOTEMUX_RUNTIME_ROOT:-$WORKSPACE_ROOT/runtime}"
PACKAGE_VENV_ROOT="${QUOTEMUX_PACKAGE_VENV_ROOT:-$QUOTEMUX_RUNTIME_ROOT/package_venvs}"

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1"
}

preprocess() {
    log "预处理：准备目录并检查 MarketHub 健康状态"
    mkdir -p "$RESULT_DIR" "$LOG_ROOT"
    curl --fail --silent --show-error --connect-timeout 10 --max-time 30 "$MARKETHUB_BASE_URL/api/health" >/dev/null
    if [ "$REQUEST_TRADE_DATE" != "" ]; then
        TRADE_DATE="$REQUEST_TRADE_DATE"
    else
        TRADE_DATE="$(TZ=Asia/Shanghai date '+%F')"
    fi
    ensure_playwright_chromium
}

ensure_playwright_chromium() {
    local python_path
    if [ -x "$MARKETHUB_PYTHON" ]; then
        "$MARKETHUB_PYTHON" - <<'PY'
from __future__ import annotations

from quotemux.source_packages.environment import ensure_package_environment
from quotemux.source_packages.registry import get_default_source_package_registry

manifest = get_default_source_package_registry().get_manifest("crawler_provider")
ensure_package_environment(manifest)
PY
    fi
    python_path="$(find "$PACKAGE_VENV_ROOT" -maxdepth 2 -path '*/crawler_provider-*/bin/python' -type f 2>/dev/null | sort | tail -n 1 || true)"
    if [ "$python_path" = "" ]; then
        return
    fi
    if "$python_path" - <<'PY' >/dev/null 2>&1
from __future__ import annotations

from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    browser.close()
PY
    then
        return
    fi
    log "预处理：安装 crawler_provider 所需 Playwright Chromium"
    "$python_path" -m playwright install chromium
}

is_open_trade_date() {
    local calendar_path="$RUN_ROOT/calendar-$RUN_ID.json"
    curl --fail --silent --show-error --connect-timeout 10 --max-time 120 \
        "$MARKETHUB_BASE_URL/api/markets/calendar/trading?exchange=SSE&start_date=$TRADE_DATE&end_date=$TRADE_DATE&is_open=true" \
        -o "$calendar_path"
    python3 - "$calendar_path" "$TRADE_DATE" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

calendar_path = Path(sys.argv[1])
trade_date = sys.argv[2]
payload = json.loads(calendar_path.read_text(encoding="utf-8"))
if not isinstance(payload, list):
    raise SystemExit(1)
for item in payload:
    if not isinstance(item, dict):
        continue
    if item.get("trade_date") == trade_date and item.get("is_open") is True:
        raise SystemExit(0)
raise SystemExit(1)
PY
}

write_skipped_result() {
    python3 - "$RESULT_PATH" "$TRADE_DATE" <<'PY'
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

result_path = Path(sys.argv[1])
trade_date = sys.argv[2]
payload = {
    "status": "skipped",
    "trade_date": trade_date,
    "candidate_count": 0,
    "row_count": 0,
    "items": [],
    "error_message": "非开盘日，跳过涨跌停封单额采集",
    "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
}
result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

core_execute() {
    log "核心执行：触发涨跌停封单额专用采集"
    local url="$MARKETHUB_BASE_URL/api/admin/capture/limit-order-amount/run-today"
    url="$url?trade_date=$TRADE_DATE"
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
    if ! is_open_trade_date; then
        log "预处理：$TRADE_DATE 不是开盘日，跳过采集"
        write_skipped_result
        postprocess
        return
    fi
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
