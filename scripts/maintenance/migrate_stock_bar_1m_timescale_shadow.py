from __future__ import annotations

"""Create, mirror, backfill, verify, and cut over stock_bar_1m to Timescale."""

import argparse
from datetime import date, datetime, timezone
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


SOURCE = "fact.stock_bar_1m"
SHADOW = "fact.stock_bar_1m_ts_shadow"
LEGACY = "fact.stock_bar_1m_legacy"
COLUMNS = ("market", "code", "bar_time", "open", "high", "low", "close", "volume", "amount", "loaded_at")
PK = ("market", "code", "bar_time")
ALLOWED_CHUNKS = {"7 days", "14 days", "1 month"}
ALLOWED_ORDERS = {"ASC", "DESC"}
LOCK_KEY = "markethub-stock-bar-1m-timescale-shadow"
FAILED = "fact.stock_bar_1m_ts_shadow_failed"
SAMPLE = "fact.stock_bar_1m_ts_sample"
FORWARD_JOURNAL = "audit.stock_bar_1m_ts_forward_delta"
REVERSE_JOURNAL = "audit.stock_bar_1m_ts_reverse_delta"
JOURNAL_LOCK_KEY = "markethub-stock-bar-1m-timescale-journal"


def _connect(*, autocommit: bool = False):
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        connect_timeout=10,
        application_name="markethub-stock-bar-1m-timescale-shadow",
        row_factory=dict_row,
        autocommit=autocommit,
    )


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(fraction * len(ordered) + 0.999999) - 1))]


def _json_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, default=str).encode()).hexdigest()


def _month_after(value: date) -> date:
    return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)


def _month_starts(first: datetime, last: datetime) -> Iterable[date]:
    current = first.date().replace(day=1)
    final = last.date().replace(day=1)
    while current <= final:
        yield current
        current = _month_after(current)


def _ensure_ledger(cursor) -> None:
    cursor.execute("create schema if not exists audit")
    cursor.execute(
        """
        create table if not exists audit.timescale_shadow_migration (
            source_table text not null,
            target_table text not null,
            month_start date not null,
            status text not null check(status in ('copying','verified','failed')),
            started_at_utc timestamptz not null,
            finished_at_utc timestamptz,
            source_evidence jsonb,
            target_evidence jsonb,
            error text,
            primary key(source_table,target_table,month_start)
        )
        """
    )
    cursor.execute(
        """
        create table if not exists audit.timescale_shadow_cutover (
            source_table text primary key,
            target_table text not null,
            legacy_table text not null,
            cutover_at_utc timestamptz not null,
            reverse_mirror_removed_at_utc timestamptz,
            accelerated_acceptance_sha256 text
        )
        """
    )
    cursor.execute(
        "alter table audit.timescale_shadow_cutover add column if not exists accelerated_acceptance_sha256 text"
    )


def create_shadow(chunk_interval: str, order: str) -> dict[str, object]:
    if chunk_interval not in ALLOWED_CHUNKS or order not in ALLOWED_ORDERS:
        raise ValueError("unsupported chunk/order")
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (LOCK_KEY,))
        _ensure_ledger(cursor)
        cursor.execute("select to_regclass(%s) as table_name", (SHADOW,))
        if cursor.fetchone()["table_name"] is not None:
            raise RuntimeError(f"shadow already exists: {SHADOW}")
        cursor.execute(
            """create table fact.stock_bar_1m_ts_shadow (
                   like fact.stock_bar_1m including defaults including generated including identity including storage including comments
               )"""
        )
        cursor.execute(
            "select create_hypertable(%s::regclass,by_range('bar_time',%s::interval),create_default_indexes=>false)",
            (SHADOW, chunk_interval),
        )
        cursor.execute("alter table fact.stock_bar_1m_ts_shadow add constraint stock_bar_1m_ts_shadow_pkey primary key(market,code,bar_time)")
        cursor.execute("create index stock_bar_1m_ts_shadow_code_time_idx on fact.stock_bar_1m_ts_shadow(code,bar_time)")
        cursor.execute("create index stock_bar_1m_ts_shadow_time_idx on fact.stock_bar_1m_ts_shadow(bar_time desc)")
        cursor.execute(
            f"alter table fact.stock_bar_1m_ts_shadow set (timescaledb.enable_columnstore=true,timescaledb.segmentby='market,code',timescaledb.orderby='bar_time {order}')"
        )
        cursor.execute("call add_columnstore_policy(%s::regclass,after=>interval '30 days',if_not_exists=>true)", (SHADOW,))
        cursor.execute("alter table fact.stock_bar_1m_ts_shadow owner to datalake")
        connection.commit()
    return {"shadow": SHADOW, "chunk_interval": chunk_interval, "segmentby": "market,code", "orderby": f"bar_time {order}", "rowstore_window": "30 days"}


def set_secondary_indexes(*, present: bool, apply: bool) -> dict[str, object]:
    if not apply:
        raise RuntimeError("secondary index change requires --apply")
    definitions = (
        ("stock_bar_1m_ts_shadow_code_time_idx", "code,bar_time"),
        ("stock_bar_1m_ts_shadow_time_idx", "bar_time desc"),
    )
    with _connect(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("select to_regclass(%s) is not null as present", (SHADOW,))
        if not cursor.fetchone()["present"]:
            raise RuntimeError(f"missing shadow: {SHADOW}")
        if present:
            for name, expression in definitions:
                cursor.execute(f"create index if not exists {name} on {SHADOW}({expression})")
            cursor.execute(f"analyze {SHADOW}")
        else:
            for name, _ in definitions:
                cursor.execute(f"drop index if exists fact.{name}")
    return {"shadow": SHADOW, "secondary_indexes": "present" if present else "suspended", "definitions": [name for name, _ in definitions]}


def _mirror_sql(source_name: str, target_name: str, prefix: str) -> tuple[str, ...]:
    if (source_name, target_name, prefix) not in {
        (SOURCE, SHADOW, "stock_bar_1m_ts_forward"),
        (SOURCE, LEGACY, "stock_bar_1m_ts_reverse"),
    }:
        raise ValueError("mirror identifiers are not allowlisted")
    source = source_name
    target = target_name
    columns = ",".join(COLUMNS)
    assignments = ",".join(f"{column}=excluded.{column}" for column in COLUMNS if column not in PK)
    if prefix == "stock_bar_1m_ts_reverse":
        delete_function = f"""create or replace function audit.{prefix}_delete() returns trigger language plpgsql security definer set search_path=pg_catalog,fact,audit as $$ begin
               delete from {target} t where (t.market,t.code,t.bar_time)=(old.market,old.code,old.bar_time); return old; end $$"""
        delete_trigger = f"create trigger {prefix}_delete after delete on {source} for each row execute function audit.{prefix}_delete()"
    else:
        delete_function = f"""create or replace function audit.{prefix}_delete() returns trigger language plpgsql security definer set search_path=pg_catalog,fact,audit as $$ begin
               delete from {target} t using old_rows o where (t.market,t.code,t.bar_time)=(o.market,o.code,o.bar_time); return null; end $$"""
        delete_trigger = f"create trigger {prefix}_delete after delete on {source} referencing old table as old_rows for each statement execute function audit.{prefix}_delete()"
    return (
        f"""create or replace function audit.{prefix}_insert() returns trigger language plpgsql security definer set search_path=pg_catalog,fact,audit as $$ begin
               insert into {target}({columns}) select {columns} from new_rows
               on conflict(market,code,bar_time) do update set {assignments}; return null; end $$""",
        delete_function,
        f"""create or replace function audit.{prefix}_update() returns trigger language plpgsql security definer set search_path=pg_catalog,fact,audit as $$ begin
               delete from {target} t using old_rows o where (t.market,t.code,t.bar_time)=(o.market,o.code,o.bar_time);
               insert into {target}({columns}) select {columns} from new_rows
               on conflict(market,code,bar_time) do update set {assignments}; return null; end $$""",
        f"create trigger {prefix}_insert after insert on {source} referencing new table as new_rows for each statement execute function audit.{prefix}_insert()",
        delete_trigger,
        f"create trigger {prefix}_update after update on {source} referencing old table as old_rows new table as new_rows for each statement execute function audit.{prefix}_update()",
    )


def install_forward_mirror() -> dict[str, object]:
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (LOCK_KEY,))
        for name in (SOURCE, SHADOW):
            cursor.execute("select to_regclass(%s) is not null as present", (name,))
            if not cursor.fetchone()["present"]:
                raise RuntimeError(f"missing table: {name}")
        for suffix in ("insert", "delete", "update"):
            cursor.execute(f"drop trigger if exists stock_bar_1m_ts_forward_{suffix} on fact.stock_bar_1m")
        for statement in _mirror_sql(SOURCE, SHADOW, "stock_bar_1m_ts_forward"):
            cursor.execute(statement)
        connection.commit()
    return {"source": SOURCE, "target": SHADOW, "triggers": 3}


def remove_forward_mirror() -> dict[str, object]:
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (LOCK_KEY,))
        for suffix in ("insert", "delete", "update"):
            cursor.execute(f"drop trigger if exists stock_bar_1m_ts_forward_{suffix} on fact.stock_bar_1m")
            cursor.execute(f"drop function if exists audit.stock_bar_1m_ts_forward_{suffix}()")
        connection.commit()
    return {"source": SOURCE, "target": SHADOW, "triggers": 0}


def _journal_contract(direction: str) -> tuple[str, str, str]:
    contracts = {
        "forward": (FORWARD_JOURNAL, SHADOW, "stock_bar_1m_ts_forward_journal"),
        "reverse": (REVERSE_JOURNAL, LEGACY, "stock_bar_1m_ts_reverse_journal"),
    }
    try:
        return contracts[direction]
    except KeyError as exc:
        raise ValueError(f"unsupported journal direction: {direction}") from exc


def _journal_sql(direction: str) -> tuple[str, ...]:
    journal, _, prefix = _journal_contract(direction)
    if direction == "reverse":
        delete_function = f"""create or replace function audit.{prefix}_delete() returns trigger
            language plpgsql security definer set search_path=pg_catalog,fact,audit as $$ begin
              insert into {journal}(market,code,bar_time) values(old.market,old.code,old.bar_time);
              return old;
            end $$"""
        delete_trigger = f"create trigger {prefix}_delete after delete on {SOURCE} for each row execute function audit.{prefix}_delete()"
    else:
        delete_function = f"""create or replace function audit.{prefix}_delete() returns trigger
            language plpgsql security definer set search_path=pg_catalog,fact,audit as $$ begin
              insert into {journal}(market,code,bar_time)
              select market,code,bar_time from old_rows;
              return null;
            end $$"""
        delete_trigger = f"create trigger {prefix}_delete after delete on {SOURCE} referencing old table as old_rows for each statement execute function audit.{prefix}_delete()"
    return (
        f"""create table if not exists {journal} (
                delta_id bigint generated always as identity primary key,
                market character varying not null,
                code character(6) not null,
                bar_time timestamp without time zone not null,
                captured_at timestamptz not null default clock_timestamp()
            )""",
        f"""create or replace function audit.{prefix}_insert() returns trigger
            language plpgsql security definer set search_path=pg_catalog,fact,audit as $$ begin
              insert into {journal}(market,code,bar_time)
              select market,code,bar_time from new_rows;
              return null;
            end $$""",
        delete_function,
        f"""create or replace function audit.{prefix}_update() returns trigger
            language plpgsql security definer set search_path=pg_catalog,fact,audit as $$ begin
              insert into {journal}(market,code,bar_time)
              select market,code,bar_time from old_rows
              except select market,code,bar_time from new_rows;
              insert into {journal}(market,code,bar_time)
              select market,code,bar_time from new_rows;
              return null;
            end $$""",
        f"create trigger {prefix}_insert after insert on {SOURCE} referencing new table as new_rows for each statement execute function audit.{prefix}_insert()",
        delete_trigger,
        f"create trigger {prefix}_update after update on {SOURCE} referencing old table as old_rows new table as new_rows for each statement execute function audit.{prefix}_update()",
    )


def _drop_journal_objects(cursor, direction: str, *, require_empty: bool) -> int:
    journal, _, prefix = _journal_contract(direction)
    cursor.execute("select to_regclass(%s) as relation", (journal,))
    present = cursor.fetchone()["relation"] is not None
    backlog = 0
    if present:
        cursor.execute(f"select count(*)::bigint as n from {journal}")
        backlog = int(cursor.fetchone()["n"])
    if require_empty and backlog:
        raise RuntimeError(f"{direction} journal backlog is not empty: {backlog}")
    for suffix in ("insert", "delete", "update"):
        cursor.execute(f"drop trigger if exists {prefix}_{suffix} on {SOURCE}")
        cursor.execute(f"drop function if exists audit.{prefix}_{suffix}()")
    return backlog


def install_key_journal(direction: str) -> dict[str, object]:
    journal, target, prefix = _journal_contract(direction)
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (JOURNAL_LOCK_KEY,))
        for name in (SOURCE, target):
            cursor.execute("select to_regclass(%s) is not null as present", (name,))
            if not cursor.fetchone()["present"]:
                raise RuntimeError(f"missing table: {name}")
        _drop_journal_objects(cursor, direction, require_empty=True)
        for statement in _journal_sql(direction):
            cursor.execute(statement)
        cursor.execute(f"alter table {journal} owner to datalake")
        connection.commit()
    return {"direction": direction, "source": SOURCE, "target": target, "journal": journal, "trigger_prefix": prefix, "triggers": 3}


def remove_key_journal(direction: str, *, require_empty: bool = True) -> dict[str, object]:
    journal, target, _ = _journal_contract(direction)
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (JOURNAL_LOCK_KEY,))
        backlog = _drop_journal_objects(cursor, direction, require_empty=require_empty)
        connection.commit()
    return {"direction": direction, "source": SOURCE, "target": target, "journal": journal, "backlog": backlog, "triggers": 0}


def journal_status(direction: str) -> dict[str, object]:
    journal, target, prefix = _journal_contract(direction)
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select to_regclass(%s) as relation", (journal,))
        present = cursor.fetchone()["relation"] is not None
        backlog = 0
        oldest = None
        if present:
            cursor.execute(f"select count(*)::bigint as n,min(captured_at) as oldest from {journal}")
            row = cursor.fetchone()
            backlog = int(row["n"])
            oldest = row["oldest"]
        cursor.execute(
            """select count(*)::int as n from pg_trigger
               where tgrelid=%s::regclass and not tgisinternal and tgname like %s""",
            (SOURCE, prefix + "_%"),
        )
        triggers = int(cursor.fetchone()["n"])
        connection.rollback()
    return {"direction": direction, "source": SOURCE, "target": target, "journal": journal, "present": present, "triggers": triggers, "backlog": backlog, "oldest": oldest}


def reconcile_journal(direction: str, *, batch_size: int = 100_000, max_batches: int | None = None) -> dict[str, object]:
    if batch_size < 1 or batch_size > 1_000_000 or (max_batches is not None and max_batches < 1):
        raise ValueError("invalid journal reconcile bounds")
    journal, target, _ = _journal_contract(direction)
    processed = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (JOURNAL_LOCK_KEY,))
            cursor.execute(
                """create temporary table stock_bar_1m_delta_batch (
                       delta_id bigint primary key,
                       market character varying not null,
                       code character(6) not null,
                       bar_time timestamp without time zone not null
                   ) on commit drop"""
            )
            cursor.execute(
                f"""insert into stock_bar_1m_delta_batch(delta_id,market,code,bar_time)
                    select delta_id,market,code,bar_time from {journal}
                    order by delta_id limit %s for update skip locked""",
                (batch_size,),
            )
            selected = cursor.rowcount
            if selected == 0:
                connection.rollback()
                break
            cursor.execute(
                f"""delete from {target} t using (
                        select distinct market,code,bar_time from stock_bar_1m_delta_batch
                    ) k
                    where (t.market,t.code,t.bar_time)=(k.market,k.code,k.bar_time)
                      and not exists (
                        select 1 from {SOURCE} s
                        where (s.market,s.code,s.bar_time)=(k.market,k.code,k.bar_time)
                      )"""
            )
            deleted = cursor.rowcount
            cursor.execute(
                f"""insert into {target}({','.join(COLUMNS)})
                    select {','.join('s.' + column for column in COLUMNS)}
                    from {SOURCE} s
                    join (select distinct market,code,bar_time from stock_bar_1m_delta_batch) k
                      on (s.market,s.code,s.bar_time)=(k.market,k.code,k.bar_time)
                    on conflict(market,code,bar_time) do update set
                      open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                      volume=excluded.volume,amount=excluded.amount,loaded_at=excluded.loaded_at"""
            )
            upserted = cursor.rowcount
            cursor.execute(f"delete from {journal} j using stock_bar_1m_delta_batch b where j.delta_id=b.delta_id")
            if cursor.rowcount != selected:
                raise RuntimeError(f"journal delete mismatch: selected={selected} deleted={cursor.rowcount}")
            connection.commit()
        processed += selected
        batches += 1
    status = journal_status(direction)
    return {"direction": direction, "processed": processed, "batches": batches, "remaining": status["backlog"], "target": target}


def probe_key_journal(direction: str) -> dict[str, object]:
    key = ("SHSE", "999999", datetime(2099, 1, 2, 9, 31))
    _, target, _ = _journal_contract(direction)
    operations: list[dict[str, object]] = []
    status = journal_status(direction)
    if status["triggers"] != 3 or status["backlog"] != 0:
        raise RuntimeError(f"{direction} journal is not ready for probe: {status}")
    try:
        with _connect() as connection, connection.cursor() as cursor:
            for table in (SOURCE, target):
                cursor.execute(f"select count(*)::int as n from {table} where (market,code,bar_time)=(%s,%s,%s)", key)
                if int(cursor.fetchone()["n"]) != 0:
                    raise RuntimeError(f"probe key already exists in {table}")
            cursor.execute(f"select {','.join(COLUMNS[3:])} from {SOURCE} limit 1")
            sample = cursor.fetchone()
            cursor.execute(
                f"""insert into {SOURCE}({','.join(COLUMNS)})
                    values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                key + tuple(sample[column] for column in COLUMNS[3:]),
            )
            connection.commit()
        drained = reconcile_journal(direction)
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"select close from {target} where (market,code,bar_time)=(%s,%s,%s)", key)
            inserted = cursor.fetchone()
            if inserted is None:
                raise RuntimeError("journal insert probe did not reach shadow")
            connection.rollback()
        operations.append({"operation": "insert", "reconcile": drained, "matched": True})

        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"update {SOURCE} set close=close+0.125 where (market,code,bar_time)=(%s,%s,%s) returning close", key)
            updated_close = cursor.fetchone()["close"]
            connection.commit()
        drained = reconcile_journal(direction)
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"select close from {target} where (market,code,bar_time)=(%s,%s,%s)", key)
            shadow_close = cursor.fetchone()["close"]
            if shadow_close != updated_close:
                raise RuntimeError(f"journal update probe mismatch: {updated_close}/{shadow_close}")
            connection.rollback()
        operations.append({"operation": "update", "reconcile": drained, "matched": True})

        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"delete from {SOURCE} where (market,code,bar_time)=(%s,%s,%s)", key)
            if cursor.rowcount != 1:
                raise RuntimeError("journal delete probe source row missing")
            connection.commit()
        drained = reconcile_journal(direction)
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"select count(*)::int as n from {target} where (market,code,bar_time)=(%s,%s,%s)", key)
            remaining = int(cursor.fetchone()["n"])
            connection.rollback()
        if remaining != 0:
            raise RuntimeError("journal delete probe did not remove shadow row")
        operations.append({"operation": "delete", "reconcile": drained, "matched": True})
    finally:
        with _connect() as cleanup, cleanup.cursor() as cursor:
            cursor.execute(f"delete from {SOURCE} where (market,code,bar_time)=(%s,%s,%s)", key)
            cursor.execute(f"delete from {target} where (market,code,bar_time)=(%s,%s,%s)", key)
            journal = _journal_contract(direction)[0]
            cursor.execute(
                f"delete from {journal} where (market,code,bar_time)=(%s,%s,%s)",
                key,
            )
            cleanup.commit()
    final = journal_status(direction)
    if final["backlog"] != 0:
        raise RuntimeError(f"journal probe left backlog: {final}")
    return {"direction": direction, "key": key, "operations": operations, "cleanup_complete": True, "final": final}


def benchmark_mirror(iterations: int, row_counts: tuple[int, ...], mode: str) -> dict[str, object]:
    if iterations < 1 or not row_counts or any(value < 1 or value > 10_000 for value in row_counts):
        raise ValueError("invalid benchmark bounds")
    if mode not in {"baseline", "mirrored", "journaled"}:
        raise ValueError("invalid mirror benchmark mode")
    report: dict[str, object] = {}
    with _connect() as connection, connection.cursor() as cursor:
        trigger_pattern = "stock_bar_1m_ts_forward_journal_%" if mode == "journaled" else "stock_bar_1m_ts_forward_%"
        cursor.execute(
            """select count(*)::int as n from pg_trigger
               where tgrelid=%s::regclass and not tgisinternal and tgname like %s""",
            (SOURCE, trigger_pattern),
        )
        trigger_count = int(cursor.fetchone()["n"])
        expected = 0 if mode == "baseline" else 3
        if trigger_count != expected:
            raise RuntimeError(f"{mode} requires {expected} forward triggers, found {trigger_count}")
        connection.rollback()
    for rows in row_counts:
        timings: list[float] = []
        for _ in range(iterations):
            with _connect() as probe, probe.cursor() as probe_cursor:
                started = time.perf_counter()
                probe_cursor.execute(
                    """with selected as (select ctid from fact.stock_bar_1m order by bar_time,market,code limit %s)
                       update fact.stock_bar_1m b set close=b.close from selected s where b.ctid=s.ctid""",
                    (rows,),
                )
                probe.rollback()
                timings.append((time.perf_counter() - started) * 1000)
        report[str(rows)] = {
            "p50_ms": round(statistics.median(timings), 3),
            "p95_ms": round(_percentile(timings, 0.95), 3),
            "max_ms": round(max(timings), 3),
        }
    return {"mode": mode, "iterations": iterations, "results": report}


def probe_reverse_transition() -> dict[str, object]:
    """Exercise the Timescale-compatible reverse trigger mix and roll back."""
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select to_regclass(%s) is not null as present", (SAMPLE,))
        if not cursor.fetchone()["present"]:
            raise RuntimeError(f"missing sample hypertable: {SAMPLE}")
        cursor.execute("create schema if not exists audit")
        cursor.execute("create table audit.stock_bar_1m_ts_transition_probe (like fact.stock_bar_1m_ts_sample including defaults)")
        for operation, relation in (("insert", "new_rows"), ("update", "new_rows")):
            cursor.execute(
                f"""create function audit.stock_bar_1m_ts_transition_probe_{operation}() returns trigger
                    language plpgsql as $$ begin
                      insert into audit.stock_bar_1m_ts_transition_probe({','.join(COLUMNS)})
                      select {','.join(COLUMNS)} from {relation};
                      return null;
                    end $$"""
            )
        cursor.execute(
            """create function audit.stock_bar_1m_ts_transition_probe_delete() returns trigger
                language plpgsql as $$ begin
                  insert into audit.stock_bar_1m_ts_transition_probe(market,code,bar_time,open,high,low,close,volume,amount,loaded_at)
                  values(old.market,old.code,old.bar_time,old.open,old.high,old.low,old.close,old.volume,old.amount,old.loaded_at);
                  return old;
                end $$"""
        )
        cursor.execute(
            """create trigger stock_bar_1m_ts_transition_probe_insert after insert on fact.stock_bar_1m_ts_sample
                 referencing new table as new_rows for each statement
                 execute function audit.stock_bar_1m_ts_transition_probe_insert()"""
        )
        cursor.execute(
            """create trigger stock_bar_1m_ts_transition_probe_delete after delete on fact.stock_bar_1m_ts_sample
                 for each row
                 execute function audit.stock_bar_1m_ts_transition_probe_delete()"""
        )
        cursor.execute(
            """create trigger stock_bar_1m_ts_transition_probe_update after update on fact.stock_bar_1m_ts_sample
                 referencing old table as old_rows new table as new_rows for each statement
                 execute function audit.stock_bar_1m_ts_transition_probe_update()"""
        )
        cursor.execute(f"select {','.join(COLUMNS)} from fact.stock_bar_1m_ts_sample limit 1")
        selected = cursor.fetchone()
        cursor.execute(
            """update fact.stock_bar_1m_ts_sample set close=close
                where (market,code,bar_time)=(%s,%s,%s)""",
            (selected["market"], selected["code"], selected["bar_time"]),
        )
        updated = cursor.rowcount
        inserted_time = datetime(2099, 1, 2, 9, 31)
        cursor.execute(
            """insert into fact.stock_bar_1m_ts_sample(market,code,bar_time,open,high,low,close,volume,amount,loaded_at)
                values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                selected["market"],
                selected["code"],
                inserted_time,
                selected["open"],
                selected["high"],
                selected["low"],
                selected["close"],
                selected["volume"],
                selected["amount"],
                selected["loaded_at"],
            ),
        )
        inserted = cursor.rowcount
        cursor.execute(
            "delete from fact.stock_bar_1m_ts_sample where (market,code,bar_time)=(%s,%s,%s)",
            (selected["market"], selected["code"], inserted_time),
        )
        deleted = cursor.rowcount
        cursor.execute("select count(*)::int as n from audit.stock_bar_1m_ts_transition_probe")
        mirrored = int(cursor.fetchone()["n"])
        if updated != 1 or inserted != 1 or deleted != 1 or mirrored != 3:
            raise RuntimeError(
                f"reverse transition probe mismatch: updated={updated} inserted={inserted} deleted={deleted} mirrored={mirrored}"
            )
        connection.rollback()
    return {
        "hypertable": SAMPLE,
        "updated_rows": updated,
        "inserted_rows": inserted,
        "deleted_rows": deleted,
        "mirrored_rows": mirrored,
        "rolled_back": True,
    }


EVIDENCE_SQL = """
select count(*)::bigint as row_count,
       count(distinct (market,code,bar_time))::bigint as primary_key_count,
       min(bar_time) as min_time,max(bar_time) as max_time,
       jsonb_build_object('amount',count(*) filter(where amount is null)) as null_counts,
       sum(hashtextextended(concat_ws(chr(31),market,code,bar_time::text,open::text,high::text,low::text,close::text,volume::text,coalesce(amount::text,''),loaded_at::text),0)::numeric)::text as stable_hash_sum
from {table}
where bar_time >= %s::timestamp and bar_time < %s::timestamp
"""


def _evidence(cursor, table: str, start: date, end: date) -> dict[str, object]:
    if table not in {SOURCE, SHADOW, LEGACY}:
        raise ValueError(table)
    cursor.execute(EVIDENCE_SQL.format(table=table), (start, end))
    return dict(cursor.fetchone())


def backfill(*, max_months: int | None = None) -> dict[str, object]:
    completed: list[dict[str, object]] = []
    status = journal_status("forward")
    if status["triggers"] != 3:
        raise RuntimeError(f"backfill requires three forward key journal triggers: {status}")
    with _connect() as ledger_connection, ledger_connection.cursor() as cursor:
        _ensure_ledger(cursor)
        ledger_connection.commit()
    with _connect() as bounds_connection, bounds_connection.cursor() as cursor:
        cursor.execute("select min(bar_time) as first,max(bar_time) as last from fact.stock_bar_1m")
        bounds = cursor.fetchone()
        bounds_connection.rollback()
    if bounds is None or bounds["first"] is None:
        raise RuntimeError("source is empty")
    for index, month in enumerate(_month_starts(bounds["first"], bounds["last"])):
        if max_months is not None and index >= max_months:
            break
        following = _month_after(month)
        started = time.perf_counter()
        try:
            with _connect() as connection, connection.cursor() as cursor:
                connection.execute("set transaction isolation level repeatable read")
                connection.execute("set local max_parallel_workers_per_gather=6")
                cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (LOCK_KEY,))
                cursor.execute(
                    "select status from audit.timescale_shadow_migration where source_table=%s and target_table=%s and month_start=%s",
                    (SOURCE, SHADOW, month),
                )
                existing = cursor.fetchone()
                if existing is not None and existing["status"] == "verified":
                    connection.rollback()
                    completed.append({"month": month, "status": "already_verified"})
                    continue
                cursor.execute(
                    """insert into audit.timescale_shadow_migration(source_table,target_table,month_start,status,started_at_utc)
                       values(%s,%s,%s,'copying',clock_timestamp()) on conflict(source_table,target_table,month_start)
                       do update set status='copying',started_at_utc=excluded.started_at_utc,finished_at_utc=null,error=null""",
                    (SOURCE, SHADOW, month),
                )
                source_evidence = _evidence(cursor, SOURCE, month, following)
                # A conflict may already have been reconciled from the key journal.
                # Keeping it avoids replacing a newer source state with this snapshot.
                cursor.execute(
                    """insert into fact.stock_bar_1m_ts_shadow(market,code,bar_time,open,high,low,close,volume,amount,loaded_at)
                       select market,code,bar_time,open,high,low,close,volume,amount,loaded_at from fact.stock_bar_1m
                       where bar_time >= %s::timestamp and bar_time < %s::timestamp
                       on conflict(market,code,bar_time) do nothing""",
                    (month, following),
                )
                target_evidence = _evidence(cursor, SHADOW, month, following)
                if source_evidence != target_evidence:
                    raise RuntimeError(f"monthly evidence mismatch: {month}")
                cursor.execute(
                    """update audit.timescale_shadow_migration set status='verified',finished_at_utc=clock_timestamp(),
                         source_evidence=%s::jsonb,target_evidence=%s::jsonb,error=null
                       where source_table=%s and target_table=%s and month_start=%s""",
                    (json.dumps(source_evidence, default=str), json.dumps(target_evidence, default=str), SOURCE, SHADOW, month),
                )
                connection.commit()
            drained = reconcile_journal("forward", max_batches=1_000)
            completed.append({"month": month, "status": "verified", "rows": source_evidence["row_count"], "elapsed_seconds": round(time.perf_counter() - started, 3), "journal": drained})
        except BaseException as exc:
            with _connect(autocommit=True) as failed, failed.cursor() as cursor:
                _ensure_ledger(cursor)
                cursor.execute(
                    """insert into audit.timescale_shadow_migration(source_table,target_table,month_start,status,started_at_utc,finished_at_utc,error)
                       values(%s,%s,%s,'failed',clock_timestamp(),clock_timestamp(),%s)
                       on conflict(source_table,target_table,month_start) do update set status='failed',finished_at_utc=clock_timestamp(),error=excluded.error""",
                    (SOURCE, SHADOW, month, str(exc)),
                )
            raise
    return {"months": completed}


def inventory() -> dict[str, object]:
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """select c.relname,c.relowner::regrole::text as owner,obj_description(c.oid) as comment,
                      pg_get_userbyid(c.relowner) as owner_name
               from pg_class c where c.oid in (%s::regclass,%s::regclass) order by c.relname""",
            (SOURCE, SHADOW),
        )
        tables = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            """select n.nspname as schema_name,c.relname as table_name,i.relname as index_name,pg_get_indexdef(i.oid) as definition
               from pg_index x join pg_class c on c.oid=x.indrelid join pg_namespace n on n.oid=c.relnamespace join pg_class i on i.oid=x.indexrelid
               where c.oid in (%s::regclass,%s::regclass) order by c.relname,i.relname""",
            (SOURCE, SHADOW),
        )
        indexes = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            """select pg_describe_object(classid,objid,objsubid) as dependency,deptype
               from pg_depend where refobjid=%s::regclass order by 1""",
            (SOURCE,),
        )
        dependencies = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            """select grantee,privilege_type from information_schema.role_table_grants
               where table_schema='fact' and table_name='stock_bar_1m' order by grantee,privilege_type"""
        )
        grants = [dict(row) for row in cursor.fetchall()]
        connection.rollback()
    return {"tables": tables, "indexes": indexes, "dependencies": dependencies, "grants": grants}


def double_read() -> dict[str, object]:
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """select month_start from audit.timescale_shadow_migration
               where source_table=%s and target_table=%s and status='verified' order by month_start""",
            (SOURCE, SHADOW),
        )
        months = [row["month_start"] for row in cursor.fetchall()]
        if not months:
            raise RuntimeError("no verified months are available for double-read")
        selected = sorted({months[0], months[len(months) // 2], months[-1]})
        results: list[dict[str, object]] = []
        for month in selected:
            following = _month_after(month)
            source_evidence = _evidence(cursor, SOURCE, month, following)
            target_evidence = _evidence(cursor, SHADOW, month, following)
            query = """select market,code,count(*) as rows,sum(close::numeric)::text as close_sum,sum(volume::numeric)::text as volume_sum
                       from {table} where bar_time >= %s::timestamp and bar_time < %s::timestamp
                       group by market,code order by market,code"""
            cursor.execute(query.format(table=SOURCE), (month, following))
            source_hash = _json_hash(cursor.fetchall())
            cursor.execute(query.format(table=SHADOW), (month, following))
            target_hash = _json_hash(cursor.fetchall())
            if source_evidence != target_evidence or source_hash != target_hash:
                raise RuntimeError(f"double-read mismatch: {month}")
            results.append({"month": month, "evidence": source_evidence, "result_sha256": source_hash})
        connection.rollback()
    return {"sampled_months": results, "formal_response_source": SOURCE}


def convert_historical_to_columnstore() -> dict[str, object]:
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select hypertable_size(%s::regclass)::bigint as bytes", (SHADOW,))
        before = int(cursor.fetchone()["bytes"])
        cursor.execute(
            "select show_chunks(%s::regclass,older_than=>localtimestamp-interval '30 days')::text as chunk order by 1",
            (SHADOW,),
        )
        chunks = [row["chunk"] for row in cursor.fetchall()]
        connection.rollback()
    converted: list[str] = []
    for chunk in chunks:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute("call convert_to_columnstore(%s::regclass,if_not_columnstore=>true)", (chunk,))
            connection.commit()
        converted.append(chunk)
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select hypertable_size(%s::regclass)::bigint as bytes", (SHADOW,))
        after = int(cursor.fetchone()["bytes"])
        cursor.execute(
            """select count(*)::int as total,count(*) filter(where is_compressed)::int as columnstore
               from timescaledb_information.chunks where hypertable_schema='fact' and hypertable_name='stock_bar_1m_ts_shadow'"""
        )
        status = dict(cursor.fetchone())
        connection.rollback()
    return {
        "hypertable": SHADOW,
        "eligible_chunks": len(chunks),
        "converted_chunks": converted,
        "before_bytes": before,
        "after_bytes": after,
        "compression_percent": round((1 - after / before) * 100, 2) if before else 0,
        "chunk_status": status,
    }


def reconcile(*, apply: bool) -> dict[str, object]:
    if not apply:
        raise RuntimeError("reconcile requires --apply while all external writers are paused")
    results: list[dict[str, object]] = []
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (LOCK_KEY,))
        _ensure_ledger(cursor)
        cursor.execute("lock table fact.stock_bar_1m,fact.stock_bar_1m_ts_shadow in access exclusive mode")
        cursor.execute("select min(bar_time) as first,max(bar_time) as last from fact.stock_bar_1m")
        bounds = cursor.fetchone()
        if bounds is None or bounds["first"] is None:
            raise RuntimeError("source is empty")
        for month in _month_starts(bounds["first"], bounds["last"]):
            following = _month_after(month)
            source_evidence = _evidence(cursor, SOURCE, month, following)
            target_evidence = _evidence(cursor, SHADOW, month, following)
            repaired = source_evidence != target_evidence
            if repaired:
                cursor.execute(
                    """delete from fact.stock_bar_1m_ts_shadow t
                        where t.bar_time >= %s::timestamp and t.bar_time < %s::timestamp
                          and not exists (
                              select 1 from fact.stock_bar_1m s
                               where (s.market,s.code,s.bar_time)=(t.market,t.code,t.bar_time)
                          )""",
                    (month, following),
                )
                cursor.execute(
                    """insert into fact.stock_bar_1m_ts_shadow(market,code,bar_time,open,high,low,close,volume,amount,loaded_at)
                       select market,code,bar_time,open,high,low,close,volume,amount,loaded_at
                         from fact.stock_bar_1m
                        where bar_time >= %s::timestamp and bar_time < %s::timestamp
                       on conflict(market,code,bar_time) do update set open=excluded.open,high=excluded.high,low=excluded.low,
                         close=excluded.close,volume=excluded.volume,amount=excluded.amount,loaded_at=excluded.loaded_at""",
                    (month, following),
                )
                target_evidence = _evidence(cursor, SHADOW, month, following)
                if source_evidence != target_evidence:
                    raise RuntimeError(f"reconcile evidence mismatch: {month}")
            cursor.execute(
                """update audit.timescale_shadow_migration
                      set status='verified',finished_at_utc=clock_timestamp(),source_evidence=%s::jsonb,
                          target_evidence=%s::jsonb,error=null
                    where source_table=%s and target_table=%s and month_start=%s""",
                (
                    json.dumps(source_evidence, default=str),
                    json.dumps(target_evidence, default=str),
                    SOURCE,
                    SHADOW,
                    month,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"missing migration ledger row during reconcile: {month}")
            results.append({"month": month, "repaired": repaired, "rows": source_evidence["row_count"]})
        connection.commit()
    return {"months": results, "writers_must_remain_paused_until_cutover": True}


def _cutover_contract(cursor) -> dict[str, object]:
    cursor.execute(
        """select pg_get_userbyid(c.relowner) as owner
             from pg_class c where c.oid=%s::regclass""",
        (SOURCE,),
    )
    source = cursor.fetchone()
    if source is None:
        raise RuntimeError(f"missing source table: {SOURCE}")
    cursor.execute(
        """select grantee,privilege_type,is_grantable
             from information_schema.role_table_grants
            where table_schema='fact' and table_name='stock_bar_1m'
            order by grantee,privilege_type"""
    )
    grants = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """select vn.nspname as dependent_schema,v.relname as dependent_object,v.relkind
             from pg_depend d
             join pg_rewrite r on r.oid=d.objid
             join pg_class v on v.oid=r.ev_class
             join pg_namespace vn on vn.oid=v.relnamespace
            where d.refobjid=%s::regclass
            order by 1,2""",
        (SOURCE,),
    )
    dependent_relations = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """select conname,conrelid::regclass::text as from_table,confrelid::regclass::text as to_table
             from pg_constraint
            where confrelid=%s::regclass
            order by conname""",
        (SOURCE,),
    )
    referencing_constraints = [dict(row) for row in cursor.fetchall()]
    if dependent_relations or referencing_constraints:
        raise RuntimeError(
            "cutover has OID-bound dependents that require an explicit migration: "
            + json.dumps(
                {"relations": dependent_relations, "constraints": referencing_constraints},
                default=str,
                sort_keys=True,
            )
        )
    return {"owner": source["owner"], "grants": grants}


def _restore_contract(cursor, contract: dict[str, object]) -> int:
    owner = str(contract["owner"])
    cursor.execute(sql.SQL("alter table {} owner to {}").format(sql.Identifier("fact", "stock_bar_1m"), sql.Identifier(owner)))
    grouped: dict[tuple[str, bool], list[str]] = {}
    allowed_privileges = {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
    for grant in contract["grants"]:
        grantee = str(grant["grantee"])
        if grantee == owner:
            continue
        privilege = str(grant["privilege_type"]).upper()
        if privilege not in allowed_privileges:
            raise RuntimeError(f"unsupported table privilege: {privilege}")
        grouped.setdefault((grantee, grant["is_grantable"] == "YES"), []).append(privilege)
    restored = 0
    for (grantee, grantable), privileges in grouped.items():
        role = sql.SQL("PUBLIC") if grantee == "PUBLIC" else sql.Identifier(grantee)
        statement = sql.SQL("grant {} on {} to {}").format(
            sql.SQL(",").join(sql.SQL(item) for item in sorted(privileges)),
            sql.Identifier("fact", "stock_bar_1m"),
            role,
        )
        if grantable:
            statement += sql.SQL(" with grant option")
        cursor.execute(statement)
        restored += len(privileges)
    return restored


def cutover(*, apply: bool) -> dict[str, object]:
    if not apply:
        raise RuntimeError("cutover requires --apply after external writers are paused")
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (LOCK_KEY,))
        _ensure_ledger(cursor)
        contract = _cutover_contract(cursor)
        cursor.execute("select min(bar_time) as first,max(bar_time) as last from fact.stock_bar_1m")
        bounds = cursor.fetchone()
        expected_months = len(list(_month_starts(bounds["first"], bounds["last"])))
        cursor.execute(
            """select count(*)::int as total,count(*) filter(where status='verified')::int as verified
               from audit.timescale_shadow_migration where source_table=%s and target_table=%s""",
            (SOURCE, SHADOW),
        )
        ledger = cursor.fetchone()
        if int(ledger["total"]) != expected_months or int(ledger["verified"]) != expected_months:
            raise RuntimeError(f"migration ledger is incomplete: {dict(ledger)}/{expected_months}")
        cursor.execute("select count(*) as n from audit.timescale_shadow_migration where source_table=%s and target_table=%s and status<>'verified'", (SOURCE, SHADOW))
        if int(cursor.fetchone()["n"]) != 0:
            raise RuntimeError("migration ledger has non-verified months")
        cursor.execute("lock table fact.stock_bar_1m,fact.stock_bar_1m_ts_shadow in access exclusive mode")
        cursor.execute(f"select count(*)::bigint as n from {FORWARD_JOURNAL}")
        if int(cursor.fetchone()["n"]) != 0:
            raise RuntimeError("forward journal backlog must be zero before cutover")
        cursor.execute("select count(*) as n from fact.stock_bar_1m")
        source_rows = int(cursor.fetchone()["n"])
        cursor.execute("select count(*) as n from fact.stock_bar_1m_ts_shadow")
        target_rows = int(cursor.fetchone()["n"])
        if source_rows != target_rows:
            raise RuntimeError(f"cutover row mismatch: {source_rows}/{target_rows}")
        for month in _month_starts(bounds["first"], bounds["last"]):
            following = _month_after(month)
            if _evidence(cursor, SOURCE, month, following) != _evidence(cursor, SHADOW, month, following):
                raise RuntimeError(f"cutover monthly evidence mismatch: {month}; run reconcile while writers are paused")
        _drop_journal_objects(cursor, "forward", require_empty=True)
        cursor.execute("alter table fact.stock_bar_1m rename constraint stock_bar_1m_pkey to stock_bar_1m_legacy_pkey")
        cursor.execute("alter index fact.stock_bar_1m_code_time_idx rename to stock_bar_1m_legacy_code_time_idx")
        cursor.execute("alter index fact.stock_bar_1m_time_idx rename to stock_bar_1m_legacy_time_idx")
        cursor.execute("alter table fact.stock_bar_1m rename to stock_bar_1m_legacy")
        cursor.execute("alter table fact.stock_bar_1m_ts_shadow rename to stock_bar_1m")
        cursor.execute("alter table fact.stock_bar_1m rename constraint stock_bar_1m_ts_shadow_pkey to stock_bar_1m_pkey")
        cursor.execute("alter index fact.stock_bar_1m_ts_shadow_code_time_idx rename to stock_bar_1m_code_time_idx")
        cursor.execute("alter index fact.stock_bar_1m_ts_shadow_time_idx rename to stock_bar_1m_time_idx")
        restored_grants = _restore_contract(cursor, contract)
        _drop_journal_objects(cursor, "reverse", require_empty=True)
        for statement in _journal_sql("reverse"):
            cursor.execute(statement)
        cursor.execute(f"alter table {REVERSE_JOURNAL} owner to datalake")
        cursor.execute(
            """insert into audit.timescale_shadow_cutover(source_table,target_table,legacy_table,cutover_at_utc)
               values(%s,%s,%s,clock_timestamp()) on conflict(source_table) do update set target_table=excluded.target_table,
               legacy_table=excluded.legacy_table,cutover_at_utc=excluded.cutover_at_utc,
               reverse_mirror_removed_at_utc=null,accelerated_acceptance_sha256=null""",
            (SOURCE, SHADOW, LEGACY),
        )
        connection.commit()
    with _connect(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("analyze fact.stock_bar_1m")
    return {
        "source_rows": source_rows,
        "target_rows": target_rows,
        "reverse_journal": "installed",
        "legacy": LEGACY,
        "owner": contract["owner"],
        "explicit_grants_restored": restored_grants,
    }


def rollback_cutover(*, apply: bool) -> dict[str, object]:
    if not apply:
        raise RuntimeError("rollback requires --apply while all writers are externally paused")
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select to_regclass(%s) as failed", (FAILED,))
        if cursor.fetchone()["failed"] is not None:
            raise RuntimeError(f"rollback target already exists: {FAILED}")
        cursor.execute("lock table fact.stock_bar_1m,fact.stock_bar_1m_legacy in access exclusive mode")
        _drop_journal_objects(cursor, "reverse", require_empty=True)
        cursor.execute("alter table fact.stock_bar_1m rename constraint stock_bar_1m_pkey to stock_bar_1m_ts_shadow_failed_pkey")
        cursor.execute("alter index fact.stock_bar_1m_code_time_idx rename to stock_bar_1m_ts_shadow_failed_code_time_idx")
        cursor.execute("alter index fact.stock_bar_1m_time_idx rename to stock_bar_1m_ts_shadow_failed_time_idx")
        cursor.execute("alter table fact.stock_bar_1m rename to stock_bar_1m_ts_shadow_failed")
        cursor.execute("alter table fact.stock_bar_1m_legacy rename to stock_bar_1m")
        cursor.execute("alter table fact.stock_bar_1m rename constraint stock_bar_1m_legacy_pkey to stock_bar_1m_pkey")
        cursor.execute("alter index fact.stock_bar_1m_legacy_code_time_idx rename to stock_bar_1m_code_time_idx")
        cursor.execute("alter index fact.stock_bar_1m_legacy_time_idx rename to stock_bar_1m_time_idx")
        connection.commit()
    with _connect(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("analyze fact.stock_bar_1m")
    return {"canonical": SOURCE, "failed_hypertable": FAILED, "legacy_restored": True}


def prepare_retry_after_rollback(*, apply: bool) -> dict[str, object]:
    if not apply:
        raise RuntimeError("retry preparation requires --apply while all writers are externally paused")
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (LOCK_KEY,))
        cursor.execute("select to_regclass(%s) as shadow,to_regclass(%s) as failed", (SHADOW, FAILED))
        relations = cursor.fetchone()
        if relations["shadow"] is not None or relations["failed"] is None:
            raise RuntimeError(f"unexpected rollback retry relations: {dict(relations)}")
        cursor.execute("lock table fact.stock_bar_1m,fact.stock_bar_1m_ts_shadow_failed in access exclusive mode")
        cursor.execute("alter table fact.stock_bar_1m_ts_shadow_failed rename constraint stock_bar_1m_ts_shadow_failed_pkey to stock_bar_1m_ts_shadow_pkey")
        cursor.execute("alter index fact.stock_bar_1m_ts_shadow_failed_code_time_idx rename to stock_bar_1m_ts_shadow_code_time_idx")
        cursor.execute("alter index fact.stock_bar_1m_ts_shadow_failed_time_idx rename to stock_bar_1m_ts_shadow_time_idx")
        cursor.execute("alter table fact.stock_bar_1m_ts_shadow_failed rename to stock_bar_1m_ts_shadow")
        _drop_journal_objects(cursor, "forward", require_empty=True)
        for statement in _journal_sql("forward"):
            cursor.execute(statement)
        cursor.execute(f"alter table {FORWARD_JOURNAL} owner to datalake")
        connection.commit()
    return {"canonical": SOURCE, "shadow": SHADOW, "forward_journal": "installed", "ready_for_revalidation": True}


def remove_reverse_mirror(*, apply: bool, evidence_sha256: str | None) -> dict[str, object]:
    if not apply:
        raise RuntimeError("reverse mirror removal requires --apply")
    if evidence_sha256 is None or re.fullmatch(r"[0-9a-f]{64}", evidence_sha256) is None:
        raise RuntimeError("accelerated acceptance requires a lowercase SHA-256 evidence digest")
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select cutover_at_utc from audit.timescale_shadow_cutover where source_table=%s for update", (SOURCE,))
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("cutover evidence is missing")
        _drop_journal_objects(cursor, "reverse", require_empty=True)
        cursor.execute(
            """update audit.timescale_shadow_cutover
                  set reverse_mirror_removed_at_utc=clock_timestamp(),accelerated_acceptance_sha256=%s
                where source_table=%s""",
            (evidence_sha256, SOURCE),
        )
        connection.commit()
    return {"reverse_journal": "removed", "legacy": "retained_read_only", "accelerated_acceptance_sha256": evidence_sha256}


def _write_report(path: Path | None, payload: object) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, path)
    print(encoded, end="")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    subparsers = parser.add_subparsers(dest="action", required=True)
    create = subparsers.add_parser("create-shadow")
    create.add_argument("--chunk", choices=sorted(ALLOWED_CHUNKS), required=True)
    create.add_argument("--order", choices=sorted(ALLOWED_ORDERS), required=True)
    suspend_indexes = subparsers.add_parser("suspend-secondary-indexes")
    suspend_indexes.add_argument("--apply", action="store_true")
    restore_indexes = subparsers.add_parser("restore-secondary-indexes")
    restore_indexes.add_argument("--apply", action="store_true")
    subparsers.add_parser("install-forward-mirror")
    subparsers.add_parser("remove-forward-mirror")
    install_journal = subparsers.add_parser("install-key-journal")
    install_journal.add_argument("--direction", choices=("forward", "reverse"), required=True)
    remove_journal = subparsers.add_parser("remove-key-journal")
    remove_journal.add_argument("--direction", choices=("forward", "reverse"), required=True)
    journal_status_parser = subparsers.add_parser("journal-status")
    journal_status_parser.add_argument("--direction", choices=("forward", "reverse"), required=True)
    reconcile_journal_parser = subparsers.add_parser("reconcile-journal")
    reconcile_journal_parser.add_argument("--direction", choices=("forward", "reverse"), required=True)
    reconcile_journal_parser.add_argument("--batch-size", type=int, default=100_000)
    reconcile_journal_parser.add_argument("--max-batches", type=int)
    probe_journal_parser = subparsers.add_parser("probe-key-journal")
    probe_journal_parser.add_argument("--direction", choices=("forward", "reverse"), default="forward")
    benchmark = subparsers.add_parser("benchmark-mirror")
    benchmark.add_argument("--iterations", type=int, default=20)
    benchmark.add_argument("--rows", type=int, nargs="+", default=(1_000, 10_000))
    benchmark.add_argument("--mode", choices=("baseline", "mirrored", "journaled"), required=True)
    subparsers.add_parser("probe-reverse-transition")
    backfill_parser = subparsers.add_parser("backfill")
    backfill_parser.add_argument("--max-months", type=int)
    subparsers.add_parser("inventory")
    subparsers.add_parser("double-read")
    subparsers.add_parser("convert-historical")
    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_parser.add_argument("--apply", action="store_true")
    cutover_parser = subparsers.add_parser("cutover")
    cutover_parser.add_argument("--apply", action="store_true")
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--apply", action="store_true")
    retry_parser = subparsers.add_parser("prepare-retry-after-rollback")
    retry_parser.add_argument("--apply", action="store_true")
    remove = subparsers.add_parser("remove-reverse-mirror")
    remove.add_argument("--apply", action="store_true")
    remove.add_argument("--evidence-sha256")
    args = parser.parse_args()
    if args.action == "create-shadow":
        result = create_shadow(args.chunk, args.order)
    elif args.action == "suspend-secondary-indexes":
        result = set_secondary_indexes(present=False, apply=args.apply)
    elif args.action == "restore-secondary-indexes":
        result = set_secondary_indexes(present=True, apply=args.apply)
    elif args.action == "install-forward-mirror":
        result = install_forward_mirror()
    elif args.action == "remove-forward-mirror":
        result = remove_forward_mirror()
    elif args.action == "install-key-journal":
        result = install_key_journal(args.direction)
    elif args.action == "remove-key-journal":
        result = remove_key_journal(args.direction)
    elif args.action == "journal-status":
        result = journal_status(args.direction)
    elif args.action == "reconcile-journal":
        result = reconcile_journal(args.direction, batch_size=args.batch_size, max_batches=args.max_batches)
    elif args.action == "probe-key-journal":
        result = probe_key_journal(args.direction)
    elif args.action == "benchmark-mirror":
        result = benchmark_mirror(args.iterations, tuple(args.rows), args.mode)
    elif args.action == "probe-reverse-transition":
        result = probe_reverse_transition()
    elif args.action == "backfill":
        result = backfill(max_months=args.max_months)
    elif args.action == "inventory":
        result = inventory()
    elif args.action == "double-read":
        result = double_read()
    elif args.action == "convert-historical":
        result = convert_historical_to_columnstore()
    elif args.action == "reconcile":
        result = reconcile(apply=args.apply)
    elif args.action == "cutover":
        result = cutover(apply=args.apply)
    elif args.action == "rollback":
        result = rollback_cutover(apply=args.apply)
    elif args.action == "prepare-retry-after-rollback":
        result = prepare_retry_after_rollback(apply=args.apply)
    else:
        result = remove_reverse_mirror(apply=args.apply, evidence_sha256=args.evidence_sha256)
    _write_report(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
