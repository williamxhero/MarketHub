#!/usr/bin/env bash
set -euo pipefail

PREVIOUS_SERVICE="markethub-stock-money-flow-backfill.service"

while systemctl is-active --quiet "$PREVIOUS_SERVICE"; do
    sleep 30
done

if [[ "$(systemctl show "$PREVIOUS_SERVICE" -p Result --value)" != "success" ]]; then
    echo "stock_market_indicators_backfill=blocked money_flow_result=$(systemctl show "$PREVIOUS_SERVICE" -p Result --value)" >&2
    exit 1
fi

exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run_stock_market_indicators_backfill.sh"
