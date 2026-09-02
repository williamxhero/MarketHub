from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


MIGRATION_ID = "markethub-live-stock-bar-v1-20260902"
REQUIRED_ENV = ("MARKETHUB_DB_HOST", "MARKETHUB_DB_PORT", "MARKETHUB_DB_NAME", "MARKETHUB_DB_USER", "MARKETHUB_DB_PASSWORD")


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
        row = connection.execute("select to_regclass('fact.stock_bar_1m') is not null as fact_present").fetchone()
    if row is None or not bool(row["fact_present"]):
        raise RuntimeError("fact.stock_bar_1m is required before live staging can be installed")
    return {"migration_id": MIGRATION_ID, "ready": True}


DDL = (
    "create schema if not exists live",
    """
    create table if not exists live.stock_bar_observation (
      observation_version bigint generated always as identity unique,
      provider text not null,
      market text not null,
      code character(6) not null,
      freq text not null check (freq = '1m'),
      interval_start timestamptz not null,
      observed_at timestamptz not null,
      native_trade_time timestamp not null,
      open double precision not null,
      high double precision not null,
      low double precision not null,
      close double precision not null,
      volume bigint not null check (volume >= 0),
      amount double precision not null check (amount >= 0),
      unit_conversion text not null,
      observation_hash text not null,
      finalized_at timestamptz,
      created_at timestamptz not null default now(),
      primary key (provider, market, code, freq, interval_start, observation_hash)
    )
    """,
    """
    create table if not exists live.stock_bar_selected (
      market text not null,
      code character(6) not null,
      freq text not null check (freq = '1m'),
      interval_start timestamptz not null,
      provider text not null,
      observation_version bigint not null,
      selection_reason text not null,
      state text not null default 'staged' check (state in ('staged', 'finalized', 'failed')),
      selected_at timestamptz not null,
      updated_at timestamptz not null default now(),
      primary key (market, code, freq, interval_start),
      foreign key (observation_version) references live.stock_bar_observation(observation_version)
    )
    """,
    """
    create table if not exists live.stock_bar_provider_attempt (
      attempt_id bigint generated always as identity primary key,
      provider text not null,
      market text not null,
      code character(6) not null,
      freq text not null check (freq = '1m'),
      interval_start timestamptz not null,
      observed_at timestamptz not null,
      server text not null,
      outcome text not null,
      detail text not null default ''
    )
    """,
    "create index if not exists stock_bar_observation_interval_idx on live.stock_bar_observation (market, code, freq, interval_start desc)",
    "create index if not exists stock_bar_selected_staged_idx on live.stock_bar_selected (state, interval_start) where state = 'staged'",
    "create index if not exists stock_bar_provider_attempt_interval_idx on live.stock_bar_provider_attempt (code, interval_start desc, observed_at desc)",
)


def apply() -> dict[str, object]:
    result = {"migration_id": MIGRATION_ID, "preflight": preflight()}
    with _connect() as connection:
        with connection.transaction():
            for statement in DDL:
                connection.execute(statement)
    result["verified"] = verify()
    return result


def verify() -> dict[str, object]:
    required = ["live.stock_bar_observation", "live.stock_bar_selected", "live.stock_bar_provider_attempt"]
    with _connect() as connection:
        rows = connection.execute(
            "select name,to_regclass(name) is not null as present from unnest(%s::text[]) name order by name",
            (required,),
        ).fetchall()
    missing = [str(row["name"]) for row in rows if not bool(row["present"])]
    if missing:
        raise RuntimeError(f"live staging relations are missing: {missing}")
    return {"migration_id": MIGRATION_ID, "ready": True, "relations": [dict(row) for row in rows]}


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
