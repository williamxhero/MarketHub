#!/usr/bin/env bash
set -euo pipefail

API_SERVICE="markethub-api.service"
API_HEALTH_URL="${MARKETHUB_API_HEALTH_URL:-http://127.0.0.1:8803/api/health}"
MEMORY_RESTART_BYTES="${MARKETHUB_BACKFILL_MEMORY_RESTART_BYTES:-8589934592}"
FAILURE_ALERT_AGE_SECONDS="${MARKETHUB_BACKFILL_FAILURE_ALERT_AGE_SECONDS:-86400}"
HEALTH_WAIT_SECONDS="${MARKETHUB_BACKFILL_HEALTH_WAIT_SECONDS:-180}"
LOG_PATH="${MARKETHUB_BACKFILL_MONITOR_LOG_PATH:-/data/markethub/logs/stock_backfill_monitor.log}"
BACKFILL_SERVICES=(
    "markethub-stock-money-flow-backfill.service"
    "markethub-stock-market-indicators-backfill.service"
    "markethub-stock-margin-backfill.service"
    "markethub-stock-finance-events-backfill.service"
    "markethub-stock-industry-membership-backfill.service"
    "markethub-stock-1m-annual-import.service"
    "markethub-stock-5m-backfill.service"
    "news-crawler-cninfo-history-backfill.service"
    "markethub-stock-history-audit.service"
    "markethub-supermind-concept-history-import.service"
)

emit() {
    local message="$1"
    local line
    line="$(date --iso-8601=seconds) ${message}"
    mkdir -p "$(dirname "$LOG_PATH")"
    printf '%s\n' "$line" | tee -a "$LOG_PATH"
}

service_state() {
    local service="$1"
    systemctl show "$service" -p ActiveState -p SubState -p Result --value | paste -sd '/' -
}

state_summary() {
    /data/markethub/.venv/bin/python - <<'PY'
import json
from pathlib import Path

specs = (
    ("money_flow", Path("/data/markethub/store/backfill_state/stock_money_flow.json"), "completed_trade_dates"),
    ("market_indicators", Path("/data/markethub/store/backfill_state/stock_market_indicators.json"), "completed_trade_dates"),
    ("margin", Path("/data/markethub/store/backfill_state/stock_margin.json"), "completed_trade_dates"),
    ("finance_events", Path("/data/markethub/store/backfill_state/stock_finance_events.json"), "completed_announce_dates"),
    ("industry", Path("/data/markethub/store/backfill_state/stock_industry_membership.json"), "completed_board_codes"),
    ("stock_5m", Path("/data/markethub/store/backfill_state/stock_bar_5m.json"), "completed_trade_dates"),
    ("cninfo", Path("/data/news-crawler/backfill_state/cninfo_history_pipeline.json"), "completed_announcement_dates"),
)
parts = []
for label, path, key in specs:
    if not path.is_file():
        parts.append(f"{label}=absent")
        continue
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get(key, [])
    last = values[-1] if values else ""
    parts.append(f"{label}={len(values)}@{last}")
annual_state_root = Path("/data/markethub/import_1m_annual/state")
annual_states = sorted(annual_state_root.glob("*/*.json")) if annual_state_root.is_dir() else []
annual_last = annual_states[-1].stem if annual_states else ""
parts.append(f"annual_1m={len(annual_states)}@{annual_last}")
print(" ".join(parts))
PY
}

active_backfill_service() {
    local service
    for service in "${BACKFILL_SERVICES[@]}"; do
        if systemctl is-active --quiet "$service"; then
            printf '%s\n' "$service"
            return 0
        fi
    done
    return 1
}

failed_backfill_service() {
    local service result load_state exit_timestamp exit_epoch now age
    for service in "${BACKFILL_SERVICES[@]}"; do
        load_state="$(systemctl show "$service" -p LoadState --value)"
        if [[ "$load_state" != "loaded" ]]; then
            emit "backfill_monitor=skip_service service=${service} load_state=${load_state}"
            continue
        fi
        result="$(systemctl show "$service" -p Result --value)"
        if [[ "$result" != "success" ]]; then
            exit_timestamp="$(systemctl show "$service" -p ExecMainExitTimestamp --value)"
            exit_epoch="$(date --date="$exit_timestamp" +%s 2>/dev/null || true)"
            now="$(date +%s)"
            if [[ "$exit_epoch" =~ ^[0-9]+$ ]] && (( now - exit_epoch <= FAILURE_ALERT_AGE_SECONDS )); then
                printf '%s/%s\n' "$service" "$result"
                return 0
            fi
            age="unknown"
            if [[ "$exit_epoch" =~ ^[0-9]+$ ]]; then
                age="$((now - exit_epoch))"
            fi
            emit "backfill_monitor=stale_failed service=${service} result=${result} age_seconds=${age}"
        fi
    done
    return 1
}

wait_for_api_health() {
    local deadline=$((SECONDS + HEALTH_WAIT_SECONDS))
    while (( SECONDS < deadline )); do
        if curl --fail --silent --show-error --max-time 10 "$API_HEALTH_URL" >/dev/null; then
            return 0
        fi
        sleep 5
    done
    return 1
}

main() {
    local service states progress memory_current memory_peak
    local running_service=""
    local failed_service=""
    if ! [[ "$FAILURE_ALERT_AGE_SECONDS" =~ ^[0-9]+$ ]]; then
        emit "backfill_monitor=failed reason=invalid_failure_alert_age value=${FAILURE_ALERT_AGE_SECONDS}"
        return 1
    fi
    states=""
    for service in "${BACKFILL_SERVICES[@]}"; do
        states+="${service}:$(service_state "$service") "
    done
    memory_current="$(systemctl show "$API_SERVICE" -p MemoryCurrent --value)"
    memory_peak="$(systemctl show "$API_SERVICE" -p MemoryPeak --value)"
    progress="$(state_summary)"
    emit "backfill_monitor=sample api_memory_current=${memory_current} api_memory_peak=${memory_peak} services=${states% } progress=${progress}"

    if failed_service="$(failed_backfill_service)"; then
        emit "backfill_monitor=failed service=${failed_service}"
        return 1
    fi

    if ! [[ "$memory_current" =~ ^[0-9]+$ ]]; then
        emit "backfill_monitor=failed reason=invalid_api_memory_current value=${memory_current}"
        return 1
    fi
    if (( memory_current < MEMORY_RESTART_BYTES )); then
        return 0
    fi
    if ! running_service="$(active_backfill_service)"; then
        emit "backfill_monitor=skip_restart reason=no_active_backfill api_memory_current=${memory_current}"
        return 0
    fi

    emit "backfill_monitor=api_restart reason=memory_threshold active_backfill=${running_service} api_memory_current=${memory_current} threshold=${MEMORY_RESTART_BYTES}"
    systemctl kill --kill-who=all --signal=KILL "$API_SERVICE"
    if wait_for_api_health; then
        emit "backfill_monitor=api_recovered active_backfill=${running_service}"
        return 0
    fi

    emit "backfill_monitor=failed reason=api_health_timeout active_backfill=${running_service}"
    systemctl kill --kill-who=all --signal=TERM "$running_service"
    return 1
}

main "$@"
