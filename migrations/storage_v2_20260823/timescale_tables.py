from __future__ import annotations

"""Storage v2 migration core for the four allowlisted Timescale bar tables."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import time
from typing import Any, Iterable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[str, ...]
    keys: tuple[str, ...]
    key_types: tuple[str, ...]
    indexes: tuple[tuple[str, str], ...]
    segmentby: str
    foreign_key: tuple[str, str] | None = None
    columnstore: bool = True

    @property
    def source(self) -> str:
        return f"fact.{self.name}"

    @property
    def shadow(self) -> str:
        return f"fact.{self.name}_ts_shadow"

    @property
    def legacy(self) -> str:
        return f"fact.{self.name}_legacy"

    @property
    def failed(self) -> str:
        return f"fact.{self.name}_ts_shadow_failed"


STOCK_COLUMNS = ("market", "code", "bar_time", "open", "high", "low", "close", "volume", "amount", "loaded_at")
SPECS = {
    "stock_bar_1m": TableSpec(
        "stock_bar_1m", STOCK_COLUMNS, ("market", "code", "bar_time"),
        ("character varying", "character(6)", "timestamp without time zone"),
        (("code_time_idx", "code,bar_time"), ("time_idx", "bar_time desc")), "market,code",
    ),
    "stock_bar_5m": TableSpec(
        "stock_bar_5m", STOCK_COLUMNS, ("market", "code", "bar_time"),
        ("character varying", "character(6)", "timestamp without time zone"),
        (("code_time_idx", "code,bar_time"), ("time_idx", "bar_time desc")), "market,code",
    ),
    "stock_bar_30m": TableSpec(
        "stock_bar_30m", STOCK_COLUMNS, ("market", "code", "bar_time"),
        ("character varying", "character(6)", "timestamp without time zone"),
        (("code_time_idx", "code,bar_time"), ("time_idx", "bar_time desc")), "market,code",
    ),
    "future_bar_1m": TableSpec(
        "future_bar_1m",
        ("product_code", "exchange", "series_type", "bar_time", "open", "high", "low", "close", "volume", "open_interest", "adjustment_offset", "source_key", "loaded_at"),
        ("product_code", "exchange", "series_type", "bar_time"),
        ("text", "text", "text", "timestamp without time zone"),
        (("time_idx", "bar_time,product_code,series_type"),),
        "exchange,product_code,series_type",
        ("product_code,exchange,series_type", "ref.future_series(product_code,exchange,series_type)"),
        False,
    ),
}


def _connect(*, autocommit: bool = False):
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"], port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"], user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"], connect_timeout=10,
        application_name="markethub-remaining-bars-timescale", row_factory=dict_row,
        autocommit=autocommit,
    )


def _month_after(value: date) -> date:
    return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)


def _months(first: datetime, last: datetime) -> Iterable[date]:
    current = first.date().replace(day=1)
    final = last.date().replace(day=1)
    while current <= final:
        yield current
        current = _month_after(current)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(fraction * len(ordered) + 0.999999) - 1))]


def _ensure_ledger(cursor) -> None:
    cursor.execute("create schema if not exists audit")
    cursor.execute(
        """create table if not exists audit.timescale_shadow_migration (
             source_table text not null,target_table text not null,month_start date not null,
             status text not null check(status in ('copying','verified','failed')),
             started_at_utc timestamptz not null,finished_at_utc timestamptz,
             source_evidence jsonb,target_evidence jsonb,error text,
             primary key(source_table,target_table,month_start))"""
    )
    cursor.execute(
        """create table if not exists audit.timescale_shadow_cutover (
             source_table text primary key,target_table text not null,legacy_table text not null,
             cutover_at_utc timestamptz not null,reverse_mirror_removed_at_utc timestamptz,
             accelerated_acceptance_sha256 text,verification_evidence_sha256 text,verified_rows bigint)"""
    )
    cursor.execute("alter table audit.timescale_shadow_cutover add column if not exists accelerated_acceptance_sha256 text")
    cursor.execute("alter table audit.timescale_shadow_cutover add column if not exists verification_evidence_sha256 text")
    cursor.execute("alter table audit.timescale_shadow_cutover add column if not exists verified_rows bigint")
    cursor.execute("alter table audit.timescale_shadow_cutover add column if not exists legacy_removed_at_utc timestamptz")
    cursor.execute("alter table audit.timescale_shadow_cutover add column if not exists legacy_removed_bytes bigint")
    cursor.execute(
        """create table if not exists audit.timescale_shadow_verification (
             source_table text primary key,target_table text not null,verified_at_utc timestamptz not null,
             source_rows bigint not null,target_rows bigint not null,evidence_sha256 text not null)"""
    )


def _lock_key(spec: TableSpec, purpose: str) -> str:
    return f"markethub-{spec.name}-timescale-{purpose}"


def _journal(spec: TableSpec, direction: str) -> tuple[str, str, str]:
    if direction == "forward":
        return f"audit.{spec.name}_ts_forward_delta", spec.shadow, f"{spec.name}_ts_forward_journal"
    if direction == "reverse":
        return f"audit.{spec.name}_ts_reverse_delta", spec.legacy, f"{spec.name}_ts_reverse_journal"
    raise ValueError(direction)


def _journal_sql(spec: TableSpec, direction: str) -> tuple[str, ...]:
    journal, _, prefix = _journal(spec, direction)
    if direction == "reverse":
        delete_function = f"""create or replace function audit.{prefix}_delete() returns trigger language plpgsql security definer set search_path=pg_catalog,fact,audit as $$ begin
              if current_setting('markethub.explicit_range_journal',true)='on' then return old; end if;
              insert into {journal}(range_start,range_end) values(old.bar_time,old.bar_time); return old; end $$"""
        delete_trigger = f"create trigger {prefix}_delete after delete on {spec.source} for each row when (current_setting('markethub.explicit_range_journal',true) is distinct from 'on') execute function audit.{prefix}_delete()"
    else:
        delete_function = f"""create or replace function audit.{prefix}_delete() returns trigger language plpgsql security definer set search_path=pg_catalog,fact,audit as $$ begin
              if current_setting('markethub.explicit_range_journal',true)='on' then return null; end if;
              insert into {journal}(range_start,range_end) select min(bar_time),max(bar_time) from old_rows having count(*)>0; return null; end $$"""
        delete_trigger = f"create trigger {prefix}_delete after delete on {spec.source} referencing old table as old_rows for each statement when (current_setting('markethub.explicit_range_journal',true) is distinct from 'on') execute function audit.{prefix}_delete()"
    return (
        f"create table if not exists {journal}(delta_id bigint generated always as identity,range_start timestamp without time zone not null,range_end timestamp without time zone not null,check(range_start<=range_end),captured_at timestamptz not null default clock_timestamp())",
        f"""create or replace function audit.{prefix}_insert() returns trigger language plpgsql security definer set search_path=pg_catalog,fact,audit as $$ begin
              if current_setting('markethub.explicit_range_journal',true)='on' then return null; end if;
              insert into {journal}(range_start,range_end) select min(bar_time),max(bar_time) from new_rows having count(*)>0; return null; end $$""",
        delete_function,
        f"""create or replace function audit.{prefix}_update() returns trigger language plpgsql security definer set search_path=pg_catalog,fact,audit as $$ begin
              if current_setting('markethub.explicit_range_journal',true)='on' then return null; end if;
              insert into {journal}(range_start,range_end) select min(bar_time),max(bar_time) from new_rows having count(*)>0; return null; end $$""",
        f"""create or replace function audit.{prefix}_old_time() returns trigger language plpgsql security definer set search_path=pg_catalog,fact,audit as $$ begin
              if current_setting('markethub.explicit_range_journal',true)='on' then return old; end if;
              insert into {journal}(range_start,range_end) values(old.bar_time,old.bar_time); return old; end $$""",
        f"create trigger {prefix}_insert after insert on {spec.source} referencing new table as new_rows for each statement when (current_setting('markethub.explicit_range_journal',true) is distinct from 'on') execute function audit.{prefix}_insert()",
        delete_trigger,
        f"create trigger {prefix}_update after update on {spec.source} referencing old table as old_rows new table as new_rows for each statement when (current_setting('markethub.explicit_range_journal',true) is distinct from 'on') execute function audit.{prefix}_update()",
        f"create trigger {prefix}_old_time after update of bar_time on {spec.source} for each row when (old.bar_time is distinct from new.bar_time and current_setting('markethub.explicit_range_journal',true) is distinct from 'on') execute function audit.{prefix}_old_time()",
    )


def _journal_backlog(cursor, journal: str) -> tuple[int, int]:
    cursor.execute("select to_regclass(%s) as relation", (journal,))
    if cursor.fetchone()["relation"] is None:
        return 0, 0
    cursor.execute(f"select count(*)::bigint as n from {journal}")
    count = int(cursor.fetchone()["n"])
    return count, count


def _drop_journal(cursor, spec: TableSpec, direction: str, *, require_empty: bool) -> int:
    journal, _, prefix = _journal(spec, direction)
    cursor.execute("select to_regclass(%s) as relation", (journal,))
    present = cursor.fetchone()["relation"] is not None
    backlog, _ = _journal_backlog(cursor, journal) if present else (0, 0)
    if require_empty and backlog:
        raise RuntimeError(f"{direction} journal backlog is not empty: {backlog}")
    for suffix in ("insert", "delete", "update", "old_key", "old_time"):
        cursor.execute(f"drop trigger if exists {prefix}_{suffix} on {spec.source}")
        cursor.execute(f"drop function if exists audit.{prefix}_{suffix}()")
    return backlog


def create_shadow(spec: TableSpec) -> dict[str, object]:
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (_lock_key(spec, "migration"),))
        _ensure_ledger(cursor)
        cursor.execute("select to_regclass(%s) as relation", (spec.shadow,))
        if cursor.fetchone()["relation"] is not None:
            raise RuntimeError(f"shadow already exists: {spec.shadow}")
        cursor.execute(f"create table {spec.shadow}(like {spec.source} including defaults including generated including identity including storage including comments)")
        cursor.execute("select create_hypertable(%s::regclass,by_range('bar_time',interval '14 days'),create_default_indexes=>false)", (spec.shadow,))
        cursor.execute(f"alter table {spec.shadow} add constraint {spec.name}_ts_shadow_pkey primary key({','.join(spec.keys)})")
        for suffix, expression in spec.indexes:
            cursor.execute(f"create index {spec.name}_ts_shadow_{suffix} on {spec.shadow}({expression})")
        if spec.columnstore:
            cursor.execute(
                f"alter table {spec.shadow} set (timescaledb.enable_columnstore=true,timescaledb.segmentby='{spec.segmentby}',timescaledb.orderby='bar_time ASC')"
            )
            cursor.execute("call add_columnstore_policy(%s::regclass,after=>interval '30 days',if_not_exists=>true)", (spec.shadow,))
        cursor.execute("select pg_get_userbyid(relowner) as owner from pg_class where oid=%s::regclass", (spec.source,))
        cursor.execute(sql.SQL("alter table {} owner to {}").format(sql.Identifier("fact", f"{spec.name}_ts_shadow"), sql.Identifier(cursor.fetchone()["owner"])))
        connection.commit()
    return {"source": spec.source, "shadow": spec.shadow, "chunk": "14 days", "segmentby": spec.segmentby, "orderby": "bar_time ASC", "columnstore": spec.columnstore}


def set_secondary_indexes(spec: TableSpec, *, present: bool, apply: bool) -> dict[str, object]:
    if not apply:
        raise RuntimeError("secondary index change requires --apply")
    with _connect(autocommit=True) as connection, connection.cursor() as cursor:
        if present:
            for suffix, expression in spec.indexes:
                cursor.execute(f"create index if not exists {spec.name}_ts_shadow_{suffix} on {spec.shadow}({expression})")
            cursor.execute(f"analyze {spec.shadow}")
        else:
            for suffix, _ in spec.indexes:
                cursor.execute(f"drop index if exists fact.{spec.name}_ts_shadow_{suffix}")
    return {"table": spec.name, "secondary_indexes": "present" if present else "suspended"}


def install_journal(spec: TableSpec, direction: str) -> dict[str, object]:
    journal, target, prefix = _journal(spec, direction)
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (_lock_key(spec, "journal"),))
        for relation in (spec.source, target):
            cursor.execute("select to_regclass(%s) is not null as present", (relation,))
            if not cursor.fetchone()["present"]:
                raise RuntimeError(f"missing relation: {relation}")
        _drop_journal(cursor, spec, direction, require_empty=True)
        cursor.execute("select to_regclass(%s) as relation", (journal,))
        if cursor.fetchone()["relation"] is not None:
            cursor.execute(f"drop table {journal}")
        for statement in _journal_sql(spec, direction):
            cursor.execute(statement)
        cursor.execute("select pg_get_userbyid(relowner) as owner from pg_class where oid=%s::regclass", (spec.source,))
        cursor.execute(sql.SQL("alter table {} owner to {}").format(sql.Identifier(*journal.split(".")), sql.Identifier(cursor.fetchone()["owner"])))
        connection.commit()
    return {"direction": direction, "source": spec.source, "target": target, "journal": journal, "trigger_prefix": prefix, "triggers": 4}


def journal_status(spec: TableSpec, direction: str) -> dict[str, object]:
    journal, target, prefix = _journal(spec, direction)
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select to_regclass(%s) as relation", (journal,))
        present = cursor.fetchone()["relation"] is not None
        backlog, batches = _journal_backlog(cursor, journal) if present else (0, 0)
        cursor.execute("select count(*)::int as n from pg_trigger where tgrelid=%s::regclass and not tgisinternal and tgname like %s", (spec.source, prefix + "_%"))
        triggers = int(cursor.fetchone()["n"])
        connection.rollback()
    return {"direction": direction, "source": spec.source, "target": target, "journal": journal, "present": present, "triggers": triggers, "backlog": backlog, "journal_batches": batches}


def reconcile_journal(spec: TableSpec, direction: str, *, batch_size: int = 100_000, max_batches: int | None = None) -> dict[str, object]:
    if batch_size < 1 or batch_size > 1_000_000:
        raise ValueError("invalid batch size")
    journal, target, _ = _journal(spec, direction)
    keys = ",".join(spec.keys)
    key_placeholders = ",".join("%s" for _ in spec.keys)
    source_key_tuple = "(" + ",".join("s." + key for key in spec.keys) + ")"
    target_key_tuple = "(" + ",".join("t." + key for key in spec.keys) + ")"
    assignments = ",".join(f"{column}=excluded.{column}" for column in spec.columns if column not in spec.keys)
    key_defs = ",".join(f"{name} {kind} not null" for name, kind in zip(spec.keys, spec.key_types, strict=True))
    processed = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (_lock_key(spec, "journal"),))
            # Journal batches are intentionally small relative to the fact and
            # legacy tables. Keep reconciliation on their primary-key indexes;
            # a generic temp-table estimate can otherwise select a table scan.
            cursor.execute("set local enable_seqscan=off")
            cursor.execute("create temporary table bar_delta_ranges(delta_id bigint primary key,range_start timestamp without time zone not null,range_end timestamp without time zone not null) on commit drop")
            cursor.execute(
                f"""insert into bar_delta_ranges(delta_id,range_start,range_end)
                    select delta_id,range_start,range_end from {journal}
                    order by delta_id limit %s for update skip locked""",
                (batch_size,),
            )
            selected_batches = cursor.rowcount
            if selected_batches == 0:
                connection.rollback()
                break
            cursor.execute(f"create temporary table bar_delta_batch({key_defs},primary key({keys})) on commit drop")
            cursor.execute(
                f"""insert into bar_delta_batch({keys})
                    select {','.join('s.' + key for key in spec.keys)} from bar_delta_ranges r
                    cross join lateral (
                      select {keys} from {spec.source}
                       where bar_time between r.range_start and r.range_end offset 0
                    ) s
                    union
                    select {','.join('t.' + key for key in spec.keys)} from bar_delta_ranges r
                    cross join lateral (
                      select {keys} from {target}
                       where bar_time between r.range_start and r.range_end offset 0
                    ) t"""
            )
            selected_keys = cursor.rowcount
            cursor.execute(f"select {keys} from bar_delta_batch")
            batch_keys = [tuple(row[key] for key in spec.keys) for row in cursor.fetchall()]
            if batch_keys:
                # Execute constant-key lookups so the planner cannot turn a
                # tiny journal batch into a scan of a billion-row legacy table.
                cursor.executemany(
                    f"""delete from {target} t
                        where {target_key_tuple}=({key_placeholders})
                          and not exists(
                            select 1 from {spec.source} s
                             where {source_key_tuple}=({key_placeholders})
                          )""",
                    [key + key for key in batch_keys],
                )
                cursor.executemany(
                    f"""insert into {target}({','.join(spec.columns)})
                        select {','.join('s.' + column for column in spec.columns)}
                          from {spec.source} s
                         where {source_key_tuple}=({key_placeholders})
                        on conflict({keys}) do update set {assignments}""",
                    batch_keys,
                )
            cursor.execute(f"delete from {journal} j using bar_delta_ranges r where j.delta_id=r.delta_id")
            if cursor.rowcount != selected_batches:
                raise RuntimeError("journal delete mismatch")
            connection.commit()
        processed += selected_keys
        batches += 1
    remaining = journal_status(spec, direction)["backlog"]
    return {"direction": direction, "processed": processed, "batches": batches, "remaining": remaining, "target": target}


def probe_journal(spec: TableSpec, direction: str) -> dict[str, object]:
    journal, target, _ = _journal(spec, direction)
    status = journal_status(spec, direction)
    if status["triggers"] != 4 or status["backlog"] != 0:
        raise RuntimeError(f"journal is not ready for probe: {status}")
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(f"select {','.join(spec.columns)} from {spec.source} where close is not null limit 1")
        sample = dict(cursor.fetchone())
        connection.rollback()
    sample["bar_time"] = datetime(2099, 1, 2, 9, 31)
    key = tuple(sample[column] for column in spec.keys)
    placeholders = ",".join("%s" for _ in spec.columns)
    operations = []
    try:
        with _connect() as connection, connection.cursor() as cursor:
            for relation in (spec.source, target):
                cursor.execute(f"select count(*)::int as n from {relation} where ({','.join(spec.keys)})=({','.join('%s' for _ in spec.keys)})", key)
                if int(cursor.fetchone()["n"]):
                    raise RuntimeError(f"probe key already exists: {relation}")
            cursor.execute(f"insert into {spec.source}({','.join(spec.columns)}) values({placeholders})", tuple(sample[column] for column in spec.columns))
            connection.commit()
        operations.append({"operation": "insert", "reconcile": reconcile_journal(spec, direction)})
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"update {spec.source} set close=close+0.125 where ({','.join(spec.keys)})=({','.join('%s' for _ in spec.keys)}) returning close", key)
            expected_close = cursor.fetchone()["close"]
            connection.commit()
        operations.append({"operation": "update", "reconcile": reconcile_journal(spec, direction)})
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"select close from {target} where ({','.join(spec.keys)})=({','.join('%s' for _ in spec.keys)})", key)
            target_close = cursor.fetchone()["close"]
            connection.rollback()
        if target_close != expected_close:
            raise RuntimeError(f"journal update mismatch: {expected_close}/{target_close}")
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"delete from {spec.source} where ({','.join(spec.keys)})=({','.join('%s' for _ in spec.keys)})", key)
            connection.commit()
        operations.append({"operation": "delete", "reconcile": reconcile_journal(spec, direction)})
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"select count(*)::int as n from {target} where ({','.join(spec.keys)})=({','.join('%s' for _ in spec.keys)})", key)
            remaining = int(cursor.fetchone()["n"])
            connection.rollback()
        if remaining:
            raise RuntimeError("journal delete probe did not remove target")
    finally:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"delete from {spec.source} where ({','.join(spec.keys)})=({','.join('%s' for _ in spec.keys)})", key)
            cursor.execute(f"delete from {target} where ({','.join(spec.keys)})=({','.join('%s' for _ in spec.keys)})", key)
            cursor.execute(f"delete from {journal} where range_start=%s and range_end=%s", (sample["bar_time"], sample["bar_time"]))
            connection.commit()
    final = journal_status(spec, direction)
    if final["backlog"]:
        raise RuntimeError(f"probe left journal backlog: {final}")
    return {"table": spec.name, "direction": direction, "key": key, "operations": operations, "cleanup_complete": True, "final": final}


def benchmark(spec: TableSpec, *, mode: str, iterations: int, row_counts: tuple[int, ...]) -> dict[str, object]:
    if mode not in {"baseline", "journaled", "paired", "paired-explicit", "paired-executemany"} or iterations < 1:
        raise ValueError("invalid benchmark")
    prefix = _journal(spec, "forward")[2]
    expected = 0 if mode == "baseline" else 4
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select count(*)::int as n from pg_trigger where tgrelid=%s::regclass and not tgisinternal and tgname like %s", (spec.source, prefix + "_%"))
        actual = int(cursor.fetchone()["n"])
        connection.rollback()
    if actual != expected:
        raise RuntimeError(f"{mode} requires {expected} journal triggers, found {actual}")
    results: dict[str, object] = {}
    for rows in row_counts:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"select min(bar_time) as first,max(bar_time) as last from (select bar_time from {spec.source} order by bar_time limit %s) selected", (rows,))
            affected_range = cursor.fetchone()
            cursor.execute(f"select {','.join(spec.keys)} from {spec.source} order by bar_time limit %s", (rows,))
            execute_many_params = [tuple(row[key] for key in spec.keys) for row in cursor.fetchall()]
            connection.rollback()
        timings: dict[str, list[float]] = {"baseline": [], "journaled": []}
        variants = ("baseline",) if mode == "baseline" else ("journaled",) if mode == "journaled" else ("baseline", "journaled")
        for iteration in range(iterations):
            ordered_variants = variants if iteration % 2 == 0 else tuple(reversed(variants))
            for variant in ordered_variants:
                with _connect() as connection, connection.cursor() as cursor:
                    if mode in {"paired", "paired-explicit", "paired-executemany"} and variant == "baseline":
                        for suffix in ("insert", "delete", "update", "old_time"):
                            cursor.execute(f"alter table {spec.source} disable trigger {prefix}_{suffix}")
                    started = time.perf_counter()
                    if mode in {"paired-explicit", "paired-executemany"} and variant == "journaled":
                        cursor.execute("set local markethub.explicit_range_journal='on'")
                    if mode == "paired-executemany":
                        cursor.executemany(
                            f"update {spec.source} set close=close where ({','.join(spec.keys)})=({','.join('%s' for _ in spec.keys)})",
                            execute_many_params,
                        )
                    else:
                        cursor.execute(f"with selected as(select ctid from {spec.source} order by bar_time limit %s) update {spec.source} b set close=b.close from selected s where b.ctid=s.ctid", (rows,))
                    if mode in {"paired-explicit", "paired-executemany"} and variant == "journaled":
                        cursor.execute(f"insert into {_journal(spec, 'forward')[0]}(range_start,range_end) values(%s,%s)", (affected_range["first"], affected_range["last"]))
                    elapsed = (time.perf_counter() - started) * 1000
                    connection.rollback()
                    timings[variant].append(elapsed)
        summarized = {}
        for variant in variants:
            values = timings[variant]
            summarized[variant] = {"p50_ms": round(statistics.median(values), 3), "p95_ms": round(_percentile(values, .95), 3), "max_ms": round(max(values), 3)}
        if mode in {"paired", "paired-explicit", "paired-executemany"}:
            baseline_p95 = summarized["baseline"]["p95_ms"]
            journaled_p95 = summarized["journaled"]["p95_ms"]
            summarized["p95_change_percent"] = round((journaled_p95 / baseline_p95 - 1) * 100, 2)
            results[str(rows)] = summarized
        else:
            results[str(rows)] = summarized[mode]
    return {"table": spec.name, "mode": mode, "iterations": iterations, "results": results}


def _evidence(cursor, spec: TableSpec, relation: str, start: date, end: date) -> dict[str, object]:
    if relation not in {spec.source, spec.shadow, spec.legacy}:
        raise ValueError(relation)
    keys = ",".join(spec.keys)
    concatenated = ",".join(f"coalesce({column}::text,'')" for column in spec.columns)
    nullable = [column for column in spec.columns if column not in spec.keys]
    null_pairs = ",".join(f"'{column}',count(*) filter(where {column} is null)" for column in nullable)
    cursor.execute(
        f"""select count(*)::bigint as row_count,count(*)::bigint as primary_key_count,
                   min(bar_time) as min_time,max(bar_time) as max_time,
                   jsonb_build_object({null_pairs}) as null_counts,
                   sum(hashtextextended(concat_ws(chr(31),{concatenated}),0)::numeric)::text as stable_hash_sum
              from {relation} where bar_time >= %s::timestamp and bar_time < %s::timestamp""",
        (start, end),
    )
    return dict(cursor.fetchone())


def _assert_primary_key(cursor, spec: TableSpec, relation: str) -> str:
    cursor.execute(
        """select pg_get_constraintdef(oid) as definition from pg_constraint
             where conrelid=%s::regclass and contype='p' and convalidated""",
        (relation,),
    )
    row = cursor.fetchone()
    expected = "PRIMARY KEY (" + ", ".join(spec.keys) + ")"
    if row is None or row["definition"] != expected:
        raise RuntimeError(f"primary key contract mismatch: {relation}/{row}")
    return str(row["definition"])


def backfill(spec: TableSpec, *, start_month: date | None = None, end_month: date | None = None) -> dict[str, object]:
    if start_month is not None and start_month.day != 1:
        raise ValueError("start month must be the first day of a month")
    if end_month is not None and end_month.day != 1:
        raise ValueError("end month must be the first day of a month")
    if start_month is not None and end_month is not None and start_month > end_month:
        raise ValueError("start month must not be after end month")
    status = journal_status(spec, "forward")
    if status["triggers"] != 4:
        raise RuntimeError("backfill requires four forward journal triggers")
    with _connect() as ledger_connection, ledger_connection.cursor() as cursor:
        _ensure_ledger(cursor)
        source_primary_key = _assert_primary_key(cursor, spec, spec.source)
        target_primary_key = _assert_primary_key(cursor, spec, spec.shadow)
        ledger_connection.commit()
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(f"select min(bar_time) as first,max(bar_time) as last from {spec.source}")
        bounds = cursor.fetchone()
        connection.rollback()
    if bounds["first"] is None or bounds["last"] is None:
        return {
            "table": spec.name,
            "requested_month_range": {"start": start_month, "end": end_month},
            "months": [],
            "primary_keys": {"source": source_primary_key, "target": target_primary_key},
        }
    completed: list[dict[str, object]] = []
    for month in _months(bounds["first"], bounds["last"]):
        if start_month is not None and month < start_month:
            continue
        if end_month is not None and month > end_month:
            continue
        following = _month_after(month)
        started = time.perf_counter()
        with _connect() as connection, connection.cursor() as cursor:
            connection.execute("set transaction isolation level repeatable read")
            connection.execute("set local max_parallel_workers_per_gather=6")
            cursor.execute("select status from audit.timescale_shadow_migration where source_table=%s and target_table=%s and month_start=%s", (spec.source, spec.shadow, month))
            existing = cursor.fetchone()
            if existing is not None and existing["status"] == "verified":
                connection.rollback()
                completed.append({"month": month, "status": "already_verified"})
                continue
            cursor.execute(
                """insert into audit.timescale_shadow_migration(source_table,target_table,month_start,status,started_at_utc)
                   values(%s,%s,%s,'copying',clock_timestamp()) on conflict(source_table,target_table,month_start)
                   do update set status='copying',started_at_utc=excluded.started_at_utc,finished_at_utc=null,error=null""",
                (spec.source, spec.shadow, month),
            )
            source = _evidence(cursor, spec, spec.source, month, following)
            cursor.execute(
                f"""insert into {spec.shadow}({','.join(spec.columns)})
                    select {','.join(spec.columns)} from {spec.source}
                    where bar_time >= %s::timestamp and bar_time < %s::timestamp
                    on conflict({','.join(spec.keys)}) do nothing""", (month, following),
            )
            target = _evidence(cursor, spec, spec.shadow, month, following)
            if source != target:
                raise RuntimeError(f"monthly evidence mismatch: {spec.name}/{month}")
            cursor.execute(
                """update audit.timescale_shadow_migration set status='verified',finished_at_utc=clock_timestamp(),source_evidence=%s::jsonb,target_evidence=%s::jsonb,error=null
                   where source_table=%s and target_table=%s and month_start=%s""",
                (json.dumps(source, default=str), json.dumps(target, default=str), spec.source, spec.shadow, month),
            )
            connection.commit()
        drained = reconcile_journal(spec, "forward", max_batches=1_000)
        completed.append({"month": month, "status": "verified", "rows": source["row_count"], "seconds": round(time.perf_counter() - started, 3), "journal": drained})
    return {
        "table": spec.name,
        "requested_month_range": {"start": start_month, "end": end_month},
        "months": completed,
        "primary_keys": {"source": source_primary_key, "target": target_primary_key},
    }


def verify(spec: TableSpec, *, workers: int = 1, writers_paused: bool = False) -> dict[str, object]:
    if workers < 1 or workers > 8:
        raise ValueError("verification workers must be between 1 and 8")
    if workers > 1 and not writers_paused:
        raise RuntimeError("parallel verification requires --writers-paused")
    with _connect() as ledger_connection, ledger_connection.cursor() as cursor:
        _ensure_ledger(cursor)
        _assert_primary_key(cursor, spec, spec.source)
        _assert_primary_key(cursor, spec, spec.shadow)
        ledger_connection.commit()
    reconcile = reconcile_journal(spec, "forward")
    if reconcile["remaining"] != 0:
        raise RuntimeError("forward journal did not drain")
    if workers > 1:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"select min(bar_time) as first,max(bar_time) as last from {spec.source}")
            bounds = cursor.fetchone()
            connection.rollback()
        months = [] if bounds["first"] is None else list(_months(bounds["first"], bounds["last"]))

        def compare_month(month: date) -> dict[str, object]:
            following = _month_after(month)
            with _connect() as connection, connection.cursor() as cursor:
                connection.execute("set transaction isolation level repeatable read")
                cursor.execute(f"set local max_parallel_workers_per_gather={max(1, 8 // workers)}")
                source = _evidence(cursor, spec, spec.source, month, following)
                target = _evidence(cursor, spec, spec.shadow, month, following)
                if source != target:
                    raise RuntimeError(f"full verification mismatch: {spec.name}/{month}")
                connection.rollback()
            return {"month": month, "evidence": source}

        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(compare_month, months))
        source_rows = sum(int(item["evidence"]["row_count"]) for item in results)
        evidence_sha256 = hashlib.sha256(
            json.dumps(results, default=str, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with _connect() as connection, connection.cursor() as cursor:
            if spec.foreign_key is not None:
                columns, reference = spec.foreign_key
                name = f"{spec.name}_ts_shadow_series_fkey"
                cursor.execute("select 1 from pg_constraint where conrelid=%s::regclass and conname=%s", (spec.shadow, name))
                if cursor.fetchone() is None:
                    cursor.execute(f"alter table {spec.shadow} add constraint {name} foreign key({columns}) references {reference} not valid")
                cursor.execute(f"alter table {spec.shadow} validate constraint {name}")
            cursor.execute(
                """insert into audit.timescale_shadow_verification(source_table,target_table,verified_at_utc,source_rows,target_rows,evidence_sha256)
                   values(%s,%s,clock_timestamp(),%s,%s,%s) on conflict(source_table) do update set
                   target_table=excluded.target_table,verified_at_utc=excluded.verified_at_utc,source_rows=excluded.source_rows,
                   target_rows=excluded.target_rows,evidence_sha256=excluded.evidence_sha256""",
                (spec.source, spec.shadow, source_rows, source_rows, evidence_sha256),
            )
            connection.commit()
        return {
            "table": spec.name,
            "workers": workers,
            "writers_paused": True,
            "months": results,
            "journal": reconcile,
            "foreign_key_validated": spec.foreign_key is not None,
            "source_rows": source_rows,
            "target_rows": source_rows,
            "evidence_sha256": evidence_sha256,
        }
    with _connect() as connection, connection.cursor() as cursor:
        connection.execute("set transaction isolation level repeatable read")
        cursor.execute("set local max_parallel_workers_per_gather=6")
        cursor.execute(f"select min(bar_time) as first,max(bar_time) as last from {spec.source}")
        bounds = cursor.fetchone()
        results = []
        for month in (() if bounds["first"] is None else _months(bounds["first"], bounds["last"])):
            following = _month_after(month)
            source = _evidence(cursor, spec, spec.source, month, following)
            target = _evidence(cursor, spec, spec.shadow, month, following)
            if source != target:
                raise RuntimeError(f"full verification mismatch: {spec.name}/{month}")
            results.append({"month": month, "evidence": source})
        if spec.foreign_key is not None:
            columns, reference = spec.foreign_key
            name = f"{spec.name}_ts_shadow_series_fkey"
            cursor.execute("select 1 from pg_constraint where conrelid=%s::regclass and conname=%s", (spec.shadow, name))
            if cursor.fetchone() is None:
                cursor.execute(f"alter table {spec.shadow} add constraint {name} foreign key({columns}) references {reference} not valid")
            cursor.execute(f"alter table {spec.shadow} validate constraint {name}")
        source_rows = sum(int(item["evidence"]["row_count"]) for item in results)
        evidence_sha256 = hashlib.sha256(
            json.dumps(results, default=str, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        cursor.execute(
            """insert into audit.timescale_shadow_verification(source_table,target_table,verified_at_utc,source_rows,target_rows,evidence_sha256)
               values(%s,%s,clock_timestamp(),%s,%s,%s) on conflict(source_table) do update set
               target_table=excluded.target_table,verified_at_utc=excluded.verified_at_utc,source_rows=excluded.source_rows,
               target_rows=excluded.target_rows,evidence_sha256=excluded.evidence_sha256""",
            (spec.source, spec.shadow, source_rows, source_rows, evidence_sha256),
        )
        connection.commit()
    return {
        "table": spec.name,
        "months": results,
        "journal": reconcile,
        "foreign_key_validated": spec.foreign_key is not None,
        "source_rows": source_rows,
        "target_rows": source_rows,
        "evidence_sha256": evidence_sha256,
    }


def convert_historical(spec: TableSpec, *, workers: int = 1) -> dict[str, object]:
    if workers < 1 or workers > 8:
        raise ValueError("conversion workers must be between 1 and 8")
    if not spec.columnstore:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute("select hypertable_size(%s::regclass)::bigint as bytes", (spec.shadow,))
            before = int(cursor.fetchone()["bytes"])
            cursor.execute("call remove_columnstore_policy(%s::regclass,if_exists=>true)", (spec.shadow,))
            cursor.execute(
                """select quote_ident(chunk_schema)||'.'||quote_ident(chunk_name) as chunk
                     from timescaledb_information.chunks
                    where hypertable_schema='fact' and hypertable_name=%s and is_compressed order by range_start""",
                (spec.name + "_ts_shadow",),
            )
            compressed_chunks = [row["chunk"] for row in cursor.fetchall()]
            connection.commit()
        converted = []
        for chunk in compressed_chunks:
            with _connect() as connection, connection.cursor() as cursor:
                cursor.execute("call convert_to_rowstore(%s::regclass,if_columnstore=>true)", (chunk,))
                connection.commit()
            converted.append(chunk)
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"alter table {spec.shadow} set (timescaledb.enable_columnstore=false)")
            cursor.execute("select hypertable_size(%s::regclass)::bigint as bytes", (spec.shadow,))
            after = int(cursor.fetchone()["bytes"])
            cursor.execute(
                """select count(*)::int as total,count(*) filter(where is_compressed)::int as columnstore
                     from timescaledb_information.chunks
                    where hypertable_schema='fact' and hypertable_name=%s""",
                (spec.name + "_ts_shadow",),
            )
            status = dict(cursor.fetchone())
            connection.commit()
        return {
            "table": spec.name,
            "storage_mode": "rowstore_required_for_foreign_key",
            "converted_to_rowstore_chunks": converted,
            "before_bytes": before,
            "after_bytes": after,
            "chunk_status": status,
        }
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select hypertable_size(%s::regclass)::bigint as bytes", (spec.shadow,))
        before = int(cursor.fetchone()["bytes"])
        cursor.execute("select show_chunks(%s::regclass,older_than=>localtimestamp-interval '30 days')::text as chunk order by 1", (spec.shadow,))
        chunks = [row["chunk"] for row in cursor.fetchall()]
        connection.rollback()
    def convert_chunk(chunk: str) -> str:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute("call convert_to_columnstore(%s::regclass,if_not_columnstore=>true)", (chunk,))
            connection.commit()
        return chunk

    with ThreadPoolExecutor(max_workers=workers) as executor:
        converted = list(executor.map(convert_chunk, chunks))
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select hypertable_size(%s::regclass)::bigint as bytes", (spec.shadow,))
        after = int(cursor.fetchone()["bytes"])
        cursor.execute("select count(*)::int as total,count(*) filter(where is_compressed)::int as columnstore from timescaledb_information.chunks where hypertable_schema='fact' and hypertable_name=%s", (spec.name + "_ts_shadow",))
        status = dict(cursor.fetchone())
        connection.rollback()
    return {"table": spec.name, "workers": workers, "eligible_chunks": len(chunks), "converted_chunks": converted, "before_bytes": before, "after_bytes": after, "compression_percent": round((1 - after / before) * 100, 2) if before else 0, "chunk_status": status}


def _contract(cursor, spec: TableSpec) -> dict[str, object]:
    cursor.execute("select pg_get_userbyid(relowner) as owner from pg_class where oid=%s::regclass", (spec.source,))
    owner = cursor.fetchone()["owner"]
    cursor.execute("select grantee,privilege_type,is_grantable from information_schema.role_table_grants where table_schema='fact' and table_name=%s order by 1,2", (spec.name,))
    grants = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """select vn.nspname as schema_name,v.relname,v.relkind from pg_depend d join pg_rewrite r on r.oid=d.objid
           join pg_class v on v.oid=r.ev_class join pg_namespace vn on vn.oid=v.relnamespace where d.refobjid=%s::regclass""",
        (spec.source,),
    )
    dependents = [dict(row) for row in cursor.fetchall()]
    cursor.execute("select conname,conrelid::regclass::text from pg_constraint where confrelid=%s::regclass", (spec.source,))
    references = [dict(row) for row in cursor.fetchall()]
    if dependents or references:
        raise RuntimeError(f"OID-bound dependents: {json.dumps({'relations': dependents, 'constraints': references}, default=str)}")
    return {"owner": owner, "grants": grants}


def _restore_contract(cursor, spec: TableSpec, contract: dict[str, object]) -> None:
    cursor.execute(sql.SQL("alter table {} owner to {}").format(sql.Identifier("fact", spec.name), sql.Identifier(str(contract["owner"]))))
    allowed = {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
    for grant in contract["grants"]:
        if grant["grantee"] == contract["owner"]:
            continue
        privilege = str(grant["privilege_type"]).upper()
        if privilege not in allowed:
            raise RuntimeError(privilege)
        role = sql.SQL("PUBLIC") if grant["grantee"] == "PUBLIC" else sql.Identifier(str(grant["grantee"]))
        statement = sql.SQL("grant {} on {} to {}").format(sql.SQL(privilege), sql.Identifier("fact", spec.name), role)
        if grant["is_grantable"] == "YES":
            statement += sql.SQL(" with grant option")
        cursor.execute(statement)


def cutover(spec: TableSpec, *, apply: bool) -> dict[str, object]:
    if not apply:
        raise RuntimeError("cutover requires --apply while writers are paused")
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (_lock_key(spec, "migration"),))
        _ensure_ledger(cursor)
        contract = _contract(cursor, spec)
        journal = _journal(spec, "forward")[0]
        cursor.execute(f"select count(*)::bigint as n from {journal}")
        if int(cursor.fetchone()["n"]):
            raise RuntimeError("forward journal backlog must be zero")
        cursor.execute(
            """select source_rows,target_rows,evidence_sha256 from audit.timescale_shadow_verification
               where source_table=%s and target_table=%s for update""",
            (spec.source, spec.shadow),
        )
        verification = cursor.fetchone()
        if verification is None or int(verification["source_rows"]) != int(verification["target_rows"]):
            raise RuntimeError("current full verification evidence is required before cutover")
        cursor.execute(f"lock table {spec.source},{spec.shadow} in access exclusive mode")
        cursor.execute(f"select count(*)::bigint as n from {journal}")
        if int(cursor.fetchone()["n"]):
            raise RuntimeError("forward journal changed after full verification")
        _drop_journal(cursor, spec, "forward", require_empty=True)
        cursor.execute(f"alter table {spec.source} rename constraint {spec.name}_pkey to {spec.name}_legacy_pkey")
        for suffix, _ in spec.indexes:
            cursor.execute(f"alter index fact.{spec.name}_{suffix} rename to {spec.name}_legacy_{suffix}")
        if spec.foreign_key is not None:
            cursor.execute(f"alter table {spec.source} rename constraint {spec.name}_product_code_exchange_series_type_fkey to {spec.name}_legacy_series_fkey")
        cursor.execute(f"alter table {spec.source} rename to {spec.name}_legacy")
        cursor.execute(f"alter table {spec.shadow} rename to {spec.name}")
        cursor.execute(f"alter table {spec.source} rename constraint {spec.name}_ts_shadow_pkey to {spec.name}_pkey")
        for suffix, _ in spec.indexes:
            cursor.execute(f"alter index fact.{spec.name}_ts_shadow_{suffix} rename to {spec.name}_{suffix}")
        if spec.foreign_key is not None:
            cursor.execute(f"alter table {spec.source} rename constraint {spec.name}_ts_shadow_series_fkey to {spec.name}_product_code_exchange_series_type_fkey")
        _restore_contract(cursor, spec, contract)
        _drop_journal(cursor, spec, "reverse", require_empty=True)
        for statement in _journal_sql(spec, "reverse"):
            cursor.execute(statement)
        reverse = _journal(spec, "reverse")[0]
        cursor.execute(f"alter table {reverse} owner to datalake")
        cursor.execute(
            """insert into audit.timescale_shadow_cutover(
                 source_table,target_table,legacy_table,cutover_at_utc,verification_evidence_sha256,verified_rows)
               values(%s,%s,%s,clock_timestamp(),%s,%s) on conflict(source_table) do update set
               target_table=excluded.target_table,legacy_table=excluded.legacy_table,
               cutover_at_utc=excluded.cutover_at_utc,reverse_mirror_removed_at_utc=null,
               accelerated_acceptance_sha256=null,verification_evidence_sha256=excluded.verification_evidence_sha256,
               verified_rows=excluded.verified_rows""",
            (spec.source, spec.shadow, spec.legacy, verification["evidence_sha256"], verification["source_rows"]),
        )
        cursor.execute("delete from audit.timescale_shadow_verification where source_table=%s", (spec.source,))
        connection.commit()
    with _connect(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(f"analyze {spec.source}")
    return {
        "table": spec.name,
        "rows": int(verification["source_rows"]),
        "legacy": spec.legacy,
        "reverse_journal": "installed",
        "verification_evidence_sha256": verification["evidence_sha256"],
    }


def rollback(spec: TableSpec, *, apply: bool) -> dict[str, object]:
    if not apply:
        raise RuntimeError("rollback requires --apply while writers are paused")
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select to_regclass(%s) as failed", (spec.failed,))
        if cursor.fetchone()["failed"] is not None:
            raise RuntimeError("failed hypertable already exists")
        cursor.execute(f"lock table {spec.source},{spec.legacy} in access exclusive mode")
        _drop_journal(cursor, spec, "reverse", require_empty=True)
        cursor.execute(f"alter table {spec.source} rename constraint {spec.name}_pkey to {spec.name}_ts_shadow_failed_pkey")
        for suffix, _ in spec.indexes:
            cursor.execute(f"alter index fact.{spec.name}_{suffix} rename to {spec.name}_ts_shadow_failed_{suffix}")
        if spec.foreign_key is not None:
            cursor.execute(f"alter table {spec.source} rename constraint {spec.name}_product_code_exchange_series_type_fkey to {spec.name}_ts_shadow_failed_series_fkey")
        cursor.execute(f"alter table {spec.source} rename to {spec.name}_ts_shadow_failed")
        cursor.execute(f"alter table {spec.legacy} rename to {spec.name}")
        cursor.execute(f"alter table {spec.source} rename constraint {spec.name}_legacy_pkey to {spec.name}_pkey")
        for suffix, _ in spec.indexes:
            cursor.execute(f"alter index fact.{spec.name}_legacy_{suffix} rename to {spec.name}_{suffix}")
        if spec.foreign_key is not None:
            cursor.execute(f"alter table {spec.source} rename constraint {spec.name}_legacy_series_fkey to {spec.name}_product_code_exchange_series_type_fkey")
        connection.commit()
    return {"table": spec.name, "legacy_restored": True, "failed_hypertable": spec.failed}


def prepare_retry(spec: TableSpec, *, apply: bool) -> dict[str, object]:
    if not apply:
        raise RuntimeError("retry requires --apply while writers are paused")
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(f"lock table {spec.source},{spec.failed} in access exclusive mode")
        cursor.execute(f"alter table {spec.failed} rename constraint {spec.name}_ts_shadow_failed_pkey to {spec.name}_ts_shadow_pkey")
        for suffix, _ in spec.indexes:
            cursor.execute(f"alter index fact.{spec.name}_ts_shadow_failed_{suffix} rename to {spec.name}_ts_shadow_{suffix}")
        if spec.foreign_key is not None:
            cursor.execute(f"alter table {spec.failed} rename constraint {spec.name}_ts_shadow_failed_series_fkey to {spec.name}_ts_shadow_series_fkey")
        cursor.execute(f"alter table {spec.failed} rename to {spec.name}_ts_shadow")
        _drop_journal(cursor, spec, "forward", require_empty=True)
        for statement in _journal_sql(spec, "forward"):
            cursor.execute(statement)
        cursor.execute(
            """select verification_evidence_sha256,verified_rows from audit.timescale_shadow_cutover
               where source_table=%s for update""",
            (spec.source,),
        )
        evidence = cursor.fetchone()
        if evidence is None or evidence["verification_evidence_sha256"] is None or evidence["verified_rows"] is None:
            raise RuntimeError("rollback drill verification evidence is missing")
        cursor.execute(
            """insert into audit.timescale_shadow_verification(source_table,target_table,verified_at_utc,source_rows,target_rows,evidence_sha256)
               values(%s,%s,clock_timestamp(),%s,%s,%s) on conflict(source_table) do update set
               target_table=excluded.target_table,verified_at_utc=excluded.verified_at_utc,source_rows=excluded.source_rows,
               target_rows=excluded.target_rows,evidence_sha256=excluded.evidence_sha256""",
            (spec.source, spec.shadow, evidence["verified_rows"], evidence["verified_rows"], evidence["verification_evidence_sha256"]),
        )
        connection.commit()
    return {
        "table": spec.name,
        "shadow_restored": True,
        "forward_journal": "installed",
        "verification_evidence_sha256": evidence["verification_evidence_sha256"],
    }


def remove_reverse(spec: TableSpec, *, apply: bool, evidence_sha256: str | None) -> dict[str, object]:
    if not apply or evidence_sha256 is None or re.fullmatch(r"[0-9a-f]{64}", evidence_sha256) is None:
        raise RuntimeError("reverse removal requires --apply and lowercase evidence SHA-256")
    with _connect() as connection, connection.cursor() as cursor:
        _drop_journal(cursor, spec, "reverse", require_empty=True)
        _drop_journal(cursor, spec, "forward", require_empty=True)
        removed_tables: list[str] = []
        for direction in ("reverse", "forward"):
            journal, _, _ = _journal(spec, direction)
            cursor.execute("select to_regclass(%s) as relation", (journal,))
            if cursor.fetchone()["relation"] is not None:
                cursor.execute(f"drop table {journal}")
                removed_tables.append(journal)
        cursor.execute("update audit.timescale_shadow_cutover set reverse_mirror_removed_at_utc=clock_timestamp(),accelerated_acceptance_sha256=%s where source_table=%s", (evidence_sha256, spec.source))
        if cursor.rowcount != 1:
            raise RuntimeError("cutover ledger missing")
        connection.commit()
    return {"table": spec.name, "reverse_journal": "removed", "migration_journal_tables": removed_tables, "legacy": "retained_read_only", "evidence_sha256": evidence_sha256}


def relation_status(spec: TableSpec) -> dict[str, object]:
    with _connect() as connection, connection.cursor() as cursor:
        _ensure_ledger(cursor)
        relations: dict[str, bool] = {}
        for label, relation in (("canonical", spec.source), ("shadow", spec.shadow), ("legacy", spec.legacy), ("failed", spec.failed)):
            cursor.execute("select to_regclass(%s) is not null as present", (relation,))
            relations[label] = bool(cursor.fetchone()["present"])
        hypertable = False
        if relations["canonical"]:
            cursor.execute(
                "select exists(select 1 from timescaledb_information.hypertables where hypertable_schema='fact' and hypertable_name=%s) as present",
                (spec.name,),
            )
            hypertable = bool(cursor.fetchone()["present"])
        cursor.execute(
            """select verification_evidence_sha256,verified_rows,accelerated_acceptance_sha256,
                      reverse_mirror_removed_at_utc,legacy_removed_at_utc,legacy_removed_bytes
                 from audit.timescale_shadow_cutover where source_table=%s""",
            (spec.source,),
        )
        ledger = cursor.fetchone()
        connection.commit()
    return {
        "table": spec.name,
        "relations": relations,
        "canonical_hypertable": hypertable,
        "cutover": None if ledger is None else dict(ledger),
    }


def cleanup_legacy(spec: TableSpec, *, apply: bool, acceptance_sha256: str | None) -> dict[str, object]:
    if not apply or acceptance_sha256 is None or re.fullmatch(r"[0-9a-f]{64}", acceptance_sha256) is None:
        raise RuntimeError("legacy cleanup requires --apply and lowercase acceptance SHA-256")
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (_lock_key(spec, "legacy-cleanup"),))
        _ensure_ledger(cursor)
        cursor.execute(
            """select accelerated_acceptance_sha256,reverse_mirror_removed_at_utc,
                      legacy_removed_at_utc,legacy_removed_bytes
                 from audit.timescale_shadow_cutover where source_table=%s for update""",
            (spec.source,),
        )
        ledger = cursor.fetchone()
        if ledger is None or ledger["accelerated_acceptance_sha256"] != acceptance_sha256:
            raise RuntimeError("acceptance SHA-256 does not match the cutover ledger")
        if ledger["reverse_mirror_removed_at_utc"] is None:
            raise RuntimeError("reverse journal must be removed before legacy cleanup")
        cursor.execute(
            "select exists(select 1 from timescaledb_information.hypertables where hypertable_schema='fact' and hypertable_name=%s) as present",
            (spec.name,),
        )
        if not cursor.fetchone()["present"]:
            raise RuntimeError(f"canonical relation is not a hypertable: {spec.source}")
        for relation in (spec.shadow, spec.failed):
            cursor.execute("select to_regclass(%s) is not null as present", (relation,))
            if cursor.fetchone()["present"]:
                raise RuntimeError(f"migration residue still exists: {relation}")
        for direction in ("forward", "reverse"):
            journal, _, prefix = _journal(spec, direction)
            cursor.execute("select to_regclass(%s) is not null as present", (journal,))
            if cursor.fetchone()["present"]:
                raise RuntimeError(f"migration journal still exists: {journal}")
            cursor.execute(
                "select count(*)::int as n from pg_trigger where tgrelid=%s::regclass and not tgisinternal and tgname like %s",
                (spec.source, prefix + "_%"),
            )
            if int(cursor.fetchone()["n"]):
                raise RuntimeError(f"migration trigger still exists: {prefix}")
        cursor.execute("select to_regclass(%s) is not null as present", (spec.legacy,))
        if not cursor.fetchone()["present"]:
            if ledger["legacy_removed_at_utc"] is None:
                raise RuntimeError("legacy relation is missing but cleanup was not recorded")
            connection.commit()
            return {
                "table": spec.name,
                "legacy": "already_removed",
                "removed_bytes": int(ledger["legacy_removed_bytes"] or 0),
                "acceptance_sha256": acceptance_sha256,
            }
        cursor.execute(
            """select exists(
                   select 1 from pg_depend d join pg_rewrite r on r.oid=d.objid
                   where d.refobjid=%s::regclass
                 ) or exists(
                   select 1 from pg_constraint where confrelid=%s::regclass
                 ) as blocked""",
            (spec.legacy, spec.legacy),
        )
        if cursor.fetchone()["blocked"]:
            raise RuntimeError(f"legacy relation still has OID-bound dependents: {spec.legacy}")
        cursor.execute("select pg_total_relation_size(%s::regclass)::bigint as bytes", (spec.legacy,))
        removed_bytes = int(cursor.fetchone()["bytes"])
        cursor.execute(sql.SQL("drop table {}").format(sql.Identifier("fact", f"{spec.name}_legacy")))
        cursor.execute(
            """update audit.timescale_shadow_cutover
                  set legacy_removed_at_utc=clock_timestamp(),legacy_removed_bytes=%s
                where source_table=%s""",
            (removed_bytes, spec.source),
        )
        connection.commit()
    return {
        "table": spec.name,
        "legacy": "removed",
        "removed_bytes": removed_bytes,
        "acceptance_sha256": acceptance_sha256,
    }


def _write(path: Path | None, payload: object) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, path)
    print(encoded, end="")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", choices=tuple(SPECS), required=True)
    parser.add_argument("--output", type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("status")
    actions.add_parser("create-shadow")
    suspend = actions.add_parser("suspend-secondary-indexes"); suspend.add_argument("--apply", action="store_true")
    restore = actions.add_parser("restore-secondary-indexes"); restore.add_argument("--apply", action="store_true")
    install = actions.add_parser("install-journal"); install.add_argument("--direction", choices=("forward", "reverse"), required=True)
    status = actions.add_parser("journal-status"); status.add_argument("--direction", choices=("forward", "reverse"), required=True)
    reconcile = actions.add_parser("reconcile-journal"); reconcile.add_argument("--direction", choices=("forward", "reverse"), required=True); reconcile.add_argument("--batch-size", type=int, default=100_000)
    probe = actions.add_parser("probe-journal"); probe.add_argument("--direction", choices=("forward", "reverse"), required=True)
    bench = actions.add_parser("benchmark"); bench.add_argument("--mode", choices=("baseline", "journaled", "paired", "paired-explicit", "paired-executemany"), required=True); bench.add_argument("--iterations", type=int, default=20); bench.add_argument("--rows", type=int, nargs="+", default=(1_000, 10_000))
    backfill_action = actions.add_parser("backfill")
    backfill_action.add_argument("--start-month", type=date.fromisoformat)
    backfill_action.add_argument("--end-month", type=date.fromisoformat)
    verify_action = actions.add_parser("verify")
    verify_action.add_argument("--workers", type=int, default=1)
    verify_action.add_argument("--writers-paused", action="store_true")
    convert = actions.add_parser("convert-historical"); convert.add_argument("--workers", type=int, default=1)
    cut = actions.add_parser("cutover"); cut.add_argument("--apply", action="store_true")
    roll = actions.add_parser("rollback"); roll.add_argument("--apply", action="store_true")
    retry = actions.add_parser("prepare-retry"); retry.add_argument("--apply", action="store_true")
    remove = actions.add_parser("remove-reverse"); remove.add_argument("--apply", action="store_true"); remove.add_argument("--evidence-sha256")
    cleanup = actions.add_parser("cleanup-legacy"); cleanup.add_argument("--apply", action="store_true"); cleanup.add_argument("--acceptance-sha256")
    args = parser.parse_args()
    spec = SPECS[args.table]
    if args.action == "status": result = relation_status(spec)
    elif args.action == "create-shadow": result = create_shadow(spec)
    elif args.action == "suspend-secondary-indexes": result = set_secondary_indexes(spec, present=False, apply=args.apply)
    elif args.action == "restore-secondary-indexes": result = set_secondary_indexes(spec, present=True, apply=args.apply)
    elif args.action == "install-journal": result = install_journal(spec, args.direction)
    elif args.action == "journal-status": result = journal_status(spec, args.direction)
    elif args.action == "reconcile-journal": result = reconcile_journal(spec, args.direction, batch_size=args.batch_size)
    elif args.action == "probe-journal": result = probe_journal(spec, args.direction)
    elif args.action == "benchmark": result = benchmark(spec, mode=args.mode, iterations=args.iterations, row_counts=tuple(args.rows))
    elif args.action == "backfill": result = backfill(spec, start_month=args.start_month, end_month=args.end_month)
    elif args.action == "verify": result = verify(spec, workers=args.workers, writers_paused=args.writers_paused)
    elif args.action == "convert-historical": result = convert_historical(spec, workers=args.workers)
    elif args.action == "cutover": result = cutover(spec, apply=args.apply)
    elif args.action == "rollback": result = rollback(spec, apply=args.apply)
    elif args.action == "prepare-retry": result = prepare_retry(spec, apply=args.apply)
    elif args.action == "remove-reverse": result = remove_reverse(spec, apply=args.apply, evidence_sha256=args.evidence_sha256)
    else: result = cleanup_legacy(spec, apply=args.apply, acceptance_sha256=args.acceptance_sha256)
    _write(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
