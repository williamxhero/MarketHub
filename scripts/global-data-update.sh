#!/usr/bin/env bash
set -Eeuo pipefail

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
WORKSPACE_ROOT="${MARKETHUB_PROJECT_ROOT:-$(cd "$RUNTIME_ROOT/.." && pwd)}"
if [ ! -d "$WORKSPACE_ROOT/QuoteMux" ] && [ -d "/data/MarketHub2/current/QuoteMux" ]; then
    WORKSPACE_ROOT="/data/MarketHub2/current"
fi
MARKETHUB_PYTHON="${MARKETHUB_PYTHON:-$WORKSPACE_ROOT/.venv/bin/python}"
if [ ! -x "$MARKETHUB_PYTHON" ] && [ -x "/data/MarketHub2/current/.venv/bin/python" ]; then
    MARKETHUB_PYTHON="/data/MarketHub2/current/.venv/bin/python"
fi
RUN_ROOT="${MARKETHUB_DATA_UPDATE_ROOT:-$RUNTIME_ROOT/data-update}"
LOG_ROOT="${MARKETHUB_LOG_ROOT:-$RUNTIME_ROOT/logs}"
LOCK_PATH="$RUN_ROOT/global-data-update.lock"
RUN_ID="$(date '+%Y%m%d_%H%M%S')"
RESULT_DIR="$RUN_ROOT/results"
VALIDATION_PATH="$RESULT_DIR/$RUN_ID.validation.json"
CAPTURE_RESULT_PATH="$RESULT_DIR/$RUN_ID.capture.json"
LOG_PATH="$LOG_ROOT/global-data-update.log"
DB_HOST="${MARKETHUB_DB_HOST:-127.0.0.1}"
DB_PORT="${MARKETHUB_DB_PORT:-55432}"
DB_NAME="${MARKETHUB_DB_NAME:-markethub_dev}"
DB_USER="${MARKETHUB_DB_USER:-markethub}"
DB_PASSWORD="${MARKETHUB_DB_PASSWORD:-markethub_dev_password}"
CAPTURE_WAIT_SECONDS="${MARKETHUB_CAPTURE_WAIT_SECONDS:-21600}"
CONCEPT_BACKFILL_WINDOW_COUNT="${MARKETHUB_CONCEPT_BACKFILL_WINDOW_COUNT:-7}"
CONCEPT_BACKFILL_SCRIPT="${MARKETHUB_CONCEPT_BACKFILL_SCRIPT:-$WORKSPACE_ROOT/MarketHub/scripts/backfill_concept_runtime_refs.py}"
if [ ! -f "$CONCEPT_BACKFILL_SCRIPT" ] && [ -f "/data/MarketHub2/current/MarketHub/scripts/backfill_concept_runtime_refs.py" ]; then
    CONCEPT_BACKFILL_SCRIPT="/data/MarketHub2/current/MarketHub/scripts/backfill_concept_runtime_refs.py"
fi
log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1"
}

psql_scalar() {
    PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -A -c "$1"
}

concept_fact_ref_missing() {
    "$MARKETHUB_PYTHON" - "$MARKETHUB_BASE_URL" <<'PY'
from __future__ import annotations

import json
import sys
from urllib.request import urlopen

base_url = sys.argv[1]
with urlopen(f"{base_url}/api/admin/runtime-health", timeout=60) as response:
    payload = json.loads(response.read().decode("utf-8"))
objects = payload.get("fact_ref_availability", {}).get("objects", [])
required = {"ref.concept", "ref.concept_stock_membership", "fact.concept_daily_1d"}
missing = {
    str(item.get("name", ""))
    for item in objects
    if str(item.get("name", "")) in required and item.get("exists") is not True
}
print("\n".join(sorted(missing)))
PY
}

repair_concept_fact_ref_if_needed() {
    local missing_objects
    missing_objects="$(concept_fact_ref_missing)"
    if [ "$missing_objects" = "" ]; then
        return 0
    fi
    log "预处理：检测到概念本地表缺失，先执行自愈回填"
    printf '%s\n' "$missing_objects" | while IFS= read -r line; do
        [ "$line" != "" ] && log "缺失对象：$line"
    done
    "$MARKETHUB_PYTHON" "$CONCEPT_BACKFILL_SCRIPT" --window-count "$CONCEPT_BACKFILL_WINDOW_COUNT"
}

previous_trading_day() {
    "$MARKETHUB_PYTHON" - "$MARKETHUB_BASE_URL" <<'PY'
from __future__ import annotations

from datetime import datetime
import json
import sys
from urllib.request import urlopen
from zoneinfo import ZoneInfo

base_url = sys.argv[1]
today_text = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
with urlopen(f"{base_url}/api/markets/calendar/trading/previous?date={today_text}&count=1", timeout=60) as response:
    payload = json.loads(response.read().decode("utf-8"))
if not isinstance(payload, list) or payload == []:
    print("")
else:
    print(str(payload[0].get("trade_date", "")))
PY
}

local_c231_membership_count() {
    local trade_date="$1"
    psql_scalar "
        with target_rows as (
            select m.concept_id, m.stock_market, m.stock_code, m.valid_from, m.valid_to
            from ref.concept_stock_membership m
            where m.concept_id = 'C231'
              and m.valid_from <= date '$trade_date'
              and (m.valid_to is null or m.valid_to >= date '$trade_date')
        ),
        fallback_date as (
            select max(m.valid_from) as valid_from
            from ref.concept_stock_membership m
            where m.concept_id = 'C231'
              and m.valid_from <= date '$trade_date'
        ),
        fallback_rows as (
            select m.concept_id, m.stock_market, m.stock_code, m.valid_from, m.valid_to
            from ref.concept_stock_membership m
            join fallback_date latest on latest.valid_from = m.valid_from
            where m.concept_id = 'C231'
              and not exists (select 1 from target_rows)
        )
        select count(*)
        from (
            select * from target_rows
            union all
            select * from fallback_rows
        ) rows
    "
}

repair_c231_membership_if_needed() {
    local trade_date
    local row_count
    trade_date="$(previous_trading_day)"
    if [ "$trade_date" = "" ]; then
        log "预处理：无法确定上一交易日，跳过 C231 本地成分检查"
        return 0
    fi
    row_count="$(local_c231_membership_count "$trade_date" | tr -d '[:space:]')"
    if [ "$row_count" != "0" ]; then
        return 0
    fi
    log "预处理：C231 本地成分缺失，执行定点回填 trade_date=$trade_date"
    "$MARKETHUB_PYTHON" "$CONCEPT_BACKFILL_SCRIPT" --window-count "$CONCEPT_BACKFILL_WINDOW_COUNT" --concept-id C231
}

preprocess() {
    log "预处理：准备目录并检查 MarketHub"
    mkdir -p "$RESULT_DIR" "$LOG_ROOT"
    curl --fail --silent --show-error --connect-timeout 10 --max-time 30 "$MARKETHUB_BASE_URL/api/health" >/dev/null
    curl --fail --silent --show-error --connect-timeout 10 --max-time 60 "$MARKETHUB_BASE_URL/api/admin/capture-policies" >/dev/null
    repair_concept_fact_ref_if_needed
    repair_c231_membership_if_needed

    active_runs="$(psql_scalar "select count(*) from capability_capture_runs where status = 'running'")"
    if [ "$active_runs" != "0" ]; then
        log "已有 capability 数据更新正在运行：$active_runs"
        exit 1
    fi
}

run_due_captures() {
    log "核心执行：同步运行到期 capability 数据更新"
    curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" -X POST "$MARKETHUB_BASE_URL/api/admin/capture/run-due" -o "$CAPTURE_RESULT_PATH"
    "$MARKETHUB_PYTHON" - "$CAPTURE_RESULT_PATH" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(payload, list):
    raise SystemExit("到期 capability 更新返回值不是列表")
failed = [item for item in payload if isinstance(item, dict) and str(item.get("status", "")) == "failed"]
print(f"due_capture_runs={len(payload)} failed={len(failed)}")
if failed:
    raise SystemExit("到期 capability 更新存在失败项")
PY
}

validate_market_coverage() {
    local validation_path="$1"
    local status
    log "后处理：校验上一交易日核心数据覆盖"
    status=0
    "$MARKETHUB_PYTHON" - "$MARKETHUB_BASE_URL" "$validation_path" "$DB_HOST" "$DB_PORT" "$DB_NAME" "$DB_USER" "$DB_PASSWORD" <<'PY' || status=$?
from __future__ import annotations

from datetime import datetime
import json
import os
import sys
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import psycopg


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


def sql_count(connection: psycopg.Connection, query: str, params: tuple[object, ...]) -> int:
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        row = cursor.fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


base_url = sys.argv[1]
validation_path = sys.argv[2]
db_host = sys.argv[3]
db_port = sys.argv[4]
db_name = sys.argv[5]
db_user = sys.argv[6]
db_password = sys.argv[7]
today_text = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
previous_payload = fetch_json(base_url, "/api/markets/calendar/trading/previous", {"date": today_text, "count": 1})
if not isinstance(previous_payload, list) or previous_payload == []:
    raise SystemExit("上一交易日接口返回为空")
trade_date = str(previous_payload[0].get("trade_date", ""))
if trade_date == "":
    raise SystemExit("上一交易日接口缺少 trade_date")

connection = psycopg.connect(
    host=db_host,
    port=int(db_port),
    dbname=db_name,
    user=db_user,
    password=db_password,
)

try:
    stock_local_window_count = sql_count(
        connection,
        """
        select count(*)
        from fact.stock_daily_1d day_rows
        where day_rows.trade_date = %s::date
          and (
            (day_rows.market = 'SHSE' and left(day_rows.code, 1) = '6')
            or (day_rows.market = 'BJSE' and left(day_rows.code, 1) in ('4', '8', '9'))
            or (day_rows.market = 'SZSE' and left(day_rows.code, 1) not in ('4', '6', '8', '9'))
          )
          and coalesce(day_rows.is_suspended, false) = false
          and coalesce(day_rows.is_st, false) = false
        """,
        (trade_date,),
    )
    concept_daily_snapshot_count = sql_count(
        connection,
        """
        select count(*)
        from fact.concept_daily_1d day_rows
        where day_rows.trade_date = %s::date
        """,
        (trade_date,),
    )
    stock_intraday_code_count = sql_count(
        connection,
        """
        select count(distinct bar_rows.code)
        from fact.stock_bar_1m bar_rows
        where bar_rows.bar_time >= %s::date
          and bar_rows.bar_time < %s::date + interval '1 day'
        """,
        (trade_date, trade_date),
    )
    stock_intraday_close_count = sql_count(
        connection,
        """
        select count(*)
        from (
            select bar_rows.market, bar_rows.code, max(bar_rows.bar_time)::time as last_bar_time
            from fact.stock_bar_1m bar_rows
            where bar_rows.bar_time >= %s::date
              and bar_rows.bar_time < %s::date + interval '1 day'
            group by bar_rows.market, bar_rows.code
        ) code_rows
        where code_rows.last_bar_time >= time '15:00'
        """,
        (trade_date, trade_date),
    )
    c231_members_count = sql_count(
        connection,
        """
        with target_rows as (
            select m.concept_id, m.stock_market, m.stock_code, m.valid_from, m.valid_to
            from ref.concept_stock_membership m
            where m.concept_id = 'C231'
              and m.valid_from <= %s::date
              and (m.valid_to is null or m.valid_to >= %s::date)
        ),
        fallback_date as (
            select max(m.valid_from) as valid_from
            from ref.concept_stock_membership m
            where m.concept_id = 'C231'
              and m.valid_from <= %s::date
        ),
        fallback_rows as (
            select m.concept_id, m.stock_market, m.stock_code, m.valid_from, m.valid_to
            from ref.concept_stock_membership m
            join fallback_date latest on latest.valid_from = m.valid_from
            where m.concept_id = 'C231'
              and not exists (select 1 from target_rows)
        )
        select count(*)
        from (
            select * from target_rows
            union all
            select * from fallback_rows
        ) rows
        """,
        (trade_date, trade_date, trade_date),
    )
finally:
    connection.close()

checks = [
    {
        "name": "previous_trading_day",
        "count": item_count(previous_payload),
        "minimum": 1,
        "blocking": True,
        "capabilities": ["markets.calendar.trading"],
        "source": "api",
    },
    {
        "name": "stock_daily_snapshot",
        "count": item_count(fetch_json(base_url, "/api/stocks/quotes/daily-snapshot", {"trade_date": trade_date, "skip_suspended": "true", "skip_st": "true", "limit": 10000})),
        "minimum": env_int("MARKETHUB_MIN_STOCK_DAILY_ROWS", 3000),
        "blocking": True,
        "capabilities": ["stocks.quotes.daily_snapshot"],
        "source": "api",
    },
    {
        "name": "stock_daily_local_window",
        "count": stock_local_window_count,
        "minimum": env_int("MARKETHUB_MIN_STOCK_DAILY_ROWS", 3000),
        "blocking": True,
        "capabilities": ["stocks.quotes.daily_snapshot"],
        "source": "db",
    },
    {
        "name": "concept_daily_snapshot",
        "count": concept_daily_snapshot_count,
        "minimum": env_int("MARKETHUB_MIN_CONCEPT_DAILY_ROWS", 200),
        "blocking": True,
        "capabilities": ["concepts.quotes.daily"],
        "source": "db",
    },
    {
        "name": "stock_intraday_code_coverage",
        "count": stock_intraday_code_count,
        "minimum": env_int("MARKETHUB_MIN_STOCK_INTRADAY_CODES", 3000),
        "blocking": True,
        "capabilities": ["stocks.quotes.intraday"],
        "source": "db",
    },
    {
        "name": "stock_intraday_close_coverage",
        "count": stock_intraday_close_count,
        "minimum": env_int("MARKETHUB_MIN_STOCK_INTRADAY_CODES", 3000),
        "blocking": True,
        "capabilities": ["stocks.quotes.intraday"],
        "source": "db",
    },
    {
        "name": "index_quotes",
        "count": item_count(fetch_json(base_url, "/api/indexes/quotes", {"index_codes": "000001,399001,399006,000300,000905,000852,899050", "trade_date": trade_date, "limit": 1000})),
        "minimum": env_int("MARKETHUB_MIN_INDEX_QUOTE_ROWS", 1),
        "blocking": True,
        "capabilities": ["indexes.quotes.daily"],
        "source": "api",
    },
    {
        "name": "main_capital_flow",
        "count": item_count(fetch_json(base_url, "/api/markets/indicators/main-capital-flow", {"trade_date": trade_date})),
        "minimum": env_int("MARKETHUB_MIN_MAIN_CAPITAL_FLOW_ROWS", 1),
        "blocking": True,
        "capabilities": ["markets.indicators.main_capital_flow"],
        "source": "api",
    },
    {
        "name": "connect_capital_flow",
        "count": item_count(fetch_json(base_url, "/api/markets/connect/capital-flow", {"trade_date": trade_date})),
        "minimum": env_int("MARKETHUB_MIN_CONNECT_CAPITAL_FLOW_ROWS", 1),
        "blocking": False,
        "capabilities": ["markets.connect.capital_flow"],
        "source": "api",
    },
    {
        "name": "connect_active_top10",
        "count": item_count(fetch_json(base_url, "/api/markets/connect/active-top10", {"trade_date": trade_date, "limit": 1000})),
        "minimum": env_int("MARKETHUB_MIN_CONNECT_ACTIVE_TOP10_ROWS", 1),
        "blocking": False,
        "capabilities": ["markets.connect.active_top10"],
        "source": "api",
    },
    {
        "name": "dragon_tiger",
        "count": item_count(fetch_json(base_url, "/api/markets/participants/dragon-tiger", {"trade_date": trade_date, "limit": 1000})),
        "minimum": env_int("MARKETHUB_MIN_DRAGON_TIGER_ROWS", 1),
        "blocking": False,
        "capabilities": ["markets.participants.dragon_tiger"],
        "source": "api",
    },
    {
        "name": "dragon_tiger_institutions",
        "count": item_count(fetch_json(base_url, "/api/markets/participants/dragon-tiger/institutions", {"trade_date": trade_date, "limit": 1000})),
        "minimum": env_int("MARKETHUB_MIN_DRAGON_TIGER_INSTITUTION_ROWS", 1),
        "blocking": False,
        "capabilities": ["markets.participants.dragon_tiger.institutions"],
        "source": "api",
    },
    {
        "name": "c231_members",
        "count": c231_members_count,
        "minimum": env_int("MARKETHUB_MIN_C231_MEMBER_ROWS", 1),
        "blocking": True,
        "capabilities": ["concepts.members"],
        "source": "db",
    },
    {
        "name": "hot_money_details",
        "count": item_count(fetch_json(base_url, "/api/markets/participants/hot-money/details", {"trade_date": trade_date, "limit": 300})),
        "minimum": env_int("MARKETHUB_MIN_HOT_MONEY_DETAIL_ROWS", 0),
        "blocking": False,
        "capabilities": ["markets.participants.hot_money.details"],
        "source": "api",
    },
    {
        "name": "open_auctions",
        "count": item_count(fetch_json(base_url, "/api/markets/trading/open-auctions", {"trade_date": trade_date, "limit": 1000})),
        "minimum": env_int("MARKETHUB_MIN_OPEN_AUCTION_ROWS", 1),
        "blocking": False,
        "capabilities": ["markets.trading.open_auctions"],
        "source": "api",
    },
    {
        "name": "limit_order_amount",
        "count": item_count(fetch_json(base_url, "/api/stocks/signals/limit-order-amount", {"trade_date": trade_date})),
        "minimum": env_int("MARKETHUB_MIN_LIMIT_ORDER_AMOUNT_ROWS", 1),
        "blocking": False,
        "capabilities": ["stocks.signals.limit_order_amount"],
        "source": "api",
    },
    {
        "name": "news_events",
        "count": item_count(fetch_json(base_url, "/api/markets/events/news", {"trade_date": trade_date, "limit": 200})),
        "minimum": env_int("MARKETHUB_MIN_NEWS_EVENT_ROWS", 0),
        "blocking": False,
        "capabilities": ["markets.events.news"],
        "source": "api",
    },
]

results: list[dict[str, object]] = []
failures: list[str] = []
failed_capabilities: list[str] = []
warnings: list[str] = []
warning_capabilities: list[str] = []
for check in checks:
    minimum = int(check["minimum"])
    count = int(check["count"])
    ok = count >= minimum
    result = {**check, "trade_date": trade_date, "ok": ok}
    results.append(result)
    print(f"{check['name']} count={count} minimum={minimum} blocking={check['blocking']} source={check['source']}")
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
    "$MARKETHUB_PYTHON" - "$1" <<'PY'
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
    "$MARKETHUB_PYTHON" - "$1" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(str(payload.get("trade_date", "")))
PY
}

refresh_stock_intraday_quotes() {
    local trade_date="$1"
    local result_prefix="$2"
    if [ "$trade_date" = "" ]; then
        log "后处理：缺少交易日，跳过分钟线定向补跑"
        return 1
    fi
    log "后处理：定向补跑股票 1m 分钟线 trade_date=$trade_date"
    "$MARKETHUB_PYTHON" - "$MARKETHUB_BASE_URL" "$trade_date" "$result_prefix" "$DB_HOST" "$DB_PORT" "$DB_NAME" "$DB_USER" "$DB_PASSWORD" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import psycopg


def chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[index: index + size] for index in range(0, len(items), size)]


base_url = sys.argv[1]
trade_date = sys.argv[2]
result_prefix = Path(sys.argv[3])
db_host = sys.argv[4]
db_port = int(sys.argv[5])
db_name = sys.argv[6]
db_user = sys.argv[7]
db_password = sys.argv[8]

with psycopg.connect(host=db_host, port=db_port, dbname=db_name, user=db_user, password=db_password) as connection:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select code
            from ref.stock
            where code <> '000000'
              and listed_date <= %s::date
              and (delisted_date is null or delisted_date >= %s::date)
            order by code
            """,
            (trade_date, trade_date),
        )
        codes = [str(row[0]).zfill(6) for row in cursor.fetchall()]

if codes == []:
    raise SystemExit("没有可补跑的活跃股票代码")

total_items = 0
failed_batches: list[dict[str, object]] = []
for batch_index, batch_codes in enumerate(chunks(codes, 100), start=1):
    params = urlencode(
        {
            "codes": ",".join(batch_codes),
            "freq": "1m",
            "start_date": trade_date,
            "end_date": trade_date,
            "skip_suspended": "false",
            "skip_st": "false",
            "limit": "50000",
        }
    )
    output_path = result_prefix.with_suffix(f".batch-{batch_index:03d}.json")
    try:
        with urlopen(f"{base_url}/api/stocks/quotes?{params}", timeout=600) as response:
            payload = json.loads(response.read().decode("utf-8"))
        items = payload.get("items", []) if isinstance(payload, dict) else []
        total_items += len(items)
        output_path.write_text(json.dumps({"codes": batch_codes, "item_count": len(items), "meta": payload.get("meta", {})}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"intraday_batch={batch_index} codes={len(batch_codes)} items={len(items)}", flush=True)
    except Exception as exc:
        failed_batches.append({"batch_index": batch_index, "codes": batch_codes, "error": str(exc)})
        print(f"intraday_batch={batch_index} failed={type(exc).__name__}: {exc}", flush=True)

summary = {"trade_date": trade_date, "code_count": len(codes), "total_items": total_items, "failed_batches": failed_batches}
result_prefix.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
if total_items == 0:
    raise SystemExit("分钟线定向补跑没有返回任何数据")
if failed_batches:
    raise SystemExit("分钟线定向补跑存在失败批次")
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

        if [ "$capability_id" = "concepts.members" ]; then
            "$MARKETHUB_PYTHON" "$CONCEPT_BACKFILL_SCRIPT" --window-count "$CONCEPT_BACKFILL_WINDOW_COUNT" --concept-id C231
            continue
        fi
        if [ "$compact_trade_date" != "" ] && [ "$capability_id" = "stocks.quotes.daily_snapshot" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/stocks/quotes/daily-snapshot?trade_date=$compact_trade_date&skip_suspended=true&skip_st=true&limit=10000" -o "$force_result_path.targeted.json"
            continue
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "stocks.quotes.intraday" ]; then
            refresh_stock_intraday_quotes "$trade_date" "$force_result_path.targeted"
            continue
        fi
        if [ "$compact_trade_date" != "" ] && [ "$capability_id" = "concepts.quotes.daily" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/concepts/quotes/daily-snapshot?trade_date=$compact_trade_date&limit=5000" -o "$force_result_path.targeted.json"
            continue
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "indexes.quotes.daily" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/indexes/quotes?index_codes=000001,399001,399006,000300,000905,000852,899050&trade_date=$trade_date&limit=1000" -o "$force_result_path.targeted.json"
            continue
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "markets.indicators.main_capital_flow" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/markets/indicators/main-capital-flow?trade_date=$trade_date" -o "$force_result_path.targeted.json"
            continue
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "markets.connect.capital_flow" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/markets/connect/capital-flow?trade_date=$trade_date" -o "$force_result_path.targeted.json"
            continue
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "markets.connect.active_top10" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/markets/connect/active-top10?trade_date=$trade_date&limit=1000" -o "$force_result_path.targeted.json"
            continue
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "markets.participants.dragon_tiger" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/markets/participants/dragon-tiger?trade_date=$trade_date&limit=1000" -o "$force_result_path.targeted.json"
            continue
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "markets.participants.dragon_tiger.institutions" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/markets/participants/dragon-tiger/institutions?trade_date=$trade_date&limit=1000" -o "$force_result_path.targeted.json"
            continue
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "markets.participants.hot_money.details" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/markets/participants/hot-money/details?trade_date=$trade_date&limit=1000" -o "$force_result_path.targeted.json"
            continue
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "markets.trading.open_auctions" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" "$MARKETHUB_BASE_URL/api/markets/trading/open-auctions?trade_date=$trade_date" -o "$force_result_path.targeted.json"
            continue
        fi
        if [ "$trade_date" != "" ] && [ "$capability_id" = "stocks.signals.limit_order_amount" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" -X POST "$MARKETHUB_BASE_URL/api/admin/capture/limit-order-amount/run-today?trade_date=$trade_date" -o "$force_result_path.targeted.json"
            continue
        fi

        curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" -X POST "$MARKETHUB_BASE_URL/api/admin/capture-runs/$capability_id" -o "$force_result_path"
        "$MARKETHUB_PYTHON" - "$force_result_path" <<'PY'
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
    run_due_captures
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
