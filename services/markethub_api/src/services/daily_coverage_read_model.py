from __future__ import annotations

from datetime import date
import hashlib
import json
import os
from typing import Any

from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row

from quotemux.infra.db.client import query_dataframe
from services.dataset_versions import STOCK_DAILY_DATASET_ID, dataset_version_from_state
from services.request_timing import record_stage_ms


_BUILD_STATE_SQL = """
insert into readmodel.dataset_build_state(
    dataset_id,dataset_version,status,source_generation,coverage_ready,complete,
    row_count,checksum_sha256,built_at_utc,error_message,updated_at_utc
)
values(%s,%s,%s,%s,%s,%s,%s,%s,case when %s then clock_timestamp() else null end,%s,clock_timestamp())
on conflict(dataset_id,dataset_version) do update set
    status=excluded.status,
    source_generation=excluded.source_generation,
    coverage_ready=excluded.coverage_ready,
    complete=excluded.complete,
    row_count=excluded.row_count,
    checksum_sha256=excluded.checksum_sha256,
    built_at_utc=excluded.built_at_utc,
    error_message=excluded.error_message,
    updated_at_utc=clock_timestamp()
"""


_CREATE_STATE_SQL = """
create temporary table query_read_daily_state on commit drop as
with catalog as materialized (
    select distinct on (code) market,code,
           case when market='BJSE' then greatest(listed_date,date '2021-11-15') else listed_date end as listed_date,
           delisted_date
    from ref.stock
    where code<>'000000'
      and ((market='SHSE' and left(code,1)='6')
        or (market='SZSE' and left(code,1) in ('0','3'))
        or (market='BJSE' and left(code,1) in ('4','8','9')))
    order by code,(delisted_date is null) desc,listed_date desc,market
), open_dates as materialized (
    select trade_date from ref.trade_calendar
    where exchange='SHSE' and is_open and trade_date between %s::date and %s::date
), expected as materialized (
    select catalog.market,catalog.code,open_dates.trade_date
    from catalog cross join open_dates
    where (catalog.listed_date is null or catalog.listed_date<=open_dates.trade_date)
      and (catalog.delisted_date is null or open_dates.trade_date<catalog.delisted_date)
      and not exists (
        select 1 from fact.stock_suspension_history suspension
        where suspension.market=catalog.market and suspension.code=catalog.code
          and suspension.status='suspended'
          and suspension.suspend_start_date<=open_dates.trade_date
          and suspension.suspend_end_date>=open_dates.trade_date
      )
      and not exists (
        select 1 from fact.stock_daily_1d suspended_daily
        where suspended_daily.market=catalog.market and suspended_daily.code=catalog.code
          and suspended_daily.trade_date=open_dates.trade_date
          and coalesce(suspended_daily.is_suspended,false)=true
      )
)
select expected.market,expected.code,expected.trade_date,
       count(daily.code)::int as actual_rows,
       count(daily.code) filter(where
           coalesce(daily.is_suspended,false)=false
           and daily.open is not null and daily.high is not null
           and daily.low is not null and daily.close is not null and daily.volume is not null
       )::int as valid_rows
from expected
left join fact.stock_daily_1d daily
  on daily.market=expected.market and daily.code=expected.code and daily.trade_date=expected.trade_date
group by expected.market,expected.code,expected.trade_date
"""


_INSERT_DAY_SQL = """
insert into readmodel.stock_daily_coverage_day(
    dataset_version,trade_date,market,expected_rows,actual_rows,missing_rows,duplicate_rows,complete
)
select %s,trade_date,market,count(*)::int,
       count(*) filter(where actual_rows=1 and valid_rows=1)::int,
       count(*) filter(where actual_rows<>1 or valid_rows<>1)::int,
       coalesce(sum(greatest(actual_rows-1,0)),0)::int,
       bool_and(actual_rows=1 and valid_rows=1)
from query_read_daily_state
group by trade_date,market
order by trade_date,market
"""


_INSERT_GAP_SQL = """
insert into readmodel.stock_daily_coverage_gap(
    dataset_version,trade_date,market,code,reason,expected_rows,actual_rows
)
select %s,trade_date,market,code,
       case when actual_rows=0 then 'missing'
            when actual_rows>1 then 'duplicate'
            else 'invalid_required_fields' end,
       1,actual_rows
from query_read_daily_state
where actual_rows<>1 or valid_rows<>1
order by trade_date,market,code
"""


def _connect(*, autocommit: bool = False) -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        connect_timeout=10,
        row_factory=dict_row,
        autocommit=autocommit,
        application_name="markethub-daily-coverage-readmodel",
    )


def _dataset_state(connection: psycopg.Connection[Any]) -> tuple[str, int, str]:
    row = connection.execute(
        "select baseline_id,generation from audit.dataset_version_state where dataset_id=%s",
        (STOCK_DAILY_DATASET_ID,),
    ).fetchone()
    if row is None:
        raise RuntimeError("stock_daily_1d dataset version state unavailable")
    baseline_id = str(row["baseline_id"])
    generation = int(row["generation"])
    return baseline_id, generation, dataset_version_from_state(STOCK_DAILY_DATASET_ID, baseline_id, generation)


def _set_build_state(
    dataset_version: str,
    generation: int,
    status: str,
    *,
    coverage_ready: bool,
    complete: bool,
    row_count: int = 0,
    checksum: str | None = None,
    error: str = "",
) -> None:
    with _connect(autocommit=True) as connection:
        connection.execute(
            _BUILD_STATE_SQL,
            (
                STOCK_DAILY_DATASET_ID,
                dataset_version,
                status,
                generation,
                coverage_ready,
                complete,
                row_count,
                checksum,
                coverage_ready,
                error[:4_000],
            ),
        )


def build_current_stock_daily_coverage(start: date | None = None, end: date | None = None) -> dict[str, object]:
    with _connect() as probe:
        _, generation, dataset_version = _dataset_state(probe)
        bounds = probe.execute(
            "select max(first) first,max(last) last from ("
            "select market,min(trade_date) first,max(trade_date) last from fact.stock_daily_1d "
            "where market in ('SHSE','SZSE','BJSE') group by market) market_bounds"
        ).fetchone()
    if bounds is None or bounds["first"] is None or bounds["last"] is None:
        raise RuntimeError("stock_daily_1d is empty")
    first = max(start, bounds["first"]) if start else bounds["first"]
    last = min(end, bounds["last"]) if end else bounds["last"]
    if first > last:
        raise RuntimeError(f"empty coverage range: {first}..{last}")
    _set_build_state(dataset_version, generation, "building", coverage_ready=False, complete=False)
    try:
        with _connect() as connection:
            connection.execute("set transaction isolation level repeatable read")
            _, snapshot_generation, snapshot_version = _dataset_state(connection)
            if snapshot_version != dataset_version:
                raise RuntimeError(f"dataset changed before coverage build: {dataset_version} -> {snapshot_version}")
            connection.execute("delete from readmodel.stock_daily_coverage_day where dataset_version=%s", (dataset_version,))
            connection.execute("delete from readmodel.stock_daily_coverage_gap where dataset_version=%s", (dataset_version,))
            connection.execute(_CREATE_STATE_SQL, (first, last))
            connection.execute(_INSERT_DAY_SQL, (dataset_version,))
            connection.execute(_INSERT_GAP_SQL, (dataset_version,))
            day_rows = connection.execute(
                "select trade_date,market,expected_rows,actual_rows,missing_rows,duplicate_rows,complete "
                "from readmodel.stock_daily_coverage_day where dataset_version=%s order by trade_date,market",
                (dataset_version,),
            ).fetchall()
            gap_rows = connection.execute(
                "select trade_date,market,code,reason,expected_rows,actual_rows "
                "from readmodel.stock_daily_coverage_gap where dataset_version=%s order by trade_date,market,code,reason",
                (dataset_version,),
            ).fetchall()
            digest = hashlib.sha256()
            for row in (*day_rows, *gap_rows):
                digest.update(json.dumps(dict(row), sort_keys=True, default=str, separators=(",", ":")).encode())
            complete = not gap_rows and all(bool(row["complete"]) for row in day_rows)
            status = "parquet_pending" if complete else "failed"
            error = "" if complete else f"daily coverage gaps={len(gap_rows)}"
            connection.execute(
                _BUILD_STATE_SQL,
                (
                    STOCK_DAILY_DATASET_ID,
                    dataset_version,
                    status,
                    snapshot_generation,
                    True,
                    complete,
                    len(day_rows) + len(gap_rows),
                    digest.hexdigest(),
                    True,
                    error,
                ),
            )
        with _connect() as verify:
            _, current_generation, current_version = _dataset_state(verify)
        if current_version != dataset_version:
            error = f"dataset changed during coverage build: {dataset_version} -> {current_version}"
            _set_build_state(dataset_version, generation, "failed", coverage_ready=False, complete=False, error=error)
            raise RuntimeError(error)
        return {
            "dataset_id": STOCK_DAILY_DATASET_ID,
            "dataset_version": dataset_version,
            "generation": current_generation,
            "start": first.isoformat(),
            "end": last.isoformat(),
            "day_rows": len(day_rows),
            "gap_rows": len(gap_rows),
            "complete": complete,
            "checksum_sha256": digest.hexdigest(),
            "status": status,
        }
    except BaseException as exc:
        _set_build_state(dataset_version, generation, "failed", coverage_ready=False, complete=False, error=str(exc))
        raise


def ensure_current_stock_daily_coverage() -> dict[str, object]:
    with _connect() as connection:
        _, generation, dataset_version = _dataset_state(connection)
        state = connection.execute(
            "select status,coverage_ready,complete,row_count,checksum_sha256,error_message,built_at_utc "
            "from readmodel.dataset_build_state where dataset_id=%s and dataset_version=%s",
            (STOCK_DAILY_DATASET_ID, dataset_version),
        ).fetchone()
    if state is None or not bool(state["coverage_ready"]):
        return build_current_stock_daily_coverage()
    return {
        "dataset_id": STOCK_DAILY_DATASET_ID,
        "dataset_version": dataset_version,
        "generation": generation,
        "status": str(state["status"]),
        "complete": bool(state["complete"]),
        "row_count": int(state["row_count"]),
        "checksum_sha256": str(state["checksum_sha256"] or ""),
        "error": str(state["error_message"] or ""),
        "built_at_utc": str(state["built_at_utc"] or ""),
        "cached": True,
    }


def mark_stock_daily_publication_ready(dataset_version: str) -> None:
    with _connect(autocommit=True) as connection:
        result = connection.execute(
            "update readmodel.dataset_build_state set status='ready',updated_at_utc=clock_timestamp() "
            "where dataset_id=%s and dataset_version=%s and coverage_ready and complete",
            (STOCK_DAILY_DATASET_ID, dataset_version),
        )
        if result.rowcount != 1:
            raise RuntimeError(f"complete coverage state unavailable for publication: {dataset_version}")


def load_stock_daily_coverage_summary(
    dataset_version: str,
    start_date: str,
    end_date: str,
    *,
    codes: list[str] | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    import time

    started = time.perf_counter()
    state = query_dataframe(
        "select coverage_ready,status,complete,error_message from readmodel.dataset_build_state "
        "where dataset_id=%s and dataset_version=%s",
        (STOCK_DAILY_DATASET_ID, dataset_version),
    )
    if len(state.index) != 1 or not bool(state.iloc[0].get("coverage_ready", False)):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "READ_MODEL_NOT_READY",
                "message": "日线 coverage read model 尚未就绪",
                "details": {"dataset_id": STOCK_DAILY_DATASET_ID, "dataset_version": dataset_version},
            },
        )
    if codes:
        day = None
        row: dict[str, object] = {
            "expected_total": 0,
            "actual_total": 0,
            "missing_total": 0,
            "duplicate_total": 0,
        }
    else:
        day = query_dataframe(
            "select coalesce(sum(expected_rows),0)::bigint expected_total,"
            "coalesce(sum(actual_rows),0)::bigint actual_total,"
            "coalesce(sum(missing_rows),0)::bigint missing_total,"
            "coalesce(sum(duplicate_rows),0)::bigint duplicate_total "
            "from readmodel.stock_daily_coverage_day "
            "where dataset_version=%s and trade_date between %s::date and %s::date",
            (dataset_version, start_date, end_date),
        )
        row = day.iloc[0].to_dict() if len(day.index) == 1 else {}
    gap_params: tuple[object, ...] = (dataset_version, start_date, end_date)
    gap_filter = ""
    if codes:
        gap_filter = " and code=any(%s::text[])"
        gap_params += (codes,)
    gaps = query_dataframe(
        "select market,code,trade_date,reason,expected_rows,actual_rows "
        "from readmodel.stock_daily_coverage_gap "
        "where dataset_version=%s and trade_date between %s::date and %s::date" + gap_filter +
        " order by trade_date,market,code limit 100",
        gap_params,
    )
    record_stage_ms("coverage", (time.perf_counter() - started) * 1_000)
    gap_items = [entry.to_dict() for _, entry in gaps.iterrows()]
    if codes and gap_items:
        row["missing_total"] = sum(1 for item in gap_items if item["reason"] != "duplicate")
        row["duplicate_total"] = sum(max(int(item["actual_rows"]) - 1, 0) for item in gap_items)
    if gap_items or int(row.get("missing_total", 0) or 0) or int(row.get("duplicate_total", 0) or 0):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DATA_INCOMPLETE",
                "message": "日线窗口完整性校验失败，拒绝返回部分数据",
                "details": {
                    "dataset_id": STOCK_DAILY_DATASET_ID,
                    "dataset_version": dataset_version,
                    "expected_rows": int(row.get("expected_total", 0) or 0),
                    "actual_rows": int(row.get("actual_total", 0) or 0),
                    "missing_rows": int(row.get("missing_total", 0) or 0),
                    "duplicate_rows": int(row.get("duplicate_total", 0) or 0),
                    "gap_sample": gap_items[:20],
                    "repair_endpoint": "/api/admin/data-repairs",
                },
            },
        )
    return row, gap_items
