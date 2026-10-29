#!/usr/bin/env bash
set -euo pipefail

PREVIOUS_SERVICE="markethub-stock-money-flow-backfill.service"

while true; do
    if systemctl is-active --quiet "$PREVIOUS_SERVICE"; then
        sleep 30
    else
        query_status=$?
        if [[ "$query_status" -eq 3 ]]; then
            break
        fi
        echo "stock_market_indicators_backfill=query_error operator=systemctl query=is-active service=$PREVIOUS_SERVICE exit_code=$query_status" >&2
        exit "$query_status"
    fi
done

if result=$(systemctl show "$PREVIOUS_SERVICE" -p Result --value); then
    :
else
    query_status=$?
    echo "stock_market_indicators_backfill=query_error operator=systemctl query=show service=$PREVIOUS_SERVICE exit_code=$query_status" >&2
    exit "$query_status"
fi

if [[ "$result" != "success" ]]; then
    echo "stock_market_indicators_backfill=blocked money_flow_result=$result" >&2
    exit 1
fi

exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run_stock_market_indicators_backfill.sh"
