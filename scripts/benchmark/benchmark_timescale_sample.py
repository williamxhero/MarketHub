from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


SOURCE_TABLE = "fact.stock_bar_1m"
SAMPLE_TABLE = "fact.stock_bar_1m_ts_sample"
MONTHS = (("2022-01-01", "2022-02-01"), ("2024-01-01", "2024-02-01"), ("2026-04-01", "2026-05-01"))


@dataclass(frozen=True)
class Profile:
    name: str
    interval: str
    order: str


PROFILES = {
    profile.name: profile
    for profile in (
        Profile("7d-asc", "7 days", "ASC"),
        Profile("7d-desc", "7 days", "DESC"),
        Profile("14d-asc", "14 days", "ASC"),
        Profile("14d-desc", "14 days", "DESC"),
        Profile("1month-asc", "1 month", "ASC"),
        Profile("1month-desc", "1 month", "DESC"),
    )
}


def _connect(*, autocommit: bool = False) -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        connect_timeout=10,
        application_name="markethub-timescale-sample-benchmark",
        row_factory=dict_row,
        autocommit=autocommit,
    )


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(fraction * len(ordered) + 0.999999) - 1))
    return ordered[index]


def _json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _create_sample(profile: Profile) -> None:
    free_bytes = shutil.disk_usage("/data").free
    if free_bytes < 100 * 1024**3:
        raise RuntimeError(f"refusing sample build with less than 100 GiB free: {free_bytes}")
    with _connect(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("select pg_advisory_lock(hashtext('markethub-timescale-sample'))")
        cursor.execute("drop table if exists fact.stock_bar_1m_ts_sample")
        cursor.execute(
            """
            create table fact.stock_bar_1m_ts_sample (
                like fact.stock_bar_1m
                including defaults including generated including identity
                including storage including comments
            )
            """
        )
        cursor.execute(
            "select create_hypertable(%s::regclass, by_range('bar_time', %s::interval), create_default_indexes=>false)",
            (SAMPLE_TABLE, profile.interval),
        )
        cursor.execute("alter table fact.stock_bar_1m_ts_sample add constraint stock_bar_1m_ts_sample_pkey primary key (market,code,bar_time)")
        cursor.execute("create index stock_bar_1m_ts_sample_code_time_idx on fact.stock_bar_1m_ts_sample(code,bar_time)")
        cursor.execute("create index stock_bar_1m_ts_sample_time_idx on fact.stock_bar_1m_ts_sample(bar_time desc)")
        cursor.execute(
            sql.SQL(
                "alter table fact.stock_bar_1m_ts_sample set (timescaledb.enable_columnstore=true,timescaledb.segmentby='market,code',timescaledb.orderby={})"
            ).format(sql.Literal(f"bar_time {profile.order}"))
        )


def _load_months() -> dict[str, Any]:
    started = time.perf_counter()
    loaded: list[dict[str, Any]] = []
    with _connect() as connection, connection.cursor() as cursor:
        for start, end in MONTHS:
            month_started = time.perf_counter()
            cursor.execute(
                "select count(*) as n from fact.stock_bar_1m where bar_time >= %s::timestamp and bar_time < %s::timestamp",
                (start, end),
            )
            source_rows = int(cursor.fetchone()["n"])
            cursor.execute(
                "select count(*) as n from fact.stock_bar_1m_ts_sample where bar_time >= %s::timestamp and bar_time < %s::timestamp",
                (start, end),
            )
            target_rows = int(cursor.fetchone()["n"])
            if target_rows == source_rows:
                loaded.append({"start": start, "end": end, "elapsed_seconds": round(time.perf_counter() - month_started, 3), "affected": 0, "status": "already_complete", "row_count": source_rows})
                continue
            if target_rows != 0:
                raise RuntimeError(f"partial month requires explicit sample rebuild: {start} source={source_rows} target={target_rows}")
            cursor.execute(
                """
                insert into fact.stock_bar_1m_ts_sample
                    (market,code,bar_time,open,high,low,close,volume,amount,loaded_at)
                select market,code,bar_time,open,high,low,close,volume,amount,loaded_at
                from fact.stock_bar_1m
                where bar_time >= %s::timestamp and bar_time < %s::timestamp
                on conflict (market,code,bar_time) do update set
                    open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                    volume=excluded.volume,amount=excluded.amount,loaded_at=excluded.loaded_at
                """,
                (start, end),
            )
            connection.commit()
            loaded.append({"start": start, "end": end, "elapsed_seconds": round(time.perf_counter() - month_started, 3), "affected": cursor.rowcount, "status": "loaded", "row_count": source_rows})
    return {"months": loaded, "elapsed_seconds": round(time.perf_counter() - started, 3)}


def _convert_historical_chunks() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    with _connect(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("select show_chunks(%s::regclass)::text as chunk order by 1", (SAMPLE_TABLE,))
        chunks = [row["chunk"] for row in cursor.fetchall()]
        for chunk in chunks:
            started = time.perf_counter()
            cursor.execute("call convert_to_columnstore(%s::regclass, if_not_columnstore=>true)", (chunk,))
            results.append({"chunk": chunk, "elapsed_seconds": round(time.perf_counter() - started, 3)})
    return {"chunk_count": len(results), "chunks": results, "elapsed_seconds": round(sum(item["elapsed_seconds"] for item in results), 3)}


def _evidence(table: str) -> list[dict[str, Any]]:
    if table not in {SOURCE_TABLE, SAMPLE_TABLE}:
        raise ValueError(table)
    query = sql.SQL(
        """
        select %s::date as month_start,
               count(*) as row_count,
               count(*) as primary_key_count,
               min(bar_time) as min_time,max(bar_time) as max_time,
               count(*) filter (where amount is null) as amount_nulls,
               sum(hashtextextended(concat_ws('|',market,code,bar_time::text,open::text,high::text,low::text,close::text,volume::text,coalesce(amount::text,''),loaded_at::text),0)::numeric) as stable_hash_sum
        from {}
        where bar_time >= %s::timestamp and bar_time < %s::timestamp
        """
    ).format(sql.Identifier(*table.split(".")))
    rows: list[dict[str, Any]] = []
    with _connect() as connection, connection.cursor() as cursor:
        for start, end in MONTHS:
            cursor.execute(query, (start, start, end))
            rows.append(dict(cursor.fetchone()))
        connection.rollback()
    return rows


def _benchmark(iterations: int) -> dict[str, Any]:
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("select code from fact.stock_bar_1m_ts_sample group by code order by count(*) desc,code limit 200")
        codes = [row["code"] for row in cursor.fetchall()]
        if len(codes) != 200:
            raise RuntimeError(f"expected 200 codes, got {len(codes)}")
        scenarios = {
            "point": ("select * from fact.stock_bar_1m_ts_sample where code=%s and bar_time>='2024-01-15' and bar_time<'2024-01-16' order by market,code,bar_time", (codes[0],)),
            "single_long": ("select count(*) as n,sum(close::numeric) as close_sum,sum(volume::numeric) as volume_sum from fact.stock_bar_1m_ts_sample where code=%s", (codes[0],)),
            "codes_200": ("select count(*) as n,sum(close::numeric) as close_sum,sum(volume::numeric) as volume_sum from fact.stock_bar_1m_ts_sample where code=any(%s) and bar_time>='2024-01-01' and bar_time<'2024-02-01'", (codes,)),
            "market_range": ("select count(*) as n,sum(close::numeric) as close_sum,sum(volume::numeric) as volume_sum from fact.stock_bar_1m_ts_sample where bar_time>='2026-04-15' and bar_time<'2026-04-16'", ()),
        }
        output: dict[str, Any] = {}
        for name, (query, params) in scenarios.items():
            timings: list[float] = []
            hashes: set[str] = set()
            for _ in range(iterations):
                started = time.perf_counter()
                cursor.execute(query, params)
                rows = cursor.fetchall()
                timings.append((time.perf_counter() - started) * 1000)
                hashes.add(_json_hash(rows))
            if len(hashes) != 1:
                raise AssertionError(f"result drifted: {name}")
            cursor.execute("explain (format json) " + query, params)
            output[name] = {
                "p50_ms": round(statistics.median(timings), 3),
                "p95_ms": round(_percentile(timings, 0.95), 3),
                "max_ms": round(max(timings), 3),
                "result_sha256": next(iter(hashes)),
                "plan": cursor.fetchone()["QUERY PLAN"][0],
            }
        probe_rows = 1_000
        timings = []
        for _ in range(iterations):
            started = time.perf_counter()
            cursor.execute(
                """
                insert into fact.stock_bar_1m_ts_sample
                    (market,code,bar_time,open,high,low,close,volume,amount,loaded_at)
                select market,code,bar_time,open,high,low,close,volume,amount,loaded_at
                from fact.stock_bar_1m_ts_sample order by market,code,bar_time limit %s
                on conflict (market,code,bar_time) do update set close=excluded.close
                """,
                (probe_rows,),
            )
            connection.rollback()
            timings.append((time.perf_counter() - started) * 1000)
        output["upsert_1000_rollback"] = {
            "p50_ms": round(statistics.median(timings), 3),
            "p95_ms": round(_percentile(timings, 0.95), 3),
            "max_ms": round(max(timings), 3),
        }
        return output


def _sizes() -> dict[str, Any]:
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            select table_bytes,index_bytes,toast_bytes,total_bytes,
                   (select count(*) from timescaledb_information.chunks where hypertable_schema='fact' and hypertable_name='stock_bar_1m_ts_sample') as chunks
            from hypertable_detailed_size(%s::regclass)
            """,
            (SAMPLE_TABLE,),
        )
        return dict(cursor.fetchone())


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and benchmark one Timescale stock_bar_1m sample profile")
    parser.add_argument("--profile", choices=tuple(PROFILES), required=True)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    profile = PROFILES[args.profile]
    started_at = datetime.now(timezone.utc)
    if args.resume:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute("select count(*) as n from timescaledb_information.hypertables where hypertable_schema='fact' and hypertable_name='stock_bar_1m_ts_sample'")
            if int(cursor.fetchone()["n"]) != 1:
                raise RuntimeError("--resume requires the existing sample hypertable")
            cursor.execute("select time_interval::text as interval from timescaledb_information.dimensions where hypertable_schema='fact' and hypertable_name='stock_bar_1m_ts_sample' and column_name='bar_time'")
            actual_interval = str(cursor.fetchone()["interval"])
            cursor.execute("select segmentby,orderby from timescaledb_information.hypertable_columnstore_settings where hypertable=%s::regclass", (SAMPLE_TABLE,))
            settings = cursor.fetchone()
            expected_order = "bar_time DESC" if profile.order == "DESC" else "bar_time"
            if actual_interval != profile.interval or settings is None or settings["segmentby"] != "market,code" or settings["orderby"] != expected_order:
                raise RuntimeError(f"sample profile mismatch: interval={actual_interval} settings={settings}")
    else:
        _create_sample(profile)
    load = _load_months()
    source_evidence = _evidence(SOURCE_TABLE)
    target_before_columnstore = _evidence(SAMPLE_TABLE)
    if source_evidence != target_before_columnstore:
        raise AssertionError("sample evidence does not equal source before columnstore")
    size_before_columnstore = _sizes()
    columnstore = _convert_historical_chunks()
    target_after_columnstore = _evidence(SAMPLE_TABLE)
    if source_evidence != target_after_columnstore:
        raise AssertionError("sample evidence changed during columnstore conversion")
    report = {
        "profile": profile.name,
        "chunk_interval": profile.interval,
        "orderby": f"bar_time {profile.order}",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc),
        "months": MONTHS,
        "load": load,
        "columnstore": columnstore,
        "evidence": source_evidence,
        "size_before_columnstore": size_before_columnstore,
        "size_after_columnstore": _sizes(),
        "benchmark": _benchmark(args.iterations),
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, args.output)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
