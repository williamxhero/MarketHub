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
MARKETHUB_CODE_ROOT="${MARKETHUB_CODE_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
# 因子只在冻结基准日之后才需要每日追加；基准日当天及之前由历史预热任务负责，
# 这里不重新扫描全部历史，避免每晚全表扫描 fact.stock_daily_1d。
MARKETHUB_ADJ_FACTOR_DAILY_SINCE_DATE="${MARKETHUB_ADJ_FACTOR_DAILY_SINCE_DATE:-${QUOTEMUX_ADJUSTMENT_BASE_DATE:-}}"
RUN_ROOT="${MARKETHUB_ADJ_FACTOR_UPDATE_ROOT:-$RUNTIME_ROOT/adj-factor-update}"
LOG_ROOT="${MARKETHUB_LOG_ROOT:-$RUNTIME_ROOT/logs}"
RUN_ID="$(date '+%Y%m%d_%H%M%S')"
RESULT_DIR="$RUN_ROOT/results"
RESULT_PATH="$RESULT_DIR/$RUN_ID.json"
LOG_PATH="$LOG_ROOT/adj-factor-daily-update.log"

export PYTHONPATH="${PYTHONPATH:-}${PYTHONPATH:+:}$MARKETHUB_CODE_ROOT/QuoteMux/src:$MARKETHUB_CODE_ROOT/MarketHub/services/markethub_api/src"

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1"
}

preprocess() {
    mkdir -p "$RESULT_DIR" "$LOG_ROOT"
    test -x "$MARKETHUB_PYTHON"
    if [ -z "$MARKETHUB_ADJ_FACTOR_DAILY_SINCE_DATE" ]; then
        log "缺少 QUOTEMUX_ADJUSTMENT_BASE_DATE / MARKETHUB_ADJ_FACTOR_DAILY_SINCE_DATE，拒绝执行"
        return 64
    fi
    curl --fail --silent --show-error --connect-timeout 10 --max-time 30 "$MARKETHUB_BASE_URL/api/health" >/dev/null
}

core_execute() {
    "$MARKETHUB_PYTHON" "$MARKETHUB_CODE_ROOT/MarketHub/scripts/maintenance/backfill_tushare_adj_factor_snapshots.py" \
        --env-file "$ENV_PATH" \
        daily \
        --since-date "$MARKETHUB_ADJ_FACTOR_DAILY_SINCE_DATE" \
        --output-dir "$RUN_ROOT/artifacts" \
        > "$RESULT_PATH"
}

postprocess() {
    "$MARKETHUB_PYTHON" - "$RESULT_PATH" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

result_path = Path(sys.argv[1])
payload = json.loads(result_path.read_text(encoding="utf-8"))
if not isinstance(payload, dict) or "attempted" not in payload:
    raise SystemExit("复权因子每日更新返回值缺少 attempted 字段")
print(
    "adj_factor_daily_status="
    f"attempted={payload['attempted']} succeeded={payload['succeeded']} failed={payload['failed']}"
)
if int(payload["failed"]) > 0:
    failed_dates = ",".join(
        str(item["trade_date"]) for item in payload.get("results", []) if item.get("status") == "failed"
    )
    raise SystemExit(f"复权因子每日更新存在失败交易日，需要重试: {failed_dates}")
PY
}

main() {
    log "预处理：检查运行环境与 API"
    preprocess
    log "核心执行：补齐 $MARKETHUB_ADJ_FACTOR_DAILY_SINCE_DATE 之后缺失的复权因子交易日"
    core_execute
    log "后处理：校验每日复权因子结果"
    postprocess
    log "完成复权因子每日更新 result=$RESULT_PATH"
}

main 2>&1 | tee -a "$LOG_PATH"
