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
MARKETHUB_DATA_HEALTH_FAILURE_POLICY="${MARKETHUB_DATA_HEALTH_FAILURE_POLICY:-warn}"
MARKETHUB_GLOBAL_UPDATE_LOCK_PATH="${MARKETHUB_GLOBAL_UPDATE_LOCK_PATH:-$RUNTIME_ROOT/locks/global-data-update.lock}"
MARKETHUB_GLOBAL_UPDATE_LOCK_TIMEOUT_SECONDS="${MARKETHUB_GLOBAL_UPDATE_LOCK_TIMEOUT_SECONDS:-60}"
# The publisher waits only for its declared dependency.  `run-due` can include
# historical repairs and unrelated long-running capabilities, so waiting for it
# turns a routine publication into an unbounded global backlog drain.
MARKETHUB_HEALTH_CAPTURE_ENDPOINT="${MARKETHUB_HEALTH_CAPTURE_ENDPOINT:-/api/admin/capture/run-due-async}"
MARKETHUB_GLOBAL_UPDATE_REQUIRED_CAPABILITIES="${MARKETHUB_GLOBAL_UPDATE_REQUIRED_CAPABILITIES:-stocks.quotes.daily_snapshot}"

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1"
}

acquire_global_update_lock() {
    case "$MARKETHUB_GLOBAL_UPDATE_LOCK_TIMEOUT_SECONDS" in
        ''|*[!0-9]*)
            log "无效的 MARKETHUB_GLOBAL_UPDATE_LOCK_TIMEOUT_SECONDS=$MARKETHUB_GLOBAL_UPDATE_LOCK_TIMEOUT_SECONDS"
            return 64
            ;;
    esac
    mkdir -p "$(dirname "$MARKETHUB_GLOBAL_UPDATE_LOCK_PATH")"
    exec {MARKETHUB_GLOBAL_UPDATE_LOCK_FD}>"$MARKETHUB_GLOBAL_UPDATE_LOCK_PATH"
    if ! flock -w "$MARKETHUB_GLOBAL_UPDATE_LOCK_TIMEOUT_SECONDS" "$MARKETHUB_GLOBAL_UPDATE_LOCK_FD"; then
        log "全局更新锁超时 path=$MARKETHUB_GLOBAL_UPDATE_LOCK_PATH timeout_seconds=$MARKETHUB_GLOBAL_UPDATE_LOCK_TIMEOUT_SECONDS；未启动重复采集或发布"
        return 75
    fi
    log "已获得全局更新锁 path=$MARKETHUB_GLOBAL_UPDATE_LOCK_PATH"
}

release_global_update_lock() {
    status=$?
    flock -u "$MARKETHUB_GLOBAL_UPDATE_LOCK_FD" || true
    log "释放全局更新锁 status=$status"
}

run_once() {
    health_alert=0
    log "开始 MarketHub 全局数据更新和数据健康检查 capture_endpoint=$MARKETHUB_HEALTH_CAPTURE_ENDPOINT"
    MARKETHUB_CAPTURE_ENDPOINT="$MARKETHUB_HEALTH_CAPTURE_ENDPOINT" \
        MARKETHUB_REQUIRED_CAPTURE_CAPABILITIES="$MARKETHUB_GLOBAL_UPDATE_REQUIRED_CAPABILITIES" \
        "$GLOBAL_DATA_UPDATE_SCRIPT"
    log "全局数据更新完成，开始数据健康检查和覆盖构建"
    case "$MARKETHUB_DATA_HEALTH_FAILURE_POLICY" in
        warn|fail)
            ;;
        *)
            log "无效的 MARKETHUB_DATA_HEALTH_FAILURE_POLICY=$MARKETHUB_DATA_HEALTH_FAILURE_POLICY"
            return 64
            ;;
    esac
    if "$DATA_HEALTH_SCRIPT"; then
        log "global_update_health_outcome=passed policy=$MARKETHUB_DATA_HEALTH_FAILURE_POLICY"
    else
        health_status=$?
        # A platform-wide health report contains durable quality alerts outside
        # this run's declared publication dependencies.  Keep that alert
        # observable, but do not make a successful serialized capture look
        # failed merely because unrelated historical remediation remains.
        log "global_update_health_outcome=alert policy=$MARKETHUB_DATA_HEALTH_FAILURE_POLICY status=$health_status retry_semantics=health_remediation"
        health_alert=1
        if [ "$MARKETHUB_DATA_HEALTH_FAILURE_POLICY" = "fail" ]; then
            return "$health_status"
        fi
    fi
    if [ "$MARKETHUB_ENABLE_DAILY_PARQUET_PUBLISH" = "1" ]; then
        if [ "$health_alert" = "1" ]; then
            log "global_update_publication_outcome=deferred reason=data_health_alert retry_semantics=health_remediation"
            log "完成 MarketHub 04:00 全局数据更新；Parquet 发布已因健康告警延后"
            return 0
        fi
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

acquire_global_update_lock || {
    status=$?
    if [ "$status" -eq 75 ]; then
        # A scheduled overlap is a successful no-op: an existing owner will
        # complete the same serialized pipeline.  Returning success prevents
        # Task Center from treating normal coalescing as a production failure.
        log "global_update_outcome=skipped reason=lock_busy retry_semantics=next_timer"
        exit 0
    fi
    exit "$status"
}
trap release_global_update_lock EXIT
run_once
