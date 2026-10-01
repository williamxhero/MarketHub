#!/usr/bin/env bash
set -Eeuo pipefail
exec /data/markethub/.venv/bin/python /data/markethub/scripts/reconcile-stock-intraday-suspensions.py
