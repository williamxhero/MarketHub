"""Resolve the latest closed SHSE session from the authoritative trade calendar."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, time, timedelta

import psycopg


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", required=True, help="Current Asia/Shanghai wall-clock time")
    args = parser.parse_args()
    now = datetime.fromisoformat(args.now)
    cutoff = now.date() if now.time() >= time(15) else now.date() - timedelta(days=1)
    with psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=os.environ["MARKETHUB_DB_PORT"],
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        connect_timeout=10,
    ) as connection, connection.cursor() as cursor:
        cursor.execute(
            "select max(trade_date) from ref.trade_calendar "
            "where exchange = 'SHSE' and is_open and trade_date <= %s::date",
            (cutoff,),
        )
        day = cursor.fetchone()[0]
    if day is None:
        raise RuntimeError(f"no closed SHSE trading day in ref.trade_calendar on or before {cutoff}")
    print(day.isoformat())


if __name__ == "__main__":
    main()
