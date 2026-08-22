from __future__ import annotations

import argparse
import json
import os
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


TARGET_TABLES = ("stock_daily_1d", "stock_bar_1m", "stock_bar_5m", "stock_bar_30m", "future_bar_1m")


def _api_performance() -> dict[str, Any]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8803/api/diagnostics/performance", timeout=10) as response:
            return {"ok": True, "payload": json.load(response)}
    except Exception as exc:  # The database evidence must still be retained if the API is temporarily unavailable.
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        connect_timeout=10,
        row_factory=dict_row,
    )


def capture() -> dict[str, Any]:
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            select clock_timestamp() as captured_at,
                   current_date as server_date,
                   (clock_timestamp() at time zone 'Asia/Shanghai')::date as shanghai_date,
                   (clock_timestamp() at time zone 'Asia/Shanghai')::time >= time '21:00' as after_trading_window,
                   exists (
                       select 1 from ref.trade_calendar
                       where exchange = 'SHSE'
                         and trade_date = (clock_timestamp() at time zone 'Asia/Shanghai')::date
                         and is_open
                   ) as is_current_trading_day,
                   (select max(trade_date) from ref.trade_calendar where exchange = 'SHSE' and trade_date <= (clock_timestamp() at time zone 'Asia/Shanghai')::date and is_open) as latest_trading_day
            """
        )
        clock = cursor.fetchone()
        cursor.execute(
            """
            select schemaname, relname, indexrelname, idx_scan, idx_tup_read, idx_tup_fetch,
                   pg_relation_size(indexrelid) as size_bytes,
                   pg_get_indexdef(indexrelid) as index_definition
            from pg_stat_user_indexes
            where relname = any(%s)
            order by schemaname, relname, indexrelname
            """,
            (list(TARGET_TABLES),),
        )
        indexes = cursor.fetchall()
        cursor.execute(
            """
            select queryid, calls, total_exec_time, mean_exec_time, rows,
                   shared_blks_hit, shared_blks_read, temp_blks_read, temp_blks_written,
                   left(regexp_replace(query, E'[\\n\\r\\t ]+', ' ', 'g'), 2000) as query
            from pg_stat_statements
            where query ilike '%stock_daily_1d%'
            order by total_exec_time desc
            limit 50
            """
        )
        statements = cursor.fetchall()
        cursor.execute("select code from fact.stock_daily_1d order by code, trade_date desc limit 1")
        representative_code = cursor.fetchone()["code"]
        plan_sql = {
            "code_range": "select * from fact.stock_daily_1d where code = %s and trade_date between current_date - 365 and current_date order by trade_date",
            "market_date": "select * from fact.stock_daily_1d where trade_date = current_date - 1 order by market, code",
        }
        plans: dict[str, Any] = {}
        for name, sql in plan_sql.items():
            parameters = (representative_code,) if "%s" in sql else ()
            cursor.execute("explain (format json) " + sql, parameters)
            plans[name] = cursor.fetchone()["QUERY PLAN"][0]
        cursor.execute(
            """
            select a.indexrelid::regclass::text as first_index,
                   b.indexrelid::regclass::text as duplicate_index,
                   pg_get_indexdef(a.indexrelid) as first_definition,
                   pg_get_indexdef(b.indexrelid) as duplicate_definition
            from pg_index a
            join pg_index b on a.indrelid = b.indrelid and a.indexrelid < b.indexrelid
            where a.indrelid = 'fact.stock_daily_1d'::regclass
              and a.indkey = b.indkey
              and a.indclass = b.indclass
              and a.indcollation = b.indcollation
              and a.indoption = b.indoption
              and a.indexprs is not distinct from b.indexprs
              and a.indpred is not distinct from b.indpred
            order by first_index, duplicate_index
            """
        )
        duplicates = cursor.fetchall()
        return {
            "captured_at": clock["captured_at"],
            "server_date": clock["server_date"],
            "shanghai_date": clock["shanghai_date"],
            "after_trading_window": clock["after_trading_window"],
            "is_current_trading_day": clock["is_current_trading_day"],
            "latest_trading_day": clock["latest_trading_day"],
            "representative_code": representative_code,
            "target_tables": list(TARGET_TABLES),
            "indexes": indexes,
            "statements": statements,
            "plans": plans,
            "exact_duplicate_indexes": duplicates,
            "api_performance": _api_performance(),
        }


def _write_atomic(output_root: Path, payload: dict[str, Any]) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = output_root / f"index-observation-{timestamp}.json"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output_root, prefix=".index-observation-", suffix=".tmp", delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture cumulative PostgreSQL index and statement observations")
    parser.add_argument("--output-root", type=Path, default=Path("/data/markethub/observability/indexes"))
    args = parser.parse_args()
    destination = _write_atomic(args.output_root, capture())
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
