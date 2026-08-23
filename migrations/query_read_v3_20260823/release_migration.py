from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import socket
from typing import Any

import psycopg
from psycopg.rows import dict_row

from services.daily_coverage_read_model import build_current_stock_daily_coverage
from services.minute_coverage_read_model import build_stock_1m_daily_coverage


MIGRATION_ID = "markethub-query-read-v3-20260823"


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"], port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"], user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"], connect_timeout=10, row_factory=dict_row,
        application_name=MIGRATION_ID,
    )


def preflight() -> dict[str, object]:
    required_env = ("MARKETHUB_DB_HOST", "MARKETHUB_DB_PORT", "MARKETHUB_DB_NAME", "MARKETHUB_DB_USER", "MARKETHUB_DB_PASSWORD")
    missing_env = [name for name in required_env if not os.environ.get(name)]
    if missing_env:
        raise RuntimeError(f"missing database environment: {missing_env}")
    with _connect() as connection:
        rows = connection.execute(
            "select name,to_regclass(name) is not null present from unnest(%s::text[]) name order by name",
            (["fact.stock_daily_1d", "fact.stock_bar_1m", "ref.stock", "ref.trade_calendar"],),
        ).fetchall()
        extension = connection.execute("select extversion from pg_extension where extname='timescaledb'").fetchone()
    missing_tables = [str(row["name"]) for row in rows if not bool(row["present"])]
    if missing_tables or extension is None:
        raise RuntimeError(f"preflight failed: missing_tables={missing_tables}, timescaledb={extension}")
    return {
        "migration_id": MIGRATION_ID, "host": socket.gethostname(), "database_host": os.environ["MARKETHUB_DB_HOST"],
        "database_name": os.environ["MARKETHUB_DB_NAME"], "timescaledb": str(extension["extversion"]),
        "tables": [dict(row) for row in rows], "ready": True,
    }


def apply(start: date | None = None, end: date | None = None) -> dict[str, object]:
    result = {"migration_id": MIGRATION_ID, "preflight": preflight()}
    result["daily_coverage"] = build_current_stock_daily_coverage()
    if not bool(result["daily_coverage"].get("complete")):
        raise RuntimeError(
            "stock daily coverage is incomplete; repair the reported gaps through the formal admin repair path, "
            "then rerun this idempotent migration"
        )
    result["stock_1m_coverage"] = build_stock_1m_daily_coverage(start, end)
    result["verified"] = verify()
    return result


def verify() -> dict[str, object]:
    with _connect() as connection:
        states = connection.execute(
            "select dataset_id,dataset_version,status,coverage_ready,complete,row_count,checksum_sha256 from readmodel.dataset_build_state "
            "where dataset_id in ('stock_daily_1d','stock_bar_1m') order by updated_at_utc desc",
        ).fetchall()
        latest: dict[str, dict[str, object]] = {}
        for row in states:
            latest.setdefault(str(row["dataset_id"]), dict(row))
        versions = connection.execute("select dataset_id,generation from audit.dataset_version_state order by dataset_id").fetchall()
        samples = connection.execute(
            "select coverage.market,coverage.code,coverage.trade_date,coverage.row_count,actual.actual_rows "
            "from readmodel.stock_bar_1m_daily_coverage coverage cross join lateral ("
            "select count(*)::int actual_rows from fact.stock_bar_1m bars where bars.market=coverage.market and bars.code=coverage.code "
            "and bars.bar_time>=coverage.trade_date::timestamp and bars.bar_time<coverage.trade_date::timestamp+interval '1 day') actual "
            "order by coverage.trade_date desc,coverage.code limit 20",
        ).fetchall()
    required = {"stock_daily_1d", "stock_bar_1m"}
    if set(latest) != required or not all(bool(row["coverage_ready"]) and bool(row["complete"]) for row in latest.values()):
        raise RuntimeError(f"readmodel state incomplete: {latest}")
    mismatches = [dict(row) for row in samples if int(row["row_count"]) != int(row["actual_rows"])]
    if mismatches:
        raise RuntimeError(f"stock 1m coverage mismatch: {mismatches}")
    if len(versions) < 8:
        raise RuntimeError(f"dataset version registry incomplete: {len(versions)}")
    return {"migration_id": MIGRATION_ID, "ready": True, "states": latest, "dataset_count": len(versions), "minute_samples": len(samples), "minute_mismatches": mismatches}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("preflight", "apply", "verify"))
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = preflight() if args.action == "preflight" else apply(args.start, args.end) if args.action == "apply" else verify()
    encoded = json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, args.output)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
