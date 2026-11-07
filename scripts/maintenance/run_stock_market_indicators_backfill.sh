#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="${MARKETHUB_RUNTIME_ROOT:-}"
if [[ "$RUNTIME_ROOT" != /* || "$RUNTIME_ROOT" == */ ]]; then
    echo "MARKETHUB_RUNTIME_ROOT must be an explicit absolute directory" >&2
    exit 64
fi
PYTHON_BIN="${MARKETHUB_PYTHON:-$RUNTIME_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "MARKETHUB_PYTHON is not executable: $PYTHON_BIN" >&2
    exit 64
fi
# Operator-owned bounds and durable state are mandatory; never default to a full backfill.
START_DATE="${MARKETHUB_MARKET_INDICATORS_START_DATE:-}"
END_DATE="${MARKETHUB_MARKET_INDICATORS_END_DATE:-}"
STATE_PATH="${MARKETHUB_MARKET_INDICATORS_STATE_PATH:-}"
BATCH_DAYS="${MARKETHUB_MARKET_INDICATORS_BATCH_DAYS:-5}"
SLEEP_SECONDS="${MARKETHUB_MARKET_INDICATORS_SLEEP_SECONDS:-5}"

for bound in "$START_DATE" "$END_DATE"; do
    if ! [[ "$bound" =~ ^[0-9]{8}$ ]] ||
        [[ "$(date -d "${bound:0:4}-${bound:4:2}-${bound:6:2}" +%Y%m%d 2>/dev/null)" != "$bound" ]]; then
        echo "Invalid or missing explicit start/end date (YYYYMMDD)" >&2
        exit 64
    fi
done
if [[ "$START_DATE" > "$END_DATE" ]]; then
    echo "start date must not follow end date" >&2
    exit 64
fi
if [[ "$STATE_PATH" != /* || "$STATE_PATH" == */ || -d "$STATE_PATH" ]] ||
    [[ -e "$STATE_PATH" && ! -f "$STATE_PATH" ]]; then
    echo "An explicit absolute state file path is required" >&2
    exit 64
fi
if ! mkdir -p -- "$(dirname "$STATE_PATH")"; then
    echo "Invalid state parent directory" >&2
    exit 64
fi

# Same advisory lock as the stock-bar maintenance writers; hold across all batches.
LOCK_PATH="${MARKETHUB_STOCK_BAR_MAINTENANCE_LOCK:-$RUNTIME_ROOT/audit/stock-bar-maintenance.lock}"
mkdir -p -- "$(dirname "$LOCK_PATH")"
exec 9>>"$LOCK_PATH"
if ! flock -n 9; then
    echo "stock-bar maintenance writer already active" >&2
    exit 75
fi

while true; do
    output=$("$PYTHON_BIN" "$SCRIPT_DIR/backfill_stock_market_indicators.py" --start-date "$START_DATE" --end-date "$END_DATE" --batch-days "$BATCH_DAYS" --state-path "$STATE_PATH")
    printf '%s\n' "$output"
    if [[ "$output" == *"stock_market_indicators_backfill=complete"* ]]; then
        exit 0
    fi
    sleep "$SLEEP_SECONDS"
done
