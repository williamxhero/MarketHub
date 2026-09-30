from __future__ import annotations

"""Fetch and apply auditable Tushare full-market adjustment-factor snapshots."""

import argparse
import csv
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from platform_models import AdjFactorItem


SHANGHAI = ZoneInfo("Asia/Shanghai")
A_SHARE_SQL = """
(
  (market = 'SHSE' and left(code, 1) = '6')
  or (market = 'SZSE' and left(code, 1) in ('0', '3'))
  or (market = 'BJSE' and (left(code, 1) in ('4', '8') or left(code, 3) = '920'))
)
"""


def _load_env(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            name, value = raw.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip())


def _connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.getenv("MARKETHUB_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("MARKETHUB_DB_PORT", "5432")),
        dbname=os.getenv("MARKETHUB_DB_NAME", "datalake_dev"),
        user=os.getenv("MARKETHUB_DB_USER", "markethub"),
        password=os.getenv("MARKETHUB_DB_PASSWORD", ""),
        connect_timeout=30,
    )


def _snapshot_handler():
    from quotemux.settings import QuoteMuxSettings
    from quotemux.source_packages import get_default_source_package_registry
    from quotemux.source_packages.instance_context import use_source_instance

    instances = QuoteMuxSettings().get_contract_source_instances("stocks.factors.adj", ("tushare",))
    instance = next((item for item in instances if item.package_id == "tushare"), None)
    if instance is None:
        raise RuntimeError("tushare_source_instance_unavailable")
    handler = get_default_source_package_registry().get_handler("tushare", "get_adj_factor_snapshot")

    def fetch(trade_date: str):
        with use_source_instance(instance):
            return handler(trade_date)

    return fetch


def fetch_snapshots(start_date: str, end_date: str, output_dir: Path, handler=None) -> dict[str, object]:
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""
            select distinct trade_date::text
            from fact.stock_daily_1d
            where trade_date between %s::date and %s::date
              and {A_SHARE_SQL}
              and not coalesce(is_suspended, false)
            order by 1
            """,
            (start_date, end_date),
        )
        trade_dates = [str(row[0]) for row in cursor.fetchall()]
    fetch = handler or _snapshot_handler()
    rows: list[dict[str, object]] = []
    date_counts: dict[str, int] = {}
    for trade_date in trade_dates:
        items = fetch(trade_date)
        if not items:
            raise RuntimeError(f"tushare_adj_factor_snapshot_empty:{trade_date}")
        date_counts[trade_date] = len(items)
        rows.extend(
            {"code": item.code, "trade_date": item.trade_date, "adj_factor": item.adj_factor}
            for item in items
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / f"tushare_adj_factor_{start_date}_{end_date}.csv"
    with raw_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["code", "trade_date", "adj_factor"])
        writer.writeheader()
        writer.writerows(rows)
    digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    manifest = {
        "provider": "tushare.adj_factor",
        "source_version": "tushare.adj_factor.snapshot.v1",
        "captured_at": datetime.now(SHANGHAI).isoformat(),
        "start_date": start_date,
        "end_date": end_date,
        "raw_csv": str(raw_path.resolve()),
        "raw_sha256": digest,
        "rows": len(rows),
        "trade_dates": len(trade_dates),
        "date_min_rows": min(date_counts.values(), default=0),
        "date_max_rows": max(date_counts.values(), default=0),
    }
    manifest_path = raw_path.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _ensure_daily_status_schema(connection: psycopg.Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("create schema if not exists audit")
        cursor.execute(
            """
            create table if not exists audit.stock_adj_factor_daily_status (
                trade_date date primary key,
                status text not null check (status in ('success', 'failed')),
                row_count bigint not null default 0,
                conflict_count bigint not null default 0,
                error_message text not null default '',
                raw_sha256 text,
                attempted_at timestamptz not null default now()
            )
            """
        )
    connection.commit()


def record_day_status(
    connection: psycopg.Connection,
    trade_date: str,
    *,
    status: str,
    row_count: int = 0,
    conflict_count: int = 0,
    error_message: str = "",
    raw_sha256: str | None = None,
) -> None:
    """Persist a durable, retryable per-trading-day ingestion result.

    Upserts so a retried day overwrites its own prior attempt only; history
    for other days, and any already-applied adj_factor cell, is untouched.
    """
    _ensure_daily_status_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into audit.stock_adj_factor_daily_status
                (trade_date, status, row_count, conflict_count, error_message, raw_sha256, attempted_at)
            values (%s::date, %s, %s, %s, %s, %s, now())
            on conflict (trade_date) do update set
                status = excluded.status,
                row_count = excluded.row_count,
                conflict_count = excluded.conflict_count,
                error_message = excluded.error_message,
                raw_sha256 = excluded.raw_sha256,
                attempted_at = excluded.attempted_at
            """,
            (trade_date, status, row_count, conflict_count, error_message, raw_sha256),
        )
    connection.commit()


def list_day_statuses(connection: psycopg.Connection, since_date: str, until_date: str) -> list[dict[str, object]]:
    _ensure_daily_status_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select trade_date::text, status, row_count, conflict_count, error_message, raw_sha256, attempted_at::text
            from audit.stock_adj_factor_daily_status
            where trade_date between %s::date and %s::date
            order by trade_date
            """,
            (since_date, until_date),
        )
        columns = ["trade_date", "status", "row_count", "conflict_count", "error_message", "raw_sha256", "attempted_at"]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def find_incomplete_trade_dates(connection: psycopg.Connection, since_date: str, until_date: str) -> list[str]:
    """A-share trading days with a daily bar but at least one missing adj_factor.

    This is what makes a daily run retryable-by-construction: any day a
    previous run failed (or skipped) simply reappears here on the next run,
    alongside the newest trading day, until it is genuinely complete.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            select distinct trade_date::text
            from fact.stock_daily_1d
            where trade_date between %s::date and %s::date
              and {A_SHARE_SQL}
              and not coalesce(is_suspended, false)
              and adj_factor is null
            order by 1
            """,
            (since_date, until_date),
        )
        return [str(row[0]) for row in cursor.fetchall()]


def run_daily(env_file: Path, output_dir: Path, since_date: str, until_date: str, handler=None) -> dict[str, object]:
    """Ingest every A-share trading day since ``since_date`` still missing factors.

    Never touches a trade_date/code cell that already has a non-null
    adj_factor (``_upsert_stock_adj_factors`` only fills nulls); a provider
    value that disagrees with an already-stored one is detected by
    ``apply_artifact`` and fails that day closed (``adj_factor_existing_conflicts``)
    instead of being applied. Each date's outcome is persisted individually so
    a failed or not-yet-attempted day stays visible and is retried by the next
    run rather than silently skipped; one date's failure does not stop the
    others.
    """
    _load_env(env_file)
    fetch = handler or _snapshot_handler()
    with _connect() as connection:
        trade_dates = find_incomplete_trade_dates(connection, since_date, until_date)
    results: list[dict[str, object]] = []
    for trade_date in trade_dates:
        with _connect() as connection:
            try:
                manifest = fetch_snapshots(trade_date, trade_date, output_dir, handler=fetch)
                manifest_path = Path(manifest["raw_csv"]).with_suffix(".manifest.json")
                outcome = apply_artifact(manifest_path)
                record_day_status(
                    connection,
                    trade_date,
                    status="success",
                    row_count=int(outcome["rows"]),
                    raw_sha256=str(outcome["raw_sha256"]),
                )
                results.append({"trade_date": trade_date, "status": "success", "rows": outcome["rows"]})
            except Exception as exc:
                message = str(exc)
                conflict_count = 0
                if message.startswith("adj_factor_existing_conflicts:"):
                    conflict_count = int(message.split(":", 1)[1])
                record_day_status(
                    connection,
                    trade_date,
                    status="failed",
                    conflict_count=conflict_count,
                    error_message=message,
                )
                results.append({"trade_date": trade_date, "status": "failed", "error": message})
    failed = [item for item in results if item["status"] == "failed"]
    return {
        "since_date": since_date,
        "until_date": until_date,
        "attempted": len(results),
        "succeeded": len(results) - len(failed),
        "failed": len(failed),
        "results": results,
    }


def _coverage(connection: psycopg.Connection, start_date: str, end_date: str) -> dict[str, int]:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            select count(*)::bigint,
                   count(*) filter (where adj_factor is not null and adj_factor > 0)::bigint,
                   count(distinct code)::int,
                   count(distinct code) filter (where adj_factor is not null and adj_factor > 0)::int
            from fact.stock_daily_1d
            where trade_date between %s::date and %s::date
              and {A_SHARE_SQL}
              and not coalesce(is_suspended, false)
            """,
            (start_date, end_date),
        )
        row = cursor.fetchone()
    return {"required_rows": row[0], "factor_rows": row[1], "required_codes": row[2], "factor_codes": row[3]}


def apply_artifact(manifest_path: Path) -> dict[str, object]:
    from quotemux.fact_ref_writes import _upsert_stock_adj_factors

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_path = Path(str(manifest["raw_csv"]))
    digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    if digest != manifest["raw_sha256"]:
        raise RuntimeError("adj_factor_artifact_sha256_mismatch")
    with raw_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    items = [
        AdjFactorItem(code=row["code"], trade_date=row["trade_date"], adj_factor=float(row["adj_factor"]))
        for row in rows
    ]
    with _connect() as connection:
        before = _coverage(connection, manifest["start_date"], manifest["end_date"])
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select code, trade_date::text, adj_factor
                from fact.stock_daily_1d
                where trade_date between %s::date and %s::date and adj_factor is not null
                """,
                (manifest["start_date"], manifest["end_date"]),
            )
            existing = {(str(row[0]), str(row[1])): float(row[2]) for row in cursor.fetchall()}
        conflicts = [
            item for item in items
            if (item.code, f"{item.trade_date[:4]}-{item.trade_date[4:6]}-{item.trade_date[6:8]}") in existing
            and abs(existing[(item.code, f"{item.trade_date[:4]}-{item.trade_date[4:6]}-{item.trade_date[6:8]}")] - float(item.adj_factor)) > 1e-8
        ]
        if conflicts:
            raise RuntimeError(f"adj_factor_existing_conflicts:{len(conflicts)}")
        for offset in range(0, len(items), 5000):
            if not _upsert_stock_adj_factors(items[offset: offset + 5000]):
                raise RuntimeError(f"adj_factor_fact_write_failed:{offset}")
        with connection.cursor() as cursor:
            cursor.execute("create schema if not exists audit")
            cursor.execute(
                """
                create table if not exists audit.stock_adj_factor_import_batch (
                    raw_sha256 text primary key,
                    provider text not null,
                    source_version text not null,
                    captured_at timestamptz not null,
                    start_date date not null,
                    end_date date not null,
                    raw_uri text not null,
                    row_count bigint not null,
                    applied_at timestamptz not null default now()
                )
                """
            )
            cursor.execute(
                """
                insert into audit.stock_adj_factor_import_batch
                  (raw_sha256, provider, source_version, captured_at, start_date, end_date, raw_uri, row_count)
                values (%s, %s, %s, %s::timestamptz, %s::date, %s::date, %s, %s)
                on conflict (raw_sha256) do nothing
                """,
                (
                    digest, manifest["provider"], manifest["source_version"], manifest["captured_at"],
                    manifest["start_date"], manifest["end_date"], str(raw_path), len(items),
                ),
            )
        connection.commit()
        after = _coverage(connection, manifest["start_date"], manifest["end_date"])
    return {"status": "applied", "raw_sha256": digest, "rows": len(items), "before": before, "after": after}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="action", required=True)
    fetch = subparsers.add_parser("fetch")
    fetch.add_argument("--start-date", required=True)
    fetch.add_argument("--end-date", required=True)
    fetch.add_argument("--output-dir", required=True, type=Path)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--manifest", required=True, type=Path)
    daily = subparsers.add_parser("daily", help="Ingest every incomplete A-share trading day since --since-date (retryable, never overwrites a stored factor).")
    daily.add_argument("--since-date", required=True)
    daily.add_argument("--until-date", required=False, default="")
    daily.add_argument("--output-dir", required=True, type=Path)
    status = subparsers.add_parser("status", help="List persisted per-day ingestion status.")
    status.add_argument("--since-date", required=True)
    status.add_argument("--until-date", required=False, default="")
    args = parser.parse_args()
    _load_env(args.env_file)
    if args.action == "fetch":
        result: dict[str, object] = fetch_snapshots(args.start_date, args.end_date, args.output_dir)
    elif args.action == "apply":
        result = apply_artifact(args.manifest)
    elif args.action == "daily":
        until_date = args.until_date or datetime.now(SHANGHAI).strftime("%Y-%m-%d")
        result = run_daily(args.env_file, args.output_dir, args.since_date, until_date)
    else:
        until_date = args.until_date or datetime.now(SHANGHAI).strftime("%Y-%m-%d")
        with _connect() as connection:
            result = {"since_date": args.since_date, "until_date": until_date, "days": list_day_statuses(connection, args.since_date, until_date)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    if args.action == "daily" and int(result.get("failed", 0)) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
