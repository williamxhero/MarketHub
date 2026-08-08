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
# 服务监听通配地址不能作为本机客户端目标；显式配置仍优先。
MARKETHUB_BASE_URL="${MARKETHUB_BASE_URL:-http://${MARKETHUB_HOST/0.0.0.0/127.0.0.1}:$MARKETHUB_PORT}"
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
GAP_AUDIT_RESULT_PATH="$RESULT_DIR/$RUN_ID.gap-audit.json"
GAP_RETRY_RESULT_PATH="$RESULT_DIR/$RUN_ID.gap-retry.json"
LOG_PATH="$LOG_ROOT/global-data-update.log"
GAP_ALERT_ROOT="${MARKETHUB_GAP_ALERT_ROOT:-$RUNTIME_ROOT/alerts/market-data-gaps}"
GAP_ALERT_PATH="$GAP_ALERT_ROOT/$RUN_ID.json"
DB_HOST="${MARKETHUB_DB_HOST:-127.0.0.1}"
DB_PORT="${MARKETHUB_DB_PORT:-55432}"
DB_NAME="${MARKETHUB_DB_NAME:-markethub_dev}"
DB_USER="${MARKETHUB_DB_USER:-markethub}"
DB_PASSWORD="${MARKETHUB_DB_PASSWORD:-markethub_dev_password}"
CAPTURE_WAIT_SECONDS="${MARKETHUB_CAPTURE_WAIT_SECONDS:-18000}"
CAPTURE_STALE_SECONDS="${MARKETHUB_CAPTURE_STALE_SECONDS:-21600}"
GAP_AUDIT_WINDOW_COUNT="${MARKETHUB_GAP_AUDIT_WINDOW_COUNT:-30}"
CONCEPT_BACKFILL_WINDOW_COUNT="${MARKETHUB_CONCEPT_BACKFILL_WINDOW_COUNT:-7}"
CONCEPT_BACKFILL_SCRIPT="${MARKETHUB_CONCEPT_BACKFILL_SCRIPT:-$WORKSPACE_ROOT/MarketHub/scripts/backfill_concept_runtime_refs.py}"
INTRADAY_ARCHIVE_IMPORT_SCRIPT="${MARKETHUB_INTRADAY_ARCHIVE_IMPORT_SCRIPT:-$WORKSPACE_ROOT/MarketHub/scripts/import_daily_1m_7z_to_fact.py}"
INTRADAY_30M_REBUILD_SCRIPT="${MARKETHUB_INTRADAY_30M_REBUILD_SCRIPT:-$WORKSPACE_ROOT/MarketHub/scripts/rebuild_stock_30m_from_1m.py}"
INTRADAY_ARCHIVE_ROOT="${MARKETHUB_INTRADAY_ARCHIVE_ROOT:-/data/markethub/import_1m_7z/raw}"
if [ ! -f "$CONCEPT_BACKFILL_SCRIPT" ] && [ -f "/data/MarketHub2/current/MarketHub/scripts/backfill_concept_runtime_refs.py" ]; then
    CONCEPT_BACKFILL_SCRIPT="/data/MarketHub2/current/MarketHub/scripts/backfill_concept_runtime_refs.py"
fi
if [ ! -f "$INTRADAY_ARCHIVE_IMPORT_SCRIPT" ] && [ -f "/data/MarketHub2/current/MarketHub/scripts/import_daily_1m_7z_to_fact.py" ]; then
    INTRADAY_ARCHIVE_IMPORT_SCRIPT="/data/MarketHub2/current/MarketHub/scripts/import_daily_1m_7z_to_fact.py"
fi
if [ ! -f "$INTRADAY_30M_REBUILD_SCRIPT" ] && [ -f "/data/MarketHub2/current/MarketHub/scripts/rebuild_stock_30m_from_1m.py" ]; then
    INTRADAY_30M_REBUILD_SCRIPT="/data/MarketHub2/current/MarketHub/scripts/rebuild_stock_30m_from_1m.py"
fi
log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1"
}

psql_scalar() {
    PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -A -c "$1"
}

active_capture_runs() {
    psql_scalar "select count(*) from capability_capture_runs where status = 'running'" | tr -d '[:space:]'
}

mark_stale_capture_runs() {
    local updated_count
    updated_count="$(psql_scalar "
        with stale_runs as (
            select id
            from capability_capture_runs
            where status = 'running'
              and started_at < now() - (${CAPTURE_STALE_SECONDS} || ' seconds')::interval
        ),
        updated as (
            update capability_capture_runs runs
            set status = 'failed',
                finished_at = now(),
                error_message = '全局更新检测到超时残留 running 状态，已标记失败',
                detail_json = coalesce(runs.detail_json, '{}'::jsonb) || jsonb_build_object('stale_closed_by', 'global-data-update', 'stale_seconds', ${CAPTURE_STALE_SECONDS})
            from stale_runs
            where runs.id = stale_runs.id
            returning runs.id
        )
        select count(*) from updated
    " | tr -d '[:space:]')"
    if [ "$updated_count" != "0" ]; then
        log "预处理：已关闭超时残留 capability running 记录 count=$updated_count"
    fi
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

latest_completed_trading_day() {
    "$MARKETHUB_PYTHON" - "$MARKETHUB_BASE_URL" <<'PY'
from __future__ import annotations

from datetime import datetime, time, timedelta
import json
import sys
from urllib.request import urlopen
from zoneinfo import ZoneInfo

base_url = sys.argv[1]
now = datetime.now(ZoneInfo("Asia/Shanghai"))
# 20:00 任务应处理当天收盘数据；00:30、04:00 任务仍处理上一交易日。
query_date = now.date() + timedelta(days=1) if now.time() >= time(16, 0) else now.date()
with urlopen(f"{base_url}/api/markets/calendar/trading/previous?date={query_date:%Y%m%d}&count=1", timeout=60) as response:
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
    trade_date="$(latest_completed_trading_day)"
    if [ "$trade_date" = "" ]; then
        log "预处理：无法确定最近已收盘交易日，跳过 C231 本地成分检查"
        return 0
    fi
    row_count="$(local_c231_membership_count "$trade_date" | tr -d '[:space:]')"
    if [ "$row_count" != "0" ]; then
        return 0
    fi
    log "预处理：C231 本地成分缺失，执行定点回填 trade_date=$trade_date"
    "$MARKETHUB_PYTHON" "$CONCEPT_BACKFILL_SCRIPT" --window-count "$CONCEPT_BACKFILL_WINDOW_COUNT" --concept-id C231
}

import_intraday_archives() {
    local archives=()
    if [ ! -d "$INTRADAY_ARCHIVE_ROOT" ]; then
        log "预处理：分钟归档目录不存在，跳过归档导入 path=$INTRADAY_ARCHIVE_ROOT"
        return 0
    fi
    mapfile -d '' archives < <(find "$INTRADAY_ARCHIVE_ROOT" -maxdepth 1 -type f -name '20??????.7z' -print0 | sort -z)
    if [ "${#archives[@]}" = "0" ]; then
        log "预处理：没有可导入的分钟归档 path=$INTRADAY_ARCHIVE_ROOT"
        return 0
    fi
    log "预处理：校验并原子导入分钟归档 count=${#archives[@]}"
    "$MARKETHUB_PYTHON" "$INTRADAY_ARCHIVE_IMPORT_SCRIPT" "${archives[@]}"
}

preprocess() {
    log "预处理：准备目录并检查 MarketHub"
    mkdir -p "$RESULT_DIR" "$LOG_ROOT" "$GAP_ALERT_ROOT"
    curl --fail --silent --show-error --connect-timeout 10 --max-time 30 "$MARKETHUB_BASE_URL/api/health" >/dev/null
    curl --fail --silent --show-error --connect-timeout 10 --max-time 60 "$MARKETHUB_BASE_URL/api/admin/capture-policies" >/dev/null
    mark_stale_capture_runs
    repair_concept_fact_ref_if_needed
    repair_c231_membership_if_needed
    log "预处理：审计最近 $GAP_AUDIT_WINDOW_COUNT 个交易日的股票 1m 缺口"
    curl --fail --silent --show-error --connect-timeout 10 --max-time 600 \
        -X POST "$MARKETHUB_BASE_URL/api/admin/capture-gaps/audit?window_count=$GAP_AUDIT_WINDOW_COUNT" \
        -o "$GAP_AUDIT_RESULT_PATH"

    import_intraday_archives
}

rebuild_resolved_intraday_30m() {
    local retry_path="$1"
    local resolved_dates
    resolved_dates="$($MARKETHUB_PYTHON - "$retry_path" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for trade_date in payload.get("resolved_trade_dates", []):
    print(str(trade_date))
PY
)"
    if [ "$resolved_dates" = "" ]; then
        return 0
    fi
    while IFS= read -r trade_date; do
        if [ "$trade_date" = "" ]; then
            continue
        fi
        log "后处理：缺口已补齐，联动重建 30m trade_date=$trade_date"
        "$MARKETHUB_PYTHON" "$INTRADAY_30M_REBUILD_SCRIPT" "$trade_date" "$trade_date"
    done <<< "$resolved_dates"
}

retry_audited_intraday_gaps() {
    log "核心执行：仅重试缺口账本中最近 $GAP_AUDIT_WINDOW_COUNT 个交易日的股票 1m 数据"
    curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" \
        -X POST "$MARKETHUB_BASE_URL/api/admin/capture-gaps/retry?window_count=$GAP_AUDIT_WINDOW_COUNT" \
        -o "$GAP_RETRY_RESULT_PATH"
    "$MARKETHUB_PYTHON" - "$GAP_RETRY_RESULT_PATH" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
status = str(payload.get("status", ""))
before = int(payload.get("audit_before", {}).get("unresolved_count", 0) or 0)
after = int(payload.get("audit_after", {}).get("unresolved_count", 0) or 0)
resolved_dates = payload.get("resolved_trade_dates", [])
print(f"gap_retry_status={status} unresolved_before={before} unresolved_after={after} resolved_dates={len(resolved_dates)}")
if status not in {"success", "partial", "skipped"}:
    raise SystemExit(f"缺口定向重试发生系统失败: {status}")
PY
    rebuild_resolved_intraday_30m "$GAP_RETRY_RESULT_PATH"
}

run_due_captures() {
    local active_runs
    log "核心执行：同步运行到期 capability 数据更新"
    rm -f "$CAPTURE_RESULT_PATH"
    mark_stale_capture_runs
    active_runs="$(active_capture_runs)"
    if [ "$active_runs" != "0" ]; then
        log "核心执行：已有 capability 数据更新仍在运行，跳过本轮到期 capability 更新 active_runs=$active_runs"
        printf '[]\n' > "$CAPTURE_RESULT_PATH"
        return 0
    fi
    if ! curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" -X POST "$MARKETHUB_BASE_URL/api/admin/capture/run-due" -o "$CAPTURE_RESULT_PATH"; then
        log "核心执行：到期 capability 更新请求失败"
        return 1
    fi
    if [ ! -s "$CAPTURE_RESULT_PATH" ]; then
        log "核心执行：到期 capability 更新未生成结果"
        return 1
    fi
    "$MARKETHUB_PYTHON" - "$CAPTURE_RESULT_PATH" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(payload, list):
    raise SystemExit("到期 capability 更新返回值不是列表")
failed = [item for item in payload if isinstance(item, dict) and str(item.get("status", "")) == "failed"]
critical_capabilities = {
    "boards.quotes.daily",
    "markets.calendar.trading",
    "stocks.quotes.daily_snapshot",
    "stocks.quotes.daily",
    "stocks.quotes.intraday",
    "concepts.quotes.daily",
    "indexes.quotes.daily",
    "markets.indicators.main_capital_flow",
}
critical_failed = [item for item in failed if str(item.get("capability_id", "")) in critical_capabilities]
print(f"due_capture_runs={len(payload)} failed={len(failed)} critical_failed={len(critical_failed)}")
for item in failed:
    print(f"due_capture_failed capability={item.get('capability_id', '')} error={item.get('error', item.get('error_message', ''))}")
if critical_failed:
    raise SystemExit("到期关键 capability 更新存在失败项")
PY
}

rebuild_latest_intraday_30m() {
    local trade_date
    trade_date="$(latest_completed_trading_day)"
    if [ "$trade_date" = "" ]; then
        log "后处理：无法确定最近交易日，停止 30m 重建"
        return 1
    fi
    log "后处理：由完整 1m 重建 30m trade_date=$trade_date"
    "$MARKETHUB_PYTHON" "$INTRADAY_30M_REBUILD_SCRIPT" "$trade_date" "$trade_date"
}

validate_market_coverage() {
    local validation_path="$1"
    local status
    log "后处理：校验最近已收盘交易日核心数据覆盖"
    status=0
    "$MARKETHUB_PYTHON" - "$MARKETHUB_BASE_URL" "$validation_path" "$DB_HOST" "$DB_PORT" "$DB_NAME" "$DB_USER" "$DB_PASSWORD" <<'PY' || status=$?
from __future__ import annotations

from datetime import datetime, time, timedelta
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
now = datetime.now(ZoneInfo("Asia/Shanghai"))
query_date = now.date() + timedelta(days=1) if now.time() >= time(16, 0) else now.date()
query_date_text = query_date.strftime("%Y%m%d")
previous_payload = fetch_json(base_url, "/api/markets/calendar/trading/previous", {"date": query_date_text, "count": 1})
if not isinstance(previous_payload, list) or previous_payload == []:
    raise SystemExit("最近已收盘交易日接口返回为空")
trade_date = str(previous_payload[0].get("trade_date", ""))
if trade_date == "":
    raise SystemExit("最近已收盘交易日接口缺少 trade_date")
intraday_window_count = env_int("MARKETHUB_INTRADAY_WINDOW_COUNT", 5)
intraday_window_payload = fetch_json(base_url, "/api/markets/calendar/trading/previous", {"date": query_date_text, "count": intraday_window_count})
if not isinstance(intraday_window_payload, list) or intraday_window_payload == []:
    raise SystemExit("分钟线校验交易日窗口为空")
intraday_dates = [str(item.get("trade_date", "")) for item in intraday_window_payload if str(item.get("trade_date", "")) != ""]
if intraday_dates == []:
    raise SystemExit("分钟线校验交易日窗口缺少 trade_date")

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
    concept_reference_count = sql_count(
        connection,
        "select count(*) from ref.concept where status = 'active'",
        (),
    )
    concept_daily_minimum = max(
        env_int("MARKETHUB_MIN_CONCEPT_DAILY_ROWS", 200),
        int(concept_reference_count * 0.9),
    )
    board_daily_snapshot_count = sql_count(
        connection,
        """
        select count(*)
        from fact.board_daily_1d day_rows
        where day_rows.trade_date = %s::date
          and day_rows.board_code like 'INDUSTRY:%%'
        """,
        (trade_date,),
    )
    industry_reference_count = sql_count(
        connection,
        "select count(distinct industry) from ref.stock where industry <> ''",
        (),
    )
    board_daily_minimum = max(
        env_int("MARKETHUB_MIN_BOARD_DAILY_ROWS", 50),
        int(industry_reference_count * 0.9),
    )
    stock_intraday_coverage_rows: list[dict[str, object]] = []
    for intraday_date in intraday_dates:
        expected_count = sql_count(
            connection,
            """
            select count(*)
            from fact.stock_daily_1d day_rows
            where day_rows.trade_date = %s::date
              and not coalesce(day_rows.is_suspended, false)
            """,
            (intraday_date,),
        )
        code_count = sql_count(
            connection,
            """
            select count(distinct bar_rows.code)
            from fact.stock_bar_1m bar_rows
            where bar_rows.bar_time >= %s::date
              and bar_rows.bar_time < %s::date + interval '1 day'
            """,
            (intraday_date, intraday_date),
        )
        complete_count = sql_count(
            connection,
            """
            select count(*)
            from (
                select bar_rows.market, bar_rows.code, count(*) as bar_count
                from fact.stock_bar_1m bar_rows
                where bar_rows.bar_time >= %s::date
                  and bar_rows.bar_time < %s::date + interval '1 day'
                  and (
                        bar_rows.bar_time::time between time '09:31:00' and time '11:30:00'
                     or bar_rows.bar_time::time between time '13:01:00' and time '15:00:00'
                  )
                  and bar_rows.open is not null
                  and bar_rows.high is not null
                  and bar_rows.low is not null
                  and bar_rows.close is not null
                group by bar_rows.market, bar_rows.code
            ) code_rows
            where code_rows.bar_count = 240
            """,
            (intraday_date, intraday_date),
        )
        stock_intraday_coverage_rows.append(
            {
                "trade_date": intraday_date,
                "expected_count": expected_count,
                "code_count": code_count,
                "complete_count": complete_count,
                "missing_count": max(expected_count - complete_count, 0),
            }
        )
    stock_intraday_exact_coverage = int(
        all(
            int(row["expected_count"]) > 0
            and int(row["complete_count"]) == int(row["expected_count"])
            for row in stock_intraday_coverage_rows
        )
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
        "name": "latest_completed_trading_day",
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
        "minimum": concept_daily_minimum,
        "blocking": True,
        "capabilities": ["concepts.quotes.daily"],
        "source": "db",
    },
    {
        "name": "board_daily_snapshot",
        "count": board_daily_snapshot_count,
        "minimum": board_daily_minimum,
        "blocking": True,
        "capabilities": ["boards.quotes.daily"],
        "source": "db",
    },
    {
        "name": "stock_intraday_exact_coverage",
        "count": stock_intraday_exact_coverage,
        "minimum": 1,
        "blocking": True,
        "capabilities": ["stocks.quotes.intraday"],
        "source": "db",
        "details": stock_intraday_coverage_rows,
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
    if "details" in check:
        print(f"{check['name']} details={json.dumps(check['details'], ensure_ascii=False)}")
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
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" -X POST "$MARKETHUB_BASE_URL/api/admin/capture-gaps/retry?window_count=$GAP_AUDIT_WINDOW_COUNT" -o "$force_result_path"
            "$MARKETHUB_PYTHON" - "$force_result_path" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
status = str(payload.get("status", ""))
before = int(payload.get("audit_before", {}).get("unresolved_count", 0) or 0)
after = int(payload.get("audit_after", {}).get("unresolved_count", 0) or 0)
print(f"forced_status={status} unresolved_before={before} unresolved_after={after}")
if status not in {"success", "partial", "skipped"}:
    raise SystemExit(f"强制补跑失败: {status}")
PY
            rebuild_resolved_intraday_30m "$force_result_path"
            continue
        fi
        if [ "$compact_trade_date" != "" ] && [ "$capability_id" = "concepts.quotes.daily" ]; then
            "$MARKETHUB_PYTHON" "$CONCEPT_BACKFILL_SCRIPT" --quotes-only --window-count "$CONCEPT_BACKFILL_WINDOW_COUNT"
            continue
        fi
        if [ "$capability_id" = "boards.quotes.daily" ]; then
            curl --fail --silent --show-error --connect-timeout 10 --max-time "$CAPTURE_WAIT_SECONDS" -X POST "$MARKETHUB_BASE_URL/api/admin/capture-runs/$capability_id" -o "$force_result_path"
            "$MARKETHUB_PYTHON" - "$force_result_path" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
status = str(payload.get("status", ""))
row_count = int(payload.get("row_count", 0) or 0)
if status != "success" or row_count == 0:
    raise SystemExit(f"行业板块日线补跑失败: status={status} row_count={row_count}")
PY
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
    local rebuild_status
    rebuild_status=0
    rebuild_latest_intraday_30m || rebuild_status=$?
    if validate_market_coverage "$VALIDATION_PATH" && [ "$rebuild_status" = "0" ]; then
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
    rebuild_latest_intraday_30m
    if validate_market_coverage "$VALIDATION_PATH"; then
        log "后处理：强制补跑后覆盖达标"
        return 0
    fi
    log "后处理：强制补跑后覆盖仍不足，停止本轮更新"
    return 1
}

gap_escalation_due() {
    if [ "${MARKETHUB_FORCE_GAP_ALERT:-0}" = "1" ]; then
        return 0
    fi
    [ "$(TZ=Asia/Shanghai date '+%H')" = "04" ]
}

emit_gap_alert() {
    local reason="$1"
    local summary
    summary="$($MARKETHUB_PYTHON - "$MARKETHUB_BASE_URL" "$GAP_ALERT_PATH" "$VALIDATION_PATH" "$reason" <<'PY'
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

base_url = sys.argv[1]
alert_path = Path(sys.argv[2])
validation_path = Path(sys.argv[3])
reason = sys.argv[4]
query = urlencode({"capability_id": "stocks.quotes.intraday", "limit": 5000})
with urlopen(f"{base_url}/api/admin/capture-gaps?{query}", timeout=120) as response:
    gap_rows = json.loads(response.read().decode("utf-8"))
if not isinstance(gap_rows, list):
    raise SystemExit("缺口接口返回值不是列表")
unresolved = [item for item in gap_rows if isinstance(item, dict) and str(item.get("status", "")) != "resolved"]
validation = {}
if validation_path.is_file():
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
status_counts: dict[str, int] = {}
for item in unresolved:
    status = str(item.get("status", ""))
    status_counts[status] = status_counts.get(status, 0) + 1
payload = {
    "alert_type": "market_data_gap",
    "severity": "error",
    "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
    "reason": reason,
    "unresolved_count": len(unresolved),
    "status_counts": status_counts,
    "validation_failures": validation.get("failures", []),
    "gaps": unresolved,
}
alert_path.parent.mkdir(parents=True, exist_ok=True)
alert_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"MarketHub 行情缺口告警 reason={reason} unresolved={len(unresolved)} status_counts={json.dumps(status_counts, ensure_ascii=False)} report={alert_path}")
PY
)"
    log "$summary"
    if command -v systemd-cat >/dev/null 2>&1; then
        printf '%s\n' "$summary" | systemd-cat -t markethub-data-gap -p err
    fi
}

escalate_unresolved_gaps_if_due() {
    local gap_count
    if ! gap_escalation_due; then
        return 0
    fi
    curl --fail --silent --show-error --connect-timeout 10 --max-time 600 \
        -X POST "$MARKETHUB_BASE_URL/api/admin/capture-gaps/audit?window_count=$GAP_AUDIT_WINDOW_COUNT" \
        -o "$GAP_AUDIT_RESULT_PATH"
    gap_count="$($MARKETHUB_PYTHON - "$GAP_AUDIT_RESULT_PATH" <<'PY'
from __future__ import annotations

import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(int(payload.get("unresolved_count", 0) or 0))
PY
)"
    if [ "$gap_count" = "0" ]; then
        return 0
    fi
    emit_gap_alert "04:00 重试后仍有历史股票 1m 缺口"
    return 1
}

run_once() {
    local capture_status
    local postprocess_status
    log "开始 MarketHub / QuoteMux 全局数据更新"
    preprocess
    capture_status=0
    run_due_captures || capture_status=$?
    retry_audited_intraday_gaps || capture_status=$?
    postprocess_status=0
    postprocess || postprocess_status=$?
    if [ "$postprocess_status" != "0" ]; then
        if gap_escalation_due; then
            emit_gap_alert "04:00 更新后核心数据覆盖仍不足"
        fi
        return "$postprocess_status"
    fi
    if ! escalate_unresolved_gaps_if_due; then
        return 1
    fi
    if [ "$capture_status" != "0" ]; then
        log "核心执行：到期 capability 更新存在失败项，但覆盖校验已达标，本轮按成功处理"
        return 0
    fi
    log "完成 MarketHub / QuoteMux 全局数据更新"
}

main() {
    mkdir -p "$RUN_ROOT" "$LOG_ROOT"
    {
        # 同一轮更新已在执行时保持互斥；这是正常跳过而不是任务失败。
        flock -n 9 || { log "已有全局数据更新脚本正在运行，跳过本轮"; exit 0; }
        run_once
    } 9>"$LOCK_PATH" 2>&1 | tee -a "$LOG_PATH"
}

main
