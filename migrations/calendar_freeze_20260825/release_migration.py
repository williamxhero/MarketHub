from __future__ import annotations

import argparse
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row


MIGRATION_ID = "markethub-calendar-freeze-20260825"

_DDL = """
create schema if not exists readmodel;
create schema if not exists audit;

create table if not exists readmodel.trade_calendar_snapshot_row (
    snapshot_sha256 text not null,
    exchange varchar not null,
    trade_date date not null,
    is_open boolean not null,
    primary key (snapshot_sha256, exchange, trade_date)
);

create table if not exists audit.trade_calendar_publication (
    market_data_version text not null,
    exchange varchar not null,
    range_start date not null,
    range_end date not null,
    snapshot_sha256 text not null,
    row_count integer not null check (row_count > 0),
    open_day_count integer not null check (open_day_count >= 0 and open_day_count <= row_count),
    published_at_utc timestamptz not null default clock_timestamp(),
    primary key (market_data_version, exchange, range_start, range_end)
);

create index if not exists idx_trade_calendar_publication_lookup
on audit.trade_calendar_publication (market_data_version, exchange, range_start, range_end);
"""


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        connect_timeout=10,
        row_factory=dict_row,
        application_name=MIGRATION_ID,
    )


def _expected_dates(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def canonical_snapshot(
    rows: Iterable[dict[str, object]],
    *,
    start: date,
    end: date,
) -> tuple[str, tuple[tuple[date, bool], ...]]:
    normalized = tuple(
        sorted(
            (
                row["trade_date"] if isinstance(row["trade_date"], date) else date.fromisoformat(str(row["trade_date"])),
                bool(row["is_open"]),
            )
            for row in rows
        )
    )
    actual_dates = tuple(item[0] for item in normalized)
    expected_dates = _expected_dates(start, end)
    if actual_dates != expected_dates:
        missing = [item.isoformat() for item in expected_dates if item not in set(actual_dates)]
        duplicates = len(actual_dates) - len(set(actual_dates))
        raise RuntimeError(
            "trade calendar facts are incomplete; run the authoritative QuoteMux calendar repair first: "
            f"missing={missing[:20]}, missing_count={len(missing)}, duplicates={duplicates}"
        )
    payload = "".join(f"{trade_date.isoformat()}|{int(is_open)}\n" for trade_date, is_open in normalized)
    return hashlib.sha256(payload.encode("ascii")).hexdigest(), normalized


def preflight(start: date, end: date, exchange: str) -> dict[str, object]:
    if start > end:
        raise ValueError("start must not be after end")
    with _connect() as connection:
        relation = connection.execute("select to_regclass('ref.trade_calendar') relation").fetchone()
        rows = connection.execute(
            "select trade_date,is_open from ref.trade_calendar "
            "where exchange=%s and trade_date between %s and %s order by trade_date",
            (_storage_exchange(exchange), start, end),
        ).fetchall()
    if relation is None or relation["relation"] is None:
        raise RuntimeError("ref.trade_calendar is missing")
    snapshot_sha256, normalized = canonical_snapshot(rows, start=start, end=end)
    return {
        "migration_id": MIGRATION_ID,
        "exchange": exchange,
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
        "row_count": len(normalized),
        "open_day_count": sum(is_open for _, is_open in normalized),
        "snapshot_sha256": snapshot_sha256,
        "ready": True,
    }


def apply(market_data_version: str, start: date, end: date, exchange: str) -> dict[str, object]:
    if not market_data_version.startswith("mhf-v1-") or len(market_data_version) != 71:
        raise ValueError("market_data_version must be an mhf-v1 sha256 version")
    storage_exchange = _storage_exchange(exchange)
    with _connect() as connection:
        connection.execute(_DDL)
        rows = connection.execute(
            "select trade_date,is_open from ref.trade_calendar "
            "where exchange=%s and trade_date between %s and %s order by trade_date",
            (storage_exchange, start, end),
        ).fetchall()
        snapshot_sha256, normalized = canonical_snapshot(rows, start=start, end=end)
        connection.executemany(
            "insert into readmodel.trade_calendar_snapshot_row "
            "(snapshot_sha256,exchange,trade_date,is_open) values (%s,%s,%s,%s) on conflict do nothing",
            [(snapshot_sha256, exchange, trade_date, is_open) for trade_date, is_open in normalized],
        )
        connection.execute(
            "insert into audit.trade_calendar_publication "
            "(market_data_version,exchange,range_start,range_end,snapshot_sha256,row_count,open_day_count) "
            "values (%s,%s,%s,%s,%s,%s,%s) on conflict do nothing",
            (
                market_data_version,
                exchange,
                start,
                end,
                snapshot_sha256,
                len(normalized),
                sum(is_open for _, is_open in normalized),
            ),
        )
    return verify(market_data_version, start, end, exchange)


def verify(market_data_version: str, start: date, end: date, exchange: str) -> dict[str, object]:
    with _connect() as connection:
        publication = connection.execute(
            "select snapshot_sha256,row_count,open_day_count from audit.trade_calendar_publication "
            "where market_data_version=%s and exchange=%s and range_start=%s and range_end=%s",
            (market_data_version, exchange, start, end),
        ).fetchone()
        if publication is None:
            raise RuntimeError("calendar publication is missing")
        rows = connection.execute(
            "select trade_date,is_open from readmodel.trade_calendar_snapshot_row "
            "where snapshot_sha256=%s and exchange=%s and trade_date between %s and %s order by trade_date",
            (publication["snapshot_sha256"], exchange, start, end),
        ).fetchall()
    snapshot_sha256, normalized = canonical_snapshot(rows, start=start, end=end)
    if snapshot_sha256 != publication["snapshot_sha256"]:
        raise RuntimeError("calendar publication checksum mismatch")
    if len(normalized) != int(publication["row_count"]):
        raise RuntimeError("calendar publication row count mismatch")
    open_day_count = sum(is_open for _, is_open in normalized)
    if open_day_count != int(publication["open_day_count"]):
        raise RuntimeError("calendar publication open-day count mismatch")
    return {
        "migration_id": MIGRATION_ID,
        "market_data_version": market_data_version,
        "exchange": exchange,
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
        "snapshot_sha256": snapshot_sha256,
        "row_count": len(normalized),
        "open_day_count": open_day_count,
        "verified": True,
    }


def _storage_exchange(exchange: str) -> str:
    return {"SSE": "SHSE", "SH": "SHSE", "SZ": "SZSE", "BSE": "BJSE"}.get(exchange.upper(), exchange.upper())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("preflight", "apply", "verify"))
    parser.add_argument("--market-data-version", required=True)
    parser.add_argument("--exchange", default="SSE")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "preflight":
        result = preflight(args.start, args.end, args.exchange)
    elif args.action == "apply":
        result = apply(args.market_data_version, args.start, args.end, args.exchange)
    else:
        result = verify(args.market_data_version, args.start, args.end, args.exchange)
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
