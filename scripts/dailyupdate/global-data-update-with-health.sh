#!/usr/bin/env bash
set -Eeuo pipefail

# 04:00 全局数据更新入口：数据更新成功结束后，再执行数据健康检查。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="${MARKETHUB_RUNTIME_ROOT:-/data/markethub}"
ENV_PATH="${MARKETHUB_ENV_PATH:-$RUNTIME_ROOT/env/markethub.env}"
if [ -f "$ENV_PATH" ]; then
    set -a
    . "$ENV_PATH"
    set +a
fi
GLOBAL_DATA_UPDATE_SCRIPT="${MARKETHUB_GLOBAL_DATA_UPDATE_SCRIPT:-$SCRIPT_DIR/global-data-update.sh}"
DATA_HEALTH_SCRIPT="${MARKETHUB_DATA_HEALTH_SCRIPT:-$SCRIPT_DIR/data-health-check.sh}"
PARQUET_PUBLISHER_SCRIPT="${MARKETHUB_PARQUET_PUBLISHER_SCRIPT:-$SCRIPT_DIR/../publisher/publish_stock_daily_parquet.py}"
MARKETHUB_PYTHON="${MARKETHUB_PYTHON:-$RUNTIME_ROOT/.venv/bin/python}"
MARKETHUB_CODE_ROOT="${MARKETHUB_CODE_ROOT:-}"
MARKETHUB_EXPORT_ROOT="${MARKETHUB_EXPORT_ROOT:-/data/MarketHub2/exports}"
MARKETHUB_ENABLE_DAILY_PARQUET_PUBLISH="${MARKETHUB_ENABLE_DAILY_PARQUET_PUBLISH:-0}"
# Long-running catch-up captures must not block the health and publisher chain.
# The accepted background run remains observable through /api/admin/capture-runs.
MARKETHUB_HEALTH_CAPTURE_ENDPOINT="${MARKETHUB_HEALTH_CAPTURE_ENDPOINT:-/api/admin/capture/run-due-async}"

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1"
}

run_once() {
    log "开始 MarketHub 04:00 全局数据更新和数据健康检查"
    MARKETHUB_CAPTURE_ENDPOINT="$MARKETHUB_HEALTH_CAPTURE_ENDPOINT" "$GLOBAL_DATA_UPDATE_SCRIPT"
    log "全局数据更新完成，开始数据健康检查"
    "$DATA_HEALTH_SCRIPT"
    if [ "$MARKETHUB_ENABLE_DAILY_PARQUET_PUBLISH" = "1" ]; then
        log "数据健康检查通过，开始发布版本化 stock_daily_1d Parquet"
        if [ -z "$MARKETHUB_CODE_ROOT" ]; then
            log "缺少 MARKETHUB_CODE_ROOT，无法从当前 release 加载 publisher 依赖"
            return 1
        fi
        PYTHONPATH="$MARKETHUB_CODE_ROOT/QuoteMux/src:$MARKETHUB_CODE_ROOT/MarketHub/services/markethub_api/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$MARKETHUB_PYTHON" "$PARQUET_PUBLISHER_SCRIPT" \
            --export-root "$MARKETHUB_EXPORT_ROOT" \
            --compression "${MARKETHUB_PARQUET_COMPRESSION:-zstd}" \
            --row-group-mib "${MARKETHUB_PARQUET_ROW_GROUP_MIB:-64}"
    else
        log "版本化 Parquet 自动发布尚未启用"
    fi
    log "完成 MarketHub 04:00 全局数据更新和数据健康检查"
}

run_once
