"""Audit index daily coverage for a reproducible date range without writing data."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date

import psycopg
from psycopg.rows import dict_row


def _connection() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=os.environ["MARKETHUB_DB_PORT"],
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        connect_timeout=15,
        row_factory=dict_row,
        application_name="markethub-audit-index-daily-gap",
    )


def audit(start_date: date, end_date: date) -> dict[str, object]:
    with _connection() as connection:
        expected = connection.execute(
            """select trade_date::text
               from ref.trade_calendar
               where exchange='SHSE' and is_open
                 and trade_date between %s and %s
               order by trade_date""",
            (start_date, end_date),
        ).fetchall()
        index_codes = connection.execute(
            "select index_code from ref.index order by index_code"
        ).fetchall()
        rows = connection.execute(
            """select trade_date::text, index_code
               from fact.index_bar_1d
               where trade_date between %s and %s
               order by trade_date,index_code""",
            (start_date, end_date),
        ).fetchall()
    expected_dates = [str(row["trade_date"]) for row in expected]
    expected_codes = [str(row["index_code"]) for row in index_codes]
    actual_by_date: dict[str, set[str]] = {}
    for row in rows:
        actual_by_date.setdefault(str(row["trade_date"]), set()).add(str(row["index_code"]))
    return {
        "contract": "markethub-index-daily-gap-audit-v1",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "expected_trade_dates": expected_dates,
        "ref_index_codes": expected_codes,
        "missing_by_date": {
            day: sorted(set(expected_codes) - actual_by_date.get(day, set()))
            for day in expected_dates
            if set(expected_codes) - actual_by_date.get(day, set())
        },
        "index_count_by_date": {
            day: len(actual_by_date.get(day, set())) for day in expected_dates
        },
        "write_performed": False,
        "repair_hint": "POST /api/admin/capture-runs/indexes.quotes.daily after verifying provider evidence",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    if args.start_date > args.end_date:
        raise SystemExit("start date must not be after end date")
    print(json.dumps(audit(args.start_date, args.end_date), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
