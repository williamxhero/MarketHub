#!/usr/bin/env bash
set -Eeuo pipefail

# 04:00 全局数据更新入口：数据更新成功结束后，再执行数据健康检查。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GLOBAL_DATA_UPDATE_SCRIPT="${MARKETHUB_GLOBAL_DATA_UPDATE_SCRIPT:-$SCRIPT_DIR/global-data-update.sh}"
DATA_HEALTH_SCRIPT="${MARKETHUB_DATA_HEALTH_SCRIPT:-$SCRIPT_DIR/data-health-check.sh}"

if [ ! -f "$GLOBAL_DATA_UPDATE_SCRIPT" ] && [ -f "/data/markethub/scripts/global-data-update.sh" ]; then
    GLOBAL_DATA_UPDATE_SCRIPT="/data/markethub/scripts/global-data-update.sh"
fi
if [ ! -f "$DATA_HEALTH_SCRIPT" ] && [ -f "/data/markethub/scripts/data-health-check.sh" ]; then
    DATA_HEALTH_SCRIPT="/data/markethub/scripts/data-health-check.sh"
fi

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1"
}

run_once() {
    log "开始 MarketHub 04:00 全局数据更新和数据健康检查"
    "$GLOBAL_DATA_UPDATE_SCRIPT"
    log "全局数据更新完成，开始数据健康检查"
    "$DATA_HEALTH_SCRIPT"
    log "完成 MarketHub 04:00 全局数据更新和数据健康检查"
}

run_once
