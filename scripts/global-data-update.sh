#!/usr/bin/env bash
set -Eeuo pipefail

# 全局数据更新入口：Task Center 可以多次调用；脚本按覆盖结果决定是否需要补跑。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="${MARKETHUB_RUNTIME_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_PATH="${MARKETHUB_ENV_PATH:-$RUNTIME_ROOT/env/markethub.env}"
if [ ! -f "$ENV_PATH" ] && [ -f "/data/markethub/env/markethub.env" ]; then
    ENV_PATH="/data/markethub/env/markethub.env"
fi
if [ -f "$ENV_PATH" ]; then
    set -a
    . "$ENV_PATH"
    set +a
fi

MARKETHUB_HOST="${MARKETHUB_HOST:-127.0.0.1}"
MARKETHUB_PORT="${MARKETHUB_PORT:-8803}"
MARKETHUB_BASE_URL="${MARKETHUB_BASE_URL:-http://$MARKETHUB_HOST:$MARKETHUB_PORT}"
RUN_ROOT="${MARKETHUB_DATA_UPDATE_ROOT:-$RUNTIME_ROOT/data-update}"
LOG_ROOT="${MARKETHUB_LOG_ROOT:-$RUNTIME_ROOT/logs}"
LOCK_PATH="$RUN_ROOT/global-data-update.lock"
RUN_ID="$(date '+%Y%m%d_%H%M%S')"
RUN_STARTED_AT="$(date '+%Y-%m-%d %H:%M:%S')"
RESULT_DIR="$RUN_ROOT/results"
RESULT_PATH="$RESULT_DIR/$RUN_ID.json"
VALIDATION_PATH="$RESULT_DIR/$RUN_ID.validation.json"
LOG_PATH="$LOG_ROOT/global-data-update.log"
DB_HOST="${MARKETHUB_DB_HOST:-127.0.0.1}"
DB_PORT="${MARKETHUB_DB_PORT:-55432}"
DB_NAME="${MARKETHUB_DB_NAME:-markethub_dev}"
DB_USER="${MARKETHUB_DB_USER:-markethub}"
DB_PASSWORD="${MARKETHUB_DB_PASSWORD:-markethub_dev_password}"
CAPTURE_WAIT_SECONDS="${MARKETHUB_CAPTURE_WAIT_SECONDS:-21600}"
CAPTURE_POLL_SECONDS="${MARKETHUB_CAPTURE_POLL_SECONDS:-60}"
CAPTURE_START_GRACE_SECONDS="${MARKETHUB_CAPTURE_START_GRACE_SECONDS:-120}"

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1"
}

psql_scalar() {
    PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -A -c "$1"
}

preprocess() {
    log "预处理：准备目录并检查 MarketHub"
    mkdir -p "$RESULT_DIR" "$LOG_ROOT"
    curl --fail --silent --show-error --connect-timeout 10 --max-time 30 "$MARKETHUB_BASE_URL/api/health" >/dev/null
    curl --fail --silent --show-error --connect-timeout 10 --max-time 60 "$MARKETHUB_BASE_URL/api/admin/capture-policies" >/dev/null

    active_runs="$(psql_scalar "select count(*) from capability_capture_runs where status = 'running'")"
    if [ "$active_runs" != "0" ]; then
        log "已有 capability 数据更新正在运行：$active_runs"
        exit 1
    fi
}

submit_due_capture() {
    log "核心执行：提交到期 capability 数据更新"
    curl --fail --silent --show-error --connect-timeout 10 --max-time 60 -X POST "$MARKETHUB_BASE_URL/api/admin/capture/run-due-async" -o "$RESULT_PATH"
    python3 - "$RESULT_PATH" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit("capture 后台提交返回值不是对象")
accepted = payload.get("accepted", False)
print(f"accepted={accepted}")
if accepted is not True:
    raise SystemExit("capture 后台提交未被接受")
PY
}

capture_summary() {
    psql_scalar "
        select
            count(*)::text || '|' ||
            count(*) filter (where status = 'running')::text || '|' ||
            count(*) filter (where status = 'failed')::text || '|' ||
            count(*) filter (where status = 'success')::text || '|' ||
            count(*) filter (where status = 'skipped')::text
        from capability_capture_runs
        where started_at >= timestamp '$RUN_STARTED_AT'
    "
}

wait_capture_completion() {
    log "后处理：等待本轮 capture 完成"
    local start_epoch
    start_epoch="$(date +%s)"
    while true; do
        local summary total running failed success skipped now_epoch elapsed
        summary="$(capture_summary)"
        IFS='|' read -r total running failed success skipped <<< "$summary"
        now_epoch="$(date +%s)"
        elapsed=$((now_epoch - start_epoch))
        log "capture 状态：total=$total running=$running success=$success failed=$failed skipped=$skipped elapsed=${elapsed}s"

        if [ "$total" != "0" ] && [ "$running" = "0" ]; then
            if [ "$failed" != "0" ]; then
                log "本轮 capture 出现失败任务：$failed"
                return 1
            fi
            return 0
        fi
        if [ "$total" = "0" ] && [ "$elapsed" -ge "$CAPTURE_START_GRACE_SECONDS" ]; then
            log "本轮没有新的到期 capture 任务"
            return 0
        fi
        if [ "$elapsed" -ge "$CAPTURE_WAIT_SECONDS" ]; then
            log "等待 capture 完成超时"
            return 1
        fi
        sleep "$CAPTURE_POLL_SECONDS"
    done
}

validate_market_coverage() {
    local validation_path="$1"
    local status
    log "后处理：校验上一交易日核心数据覆盖"
    status=0
    python3 - "$MARKETHUB_BASE_URL" "$validation_path" <<'PY' || status=$?
from __future__ import annotations

from datetime import datetime
import json
import os
import sys
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo


def fetch_json(base_url: str, path: str, params: dict[str, object]) -> object:
    url = f"{base_url}{path}?{urlencode(params)}"
    with urlopen(url, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def item_count(payload: object) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return len(payload["items"])
    return 0


def env_int(name: str, default: int) -> int:
    value = os.getenv(name, "")
    if value == "":
        return default
    return int(value)


base_url = sys.argv[1]
validation_path = sys.argv[2]
today_text = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
previous_payload = fetch_json(base_url, "/api/markets/calendar/trading/previous", {"date": today_text, "count": 1})
if not isinstance(previous_payload, list) or previous_payload == []:
    raise SystemExit("上一交易日接口返回为空")
trade_date = str(previous_payload[0].get("trade_date", ""))
if trade_date == "":
    raise SystemExit("上一交易日接口缺少 trade_date")

checks = [
    {
        "name": "previous_trading_day",
        "path": "/api/markets/calendar/trading/previous",
        "params": {"date": today_text, "count": 1},
        "minimum": 1,
        "blocking": True,
        "capabilities": ["markets.calendar.trading"],
    },
    {
        "name": "stock_daily_snapshot",
        "path": "/api/stocks/quotes/daily-snapshot",
        "params": {"trade_date": trade_date, "skip_suspended": "true", "skip_st": "true", "limit": 10000},
        "minimum": env_int("MARKETHUB_MIN_STOCK_DAILY_ROWS", 3000),
        "blocking": True,
        "capabilities": ["stocks.quotes.daily_snapshot"],
    },
    {
        "name": "stock_daily_local_window",
        "path": "/api/stocks/quotes/daily-local-window",
        "params": {"start_date": trade_date, "end_date": trade_date, "skip_suspended": "true", "skip_st": "true", "limit": 50000, "fields": "code,trade_time,close,pct_chg,amount"},
        "minimum": env_int("MARKETHUB_MIN_STOCK_DAILY_ROWS", 3000),
        "blocking": True,
        "capabilities": ["stocks.quotes.daily_snapshot"],
    },
    {
        "name": "concept_daily_snapshot",
        "path": "/api/concepts/quotes/daily-snapshot",
        "params": {"trade_date": trade_date, "limit": 5000},
        "minimum": env_int("MARKETHUB_MIN_CONCEPT_DAILY_ROWS", 200),
        "blocking": True,
        "capabilities": ["concepts.quotes.daily"],
    },
    {
        "name": "index_quotes",
        "path": "/api/indexes/quotes",
        "params": {"index_codes": "000001,399001,399006,000300,000905,000852,899050", "trade_date": trade_date, "limit": 1000},
        "minimum": env_int("MARKETHUB_MIN_INDEX_QUOTE_ROWS", 1),
        "blocking": True,
        "capabilities": ["indexes.quotes.daily"],
    },
    {
        "name": "main_capital_flow",
        "path": "/api/markets/indicators/main-capital-flow",
        "params": {"trade_date": trade_date},
        "minimum": env_int("MARKETHUB_MIN_MAIN_CAPITAL_FLOW_ROWS", 1),
        "blocking": True,
        "capabilities": ["markets.indicators.main_capital_flow"],
    },
    {
        "name": "connect_capital_flow",
        "path": "/api/markets/connect/capital-flow",
        "params": {"trade_date": trade_date},
        "minimum": env_int("MARKETHUB_MIN_CONNECT_CAPITAL_FLOW_ROWS", 1),
        "blocking": False,
        "capabilities": ["markets.connect.capital_flow"],
    },
    {
        "name": "connect_active_top10",
        "path": "/api/markets/connect/active-top10",
        "params": {"trade_date": trade_date, "limit": 1000},
        "minimum": env_int("MARKETHUB_MIN_CONNECT_ACTIVE_TOP10_ROWS", 1),
        "blocking": False,
        "capabilities": ["markets.connect.active_top10"],
    },
    {
        "name": "dragon_tiger",
        "path": "/api/markets/participants/dragon-tiger",
        "params": {"trade_date": trade_date, "limit": 1000},
        "minimum": env_int("MARKETHUB_MIN_DRAGON_TIGER_ROWS", 1),
        "blocking": False,
        "capabilities": ["markets.participants.dragon_tiger"],
    },
    {
        "name": "dragon_tiger_institutions",
        "path": "/api/markets/participants/dragon-tiger/institutions",
        "params": {"trade_date": trade_date, "limit": 1000},
        "minimum": env_int("MARKETHUB_MIN_DRAGON_TIGER_INSTITUTION_ROWS", 1),
        "blocking": False,
        "capabilities": ["markets.participants.dragon_tiger.institutions"],
    },
    {
        "name": "c231_members",
        "path": "/api/concepts/C231/members",
        "params": {"trade_date": trade_date},
        "minimum": env_int("MARKETHUB_MIN_C231_MEMBER_ROWS", 1),
        "blocking": True,
        "capabilities": ["concepts.members"],
    },
    {
        "name": "hot_money_details",
        "path": "/api/markets/participants/hot-money/details",
        "params": {"trade_date": trade_date, "limit": 300},
        "minimum": env_int("MARKETHUB_MIN_HOT_MONEY_DETAIL_ROWS", 0),
        "blocking": False,
        "capabilities": ["markets.participants.hot_money.details"],
    },
    {
        "name": "open_auctions",
        "path": "/api/markets/trading/open-auctions",
        "params": {"trade_date": trade_date, "limit": 1000},
        "minimum": env_int("MARKETHUB_MIN_OPEN_AUCTION_ROWS", 1),
        "blocking": False,
        "capabilities": ["markets.trading.open_auctions"],
    },
    {
        "name": "limit_order_amount",
        "path": "/api/stocks/signals/limit-order-amount",
        "params": {"trade_date": trade_date},
        "minimum": env_int("MARKETHUB_MIN_LIMIT_ORDER_AMOUNT_ROWS", 1),
        "blocking": False,
        "capabilities": ["stocks.signals.limit_order_amount"],
    },
    {
        "name": "news_events",
        "path": "/api/markets/events/news",
        "params": {"trade_date": trade_date, "limit": 200},
        "minimum": env_int("MARKETHUB_MIN_NEWS_EVENT_ROWS", 0),
        "blocking": False,
        "capabilities": ["markets.events.news"],
    },
]

results: list[dict[str, object]] = []
failures: list[str] = []
failed_capabilities: list[str] = []
warnings: list[str] = []
warning_capabilities: list[str] = []
for check in checks:
    payload = fetch_json(base_url, str(check["path"]), dict(check["params"]))
    count = item_count(payload)
    minimum = int(check["minimum"])
    ok = count >= minimum
    result = {**check, "count": count, "ok": ok}
    results.append(result)
    print(f"{check['name']} count={count} minimum={minimum} blocking={check['blocking']}")
    if not ok and bool(check["blocking"]):
        failures.append(f"{check['name']} count={count} minimum={minimum}")
        failed_capabilities.extend(str(item) for item in check["capabilities"])
    elif not ok:
        warnings.append(f"{check['name']} count={count} minimum={minimum}")
        warning_capabilities.extend(str(item) for item in check["capabilities"])

deduped_capabilities = list(dict.fromkeys(failed_capabilities))
deduped_warning_capabilities = list(dict.fromkeys(warning_capabilities))
refresh_capabilities = list(dict.fromkeys([*failed_capabilities, *warning_capabilities]))
with open(validation_path, "w", encoding="utf-8") as file:
    json.dump(
        {
            "trade_date": trade_date,
            "results": results,
            "failures": failures,
            "failed_capabilities": deduped_capabilities,
            "warning_capabilities": deduped_warning_capabilities,
            "refresh_capabilities": refresh_capabilities,
            "warnings": warnings,
        },
        file,
        ensure_ascii=False,
        indent=2,
    )

if failures:
    raise SystemExit("核心数据覆盖不足：" + "; ".join(failures))
PY
    log "后处理：覆盖校验结果 $validation_path"
    return "$status"
}

failed_capabilities() {
    python3 - "$1" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for capability_id in payload.get("refresh_capabilities", payload.get("failed_capabilities", [])):
    print(str(capability_id))
PY
}

validation_trade_date() {
    python3 - "$1" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(str(payload.get("trade_date", "")))
PY
}

force_refresh_failed_capabilities() {
    local validation_path="$1"
    local capabilities
    local trade_date
    local compact_trade_date
    capabilities="$(failed_capabilities "$validation_path")"
    if [ "$capabilities" = "" ]; then
        log "后处理：没有可强制补跑的 capability"
        return 0
    fi
    trade_date="$(validation_trade_date "$validation_path")"
    compact_trade_date="${trade_date//-/}"

    while IFS= read -r capability_id; do
        if [ "$capability_id" = "" ]; then
            continue
        fi
        local force_result_path
        force_result_path="$RESULT_DIR/$RUN_ID.force-${capability_id//./_}.json"
        log "后处理：强制补跑 capability $capability_id"
        curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" -X POST "$MARKETHUB_BASE_URL/api/admin/capture-runs/$capability_id" -o "$force_result_path"
        python3 - "$force_result_path" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
status = str(payload.get("status", ""))
print(f"forced_status={status}")
if status not in {"success", "skipped"}:
    raise SystemExit(f"强制补跑失败: {status}")
PY
        if [ "$compact_trade_date" != "" ] && [ "$capability_id" = "stocks.quotes.daily_snapshot" ]; then
            log "后处理：按校验日期精确补股票日快照 $trade_date"
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/stocks/quotes/daily-snapshot?trade_date=$compact_trade_date&skip_suspended=true&skip_st=true&limit=10000" -o "$force_result_path.targeted.json"
        fi
        if [ "$compact_trade_date" != "" ] && [ "$capability_id" = "concepts.quotes.daily" ]; then
            log "后处理：按校验日期精确补题材概念日快照 $trade_date"
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/concepts/quotes/daily-snapshot?trade_date=$compact_trade_date&limit=5000" -o "$force_result_path.targeted.json"
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "indexes.quotes.daily" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/indexes/quotes?index_codes=000001,399001,399006,000300,000905,000852,899050&trade_date=$trade_date&limit=1000" -o "$force_result_path.targeted.json"
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "markets.indicators.main_capital_flow" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/markets/indicators/main-capital-flow?trade_date=$trade_date" -o "$force_result_path.targeted.json"
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "markets.connect.capital_flow" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/markets/connect/capital-flow?trade_date=$trade_date" -o "$force_result_path.targeted.json"
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "markets.connect.active_top10" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/markets/connect/active-top10?trade_date=$trade_date&limit=1000" -o "$force_result_path.targeted.json"
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "markets.participants.dragon_tiger" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/markets/participants/dragon-tiger?trade_date=$trade_date&limit=1000" -o "$force_result_path.targeted.json"
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "markets.participants.dragon_tiger.institutions" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/markets/participants/dragon-tiger/institutions?trade_date=$trade_date&limit=1000" -o "$force_result_path.targeted.json"
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "markets.participants.hot_money.details" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/markets/participants/hot-money/details?trade_date=$trade_date&limit=1000" -o "$force_result_path.targeted.json"
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "markets.trading.open_auctions" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/markets/trading/open-auctions?trade_date=$trade_date" -o "$force_result_path.targeted.json"
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "stocks.signals.limit_order_amount" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" -X POST "$MARKETHUB_BASE_URL/api/admin/capture/limit-order-amount/run-today?trade_date=$trade_date" -o "$force_result_path.targeted.json"
        fi
    done <<< "$capabilities"
}

postprocess() {
    if validate_market_coverage "$VALIDATION_PATH"; then
        log "后处理：覆盖已达标，跳过数据更新"
        return 0
    fi

    if [ ! -s "$VALIDATION_PATH" ]; then
        log "后处理：覆盖校验未生成结果，停止本轮更新"
        return 1
    fi

    if [ "$(failed_capabilities "$VALIDATION_PATH")" = "" ]; then
        log "后处理：覆盖校验没有阻塞项，停止本轮更新"
        return 1
    fi

    force_refresh_failed_capabilities "$VALIDATION_PATH"
    if validate_market_coverage "$VALIDATION_PATH"; then
        log "后处理：强制补跑后覆盖达标"
        return 0
    fi
    log "后处理：强制补跑后覆盖仍不足，停止本轮更新"
    return 1
}

run_once() {
    log "开始 MarketHub / QuoteMux 全局数据更新"
    preprocess
    postprocess
    log "完成 MarketHub / QuoteMux 全局数据更新"
}

main() {
    mkdir -p "$RUN_ROOT" "$LOG_ROOT"
    {
        flock -n 9 || { log "已有全局数据更新脚本正在运行"; exit 1; }
        run_once
    } 9>"$LOCK_PATH" 2>&1 | tee -a "$LOG_PATH"
}

main
