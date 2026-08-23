from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

from services.dataset_versions import dataset_version_from_state


DATASET_ID = "stock_bar_1m"


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"], port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"], user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"], connect_timeout=10, row_factory=dict_row,
        application_name="markethub-stock-1m-coverage-readmodel",
    )


def _next_month(value: date) -> date:
    return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)


def finalize_stock_1m_daily_coverage_state() -> dict[str, object]:
    """Publish the already-maintained coverage summary for the current generation."""
    with _connect() as connection:
        state = connection.execute(
            "select baseline_id,generation from audit.dataset_version_state where dataset_id=%s",
            (DATASET_ID,),
        ).fetchone()
        if state is None:
            raise RuntimeError("stock_bar_1m dataset state unavailable")
        generation = int(state["generation"])
        dataset_version = dataset_version_from_state(DATASET_ID, str(state["baseline_id"]), generation)
        totals = connection.execute(
            "select count(*)::bigint groups,coalesce(sum(row_count),0)::bigint rows,"
            "min(trade_date) first,max(trade_date) last from readmodel.stock_bar_1m_daily_coverage"
        ).fetchone()
        summary = dict(totals or {})
        digest = hashlib.sha256(json.dumps(summary, sort_keys=True, default=str).encode()).hexdigest()
        connection.execute(
            "insert into readmodel.dataset_build_state(dataset_id,dataset_version,status,source_generation,coverage_ready,complete,row_count,checksum_sha256,built_at_utc,updated_at_utc) "
            "values(%s,%s,'ready',%s,true,true,%s,%s,clock_timestamp(),clock_timestamp()) on conflict(dataset_id,dataset_version) do update set "
            "status='ready',source_generation=excluded.source_generation,coverage_ready=true,complete=true,row_count=excluded.row_count,"
            "checksum_sha256=excluded.checksum_sha256,built_at_utc=clock_timestamp(),error_message='',updated_at_utc=clock_timestamp()",
            (DATASET_ID, dataset_version, generation, int(summary.get("groups", 0)), digest),
        )
    return {
        "dataset_id": DATASET_ID,
        "dataset_version": dataset_version,
        "generation": generation,
        "groups": int(summary.get("groups", 0)),
        "rows": int(summary.get("rows", 0)),
        "first": str(summary.get("first") or ""),
        "last": str(summary.get("last") or ""),
        "checksum_sha256": digest,
        "complete": True,
    }


def build_stock_1m_daily_coverage(start: date | None = None, end: date | None = None) -> dict[str, object]:
    with _connect() as connection:
        state = connection.execute("select baseline_id,generation from audit.dataset_version_state where dataset_id=%s", (DATASET_ID,)).fetchone()
        bounds = connection.execute("select min(bar_time)::date first,max(bar_time)::date last from fact.stock_bar_1m").fetchone()
    if state is None or bounds is None or bounds["first"] is None or bounds["last"] is None:
        raise RuntimeError("stock_bar_1m dataset state or fact bounds unavailable")
    first = max(start or bounds["first"], bounds["first"])
    last = min(end or bounds["last"], bounds["last"])
    if first > last:
        raise ValueError("empty stock 1m coverage range")
    generation = int(state["generation"])
    dataset_version = dataset_version_from_state(DATASET_ID, str(state["baseline_id"]), generation)
    with _connect() as connection:
        connection.execute(
            "insert into readmodel.dataset_build_state(dataset_id,dataset_version,status,source_generation,coverage_ready,complete,row_count,updated_at_utc) "
            "values(%s,%s,'building',%s,false,false,0,clock_timestamp()) on conflict(dataset_id,dataset_version) do update set "
            "status='building',coverage_ready=false,complete=false,error_message='',updated_at_utc=clock_timestamp()",
            (DATASET_ID, dataset_version, generation),
        )
    current = first.replace(day=1)
    month_results: list[dict[str, object]] = []
    try:
        while current <= last:
            next_month = _next_month(current)
            chunk_start = max(first, current)
            chunk_end = min(last + timedelta(days=1), next_month)
            with _connect() as connection:
                connection.execute("set local statement_timeout='0'")
                connection.execute("delete from readmodel.stock_bar_1m_daily_coverage where trade_date >= %s and trade_date < %s", (chunk_start, chunk_end))
                inserted = connection.execute(
                    "insert into readmodel.stock_bar_1m_daily_coverage(market,code,trade_date,row_count,first_bar_time,last_bar_time,updated_at) "
                    "select market,btrim(code),bar_time::date,count(*)::int,min(bar_time),max(bar_time),clock_timestamp() from fact.stock_bar_1m "
                    "where bar_time >= %s::date and bar_time < %s::date group by market,btrim(code),bar_time::date",
                    (chunk_start, chunk_end),
                ).rowcount
            month_results.append({"start": chunk_start.isoformat(), "end_exclusive": chunk_end.isoformat(), "groups": inserted})
            current = next_month
        finalized = finalize_stock_1m_daily_coverage_state()
        return {**finalized, "start": first.isoformat(), "end": last.isoformat(), "months": month_results}
    except BaseException as exc:
        with _connect() as connection:
            connection.execute(
                "update readmodel.dataset_build_state set status='failed',coverage_ready=false,complete=false,error_message=%s,updated_at_utc=clock_timestamp() where dataset_id=%s and dataset_version=%s",
                (str(exc)[:4000], DATASET_ID, dataset_version),
            )
        raise
