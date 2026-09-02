from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


MIGRATION_ID = "markethub-live-stock-bar-v2-20260902"
REQUIRED_ENV = ("MARKETHUB_DB_HOST", "MARKETHUB_DB_PORT", "MARKETHUB_DB_NAME", "MARKETHUB_DB_USER", "MARKETHUB_DB_PASSWORD")
TABLES = ("stock_bar_observation", "stock_bar_selected", "stock_bar_provider_attempt")


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"], port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"], user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"], connect_timeout=10, row_factory=dict_row,
        application_name=MIGRATION_ID,
    )


def preflight() -> dict[str, object]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"missing database environment: {missing}")
    with _connect() as connection:
        rows = connection.execute("select to_regclass('live.' || name) is not null as present from unnest(%s::text[]) name", (list(TABLES),)).fetchall()
    if not all(bool(row["present"]) for row in rows):
        raise RuntimeError("live stock Bar v1 staging is required before the v2 frequency migration")
    return {"migration_id": MIGRATION_ID, "ready": True}


def apply() -> dict[str, object]:
    result = {"migration_id": MIGRATION_ID, "preflight": preflight()}
    with _connect() as connection:
        with connection.transaction():
            for table in TABLES:
                connection.execute(f"alter table live.{table} drop constraint if exists {table}_freq_check")
                connection.execute(f"alter table live.{table} add constraint {table}_freq_check check (freq in ('1m', '30m'))")
    result["verified"] = verify()
    return result


def verify() -> dict[str, object]:
    with _connect() as connection:
        rows = connection.execute(
            """
            select conrelid::regclass::text as table_name, pg_get_constraintdef(oid) as definition
            from pg_constraint
            where conname = any(%s::text[])
            order by table_name
            """,
            ([f"{table}_freq_check" for table in TABLES],),
        ).fetchall()
    if len(rows) != len(TABLES) or any("'30m'" not in str(row["definition"]) for row in rows):
        raise RuntimeError("live staging frequency constraints do not permit 30m")
    return {"migration_id": MIGRATION_ID, "ready": True, "constraints": [dict(row) for row in rows]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("preflight", "apply", "verify"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = {"preflight": preflight, "apply": apply, "verify": verify}[args.action]()
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
