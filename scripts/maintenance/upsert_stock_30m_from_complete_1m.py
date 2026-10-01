"""Materialize missing stock 30m days from complete real 1m facts only.

This explicit derived maintenance path never replaces an existing 30m day.
Every source code-day must have all 240 standard minutes and non-null amount.
"""

from __future__ import annotations

import argparse
import atexit
import csv
import fcntl
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

import psycopg


MAINTENANCE_LOCK = Path("/data/markethub/audit/stock-bar-maintenance.lock")


def acquire_maintenance_lock() -> bool:
    MAINTENANCE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = MAINTENANCE_LOCK.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    atexit.register(handle.close)
    return True


def db_connection() -> psycopg.Connection:
    values = {
        "host": os.getenv("MARKETHUB_DB_HOST") or os.getenv("QUOTEMUX_CACHE_DB_HOST"),
        "port": os.getenv("MARKETHUB_DB_PORT") or os.getenv("QUOTEMUX_CACHE_DB_PORT"),
        "dbname": os.getenv("MARKETHUB_DB_NAME") or os.getenv("QUOTEMUX_CACHE_DB_NAME"),
        "user": os.getenv("MARKETHUB_DB_USER") or os.getenv("QUOTEMUX_CACHE_DB_USER"),
        "password": os.getenv("MARKETHUB_DB_PASSWORD") or os.getenv("QUOTEMUX_CACHE_DB_PASSWORD"),
    }
    if any(not value for value in values.values()):
        raise RuntimeError("production database configuration is incomplete")
    return psycopg.connect(**values, connect_timeout=15)


def query_api(base_url: str, path: str) -> object:
    with urlopen(f"{base_url}{path}", timeout=15) as response:
        return json.load(response)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidate_days(connection: psycopg.Connection, minimum_codes: int, max_days: int) -> list[tuple[str, int]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            with target as (
                select bar_time::date as trade_date, market, code,
                       count(*) as row_count,
                       count(*) filter (where amount is null) as null_amount
                from fact.stock_bar_30m
                where bar_time >= current_date - interval '30 days'
                group by 1, 2, 3
            ), incomplete_days as (
                select source.trade_date, count(*)::int as code_count,
                       count(*) filter (
                           where coalesce(target.row_count, 0) <> 10
                              or coalesce(target.null_amount, 0) > 0
                       ) as incomplete_codes
                from readmodel.stock_bar_1m_daily_coverage source
                left join target on target.trade_date = source.trade_date
                                and target.market = source.market
                                and target.code = source.code
                where source.trade_date >= current_date - interval '30 days'
                  and source.row_count = 240
                group by source.trade_date
            )
            select trade_date::text, code_count
            from incomplete_days
            where code_count >= %s and incomplete_codes > 0
            order by trade_date desc
            limit %s
            """,
            (minimum_codes, max_days),
        )
        return [(str(day), int(count)) for day, count in cursor.fetchall()]


def source_keys_and_integrity(connection: psycopg.Connection, trade_date: str, expected_codes: int) -> list[tuple[str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select market, btrim(code)
            from readmodel.stock_bar_1m_daily_coverage
            where trade_date = %s::date and row_count = 240
            order by market, code
            """,
            (trade_date,),
        )
        keys = [(str(market), str(code)) for market, code in cursor.fetchall()]
        if len(keys) != expected_codes:
            raise RuntimeError(f"{trade_date}: source code count changed")
        cursor.execute(
            """
            select count(*),
                   count(*) filter (where bars.amount is null),
                   count(*) filter (where not (
                       bars.bar_time::time between time '09:31:00' and time '11:30:00'
                       or bars.bar_time::time between time '13:01:00' and time '15:00:00'
                   ))
            from fact.stock_bar_1m bars
            join readmodel.stock_bar_1m_daily_coverage complete
              on complete.market = bars.market
             and complete.code = bars.code
             and complete.trade_date = %s::date
             and complete.row_count = 240
            where bars.bar_time >= %s::date
              and bars.bar_time < %s::date + interval '1 day'
            """,
            (trade_date, trade_date, trade_date),
        )
        rows, null_amount, unexpected_time = cursor.fetchone()
    if (rows, null_amount, unexpected_time) != (expected_codes * 240, 0, 0):
        raise RuntimeError(
            f"{trade_date}: source integrity failed rows={rows} null_amount={null_amount} unexpected_time={unexpected_time}"
        )
    return keys


def materialize(connection: psycopg.Connection, trade_date: str, expected_codes: int) -> tuple[int, int]:
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("set local statement_timeout = '180s'")
            cursor.execute("set local timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
            cursor.execute(
                """
                create temporary table complete_codes on commit drop as
                select market, code
                from readmodel.stock_bar_1m_daily_coverage
                where trade_date = %s::date and row_count = 240
                """,
                (trade_date,),
            )
            cursor.execute("select count(*) from complete_codes")
            if cursor.fetchone()[0] != expected_codes:
                raise RuntimeError(f"{trade_date}: source code count changed during staging")
            cursor.execute("analyze complete_codes")
            cursor.execute(
                """
                create temporary table staged_30m on commit drop as
                with source_rows as (
                    select bars.market, bars.code, bars.bar_time, bars.open,
                           bars.high, bars.low, bars.close, bars.volume, bars.amount,
                           date_trunc('hour', bars.bar_time)
                             + floor(extract(minute from bars.bar_time) / 30) * interval '30 minutes' as bucket_time
                    from fact.stock_bar_1m bars
                    join complete_codes complete
                      on complete.market = bars.market and complete.code = bars.code
                    where bars.bar_time >= %s::date
                      and bars.bar_time < %s::date + interval '1 day'
                      and (bars.bar_time::time between time '09:31:00' and time '11:30:00'
                           or bars.bar_time::time between time '13:01:00' and time '15:00:00')
                )
                select market, code, bucket_time as bar_time,
                       (array_agg(open order by bar_time))[1] as open,
                       max(high) as high, min(low) as low,
                       (array_agg(close order by bar_time desc))[1] as close,
                       sum(volume)::bigint as volume, sum(amount) as amount
                from source_rows
                group by market, code, bucket_time
                """,
                (trade_date, trade_date),
            )
            cursor.execute(
                """
                select count(*), count(distinct (market, code)),
                       count(*) filter (where amount is null)
                from staged_30m
                """
            )
            rows, codes, null_amount = cursor.fetchone()
            if (rows, codes, null_amount) != (expected_codes * 10, expected_codes, 0):
                raise RuntimeError(f"{trade_date}: staged 30m integrity failed rows={rows} codes={codes} null={null_amount}")
            cursor.execute(
                """
                insert into fact.stock_bar_30m
                    (market, code, bar_time, open, high, low, close, volume, amount)
                select market, code, bar_time, open, high, low, close, volume, amount
                from staged_30m
                on conflict (market, code, bar_time) do nothing
                """
            )
            inserted = cursor.rowcount
            cursor.execute(
                """
                update fact.stock_bar_30m target set amount = staged.amount
                from staged_30m staged
                where target.market = staged.market and target.code = staged.code
                  and target.bar_time = staged.bar_time and target.amount is null
                  and target.bar_time >= %s::date
                  and target.bar_time < %s::date + interval '1 day'
                """,
                (trade_date, trade_date),
            )
            filled_amount = cursor.rowcount
        verify_target(connection, trade_date, expected_codes)
    return inserted, filled_amount


def verify_target(connection: psycopg.Connection, trade_date: str, expected_codes: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            with per_code as (
                select target.market, target.code, count(*) as bars,
                       count(distinct target.bar_time) as unique_bars,
                       count(*) filter (where target.amount is null) as null_amount
                from fact.stock_bar_30m target
                join readmodel.stock_bar_1m_daily_coverage source
                  on source.market = target.market and source.code = target.code
                 and source.trade_date = %s::date and source.row_count = 240
                where target.bar_time >= %s::date and target.bar_time < %s::date + interval '1 day'
                group by target.market, target.code
            )
            select count(*), sum(bars), min(bars), max(bars),
                   min(unique_bars), max(unique_bars), sum(null_amount)
            from per_code
            """,
            (trade_date, trade_date, trade_date),
        )
        actual = cursor.fetchone()
    expected = (expected_codes, expected_codes * 10, 10, 10, 10, 10, 0)
    if actual != expected:
        raise RuntimeError(f"{trade_date}: persisted 30m verification failed actual={actual} expected={expected}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-dir", type=Path, default=Path("/data/markethub/audit/stock-30m-from-complete-1m"))
    parser.add_argument("--api-url", default="http://127.0.0.1:8803")
    parser.add_argument("--task-url", default="http://127.0.0.1:8810")
    parser.add_argument("--minimum-codes", type=int, default=1)
    parser.add_argument("--max-days", type=int, default=3)
    args = parser.parse_args()
    if args.minimum_codes < 1 or not 1 <= args.max_days <= 10:
        parser.error("minimum-codes must be >=1 and max-days must be 1..10")
    if not acquire_maintenance_lock():
        print("deferred: stock-bar maintenance is already running", file=sys.stderr)
        return 75
    args.audit_dir.mkdir(parents=True, exist_ok=True)
    tasks = query_api(args.task_url, "/api/tasks")
    if not isinstance(tasks, list) or any(
        item.get("group_name") == "MARKETHUB" and item.get("status") == "running"
        for item in tasks
    ):
        print("deferred: MARKETHUB task running; 30m repair remains pending", file=sys.stderr)
        return 75
    health = query_api(args.api_url, "/api/health")
    if not isinstance(health, dict) or health.get("status") != "ok":
        raise RuntimeError("MarketHub health is not ok")
    with db_connection() as connection:
        candidates = candidate_days(connection, args.minimum_codes, args.max_days)
        for trade_date, expected_codes in candidates:
            day_dir = args.audit_dir / trade_date
            day_dir.mkdir(exist_ok=True)
            keys = source_keys_and_integrity(connection, trade_date, expected_codes)
            keys_path = day_dir / "source-complete-1m-code-days.csv.gz"
            with gzip.open(keys_path, "wt", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(("market", "code", "trade_date", "bars"))
                writer.writerows((market, code, trade_date, 240) for market, code in keys)
            manifest = {
                "remediation": "explicit_30m_materialization_from_complete_real_1m",
                "trade_date": trade_date,
                "source": "fact.stock_bar_1m",
                "source_complete_code_days": expected_codes,
                "source_rows": expected_codes * 240,
                "source_keys_sha256": sha256(keys_path),
                "script_sha256": sha256(Path(__file__)),
                "expected_target_rows": expected_codes * 10,
                "release": health.get("version"),
                "data_version_before": health.get("data_version"),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            manifest_path = day_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            # Read-only probes start a psycopg transaction; commit them before
            # the write so temp tables are dropped at this day's commit.
            connection.commit()
            inserted, filled_amount = materialize(connection, trade_date, expected_codes)
            verify_target(connection, trade_date, expected_codes)
            connection.commit()
            manifest["inserted_rows"] = inserted
            manifest["filled_null_amount_rows"] = filled_amount
            manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"trade_date": trade_date, "complete_codes": expected_codes, "inserted_rows": inserted, "filled_null_amount_rows": filled_amount}), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"stock 30m materialization failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
