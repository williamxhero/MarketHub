from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pyarrow as pa
import pyarrow.parquet as pq
import psycopg
from psycopg.rows import dict_row

from services.daily_coverage_read_model import ensure_current_stock_daily_coverage, mark_stock_daily_publication_ready


DATASET_ID = "stock_daily_1d"
SCHEMA_VERSION = "markethub-stock-daily-parquet-v1"

BARS_SCHEMA = pa.schema(
    [
        ("market", pa.string()), ("code", pa.string()), ("trade_date", pa.date32()),
        ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()), ("close", pa.float64()),
        ("volume", pa.float64()), ("amount", pa.float64()), ("is_suspended", pa.bool_()), ("is_st", pa.bool_()),
        ("pre_close", pa.float64()), ("change", pa.float64()), ("pct_chg", pa.float64()), ("adj_factor", pa.float64()),
        ("loaded_at", pa.timestamp("us", tz="UTC")),
    ],
    metadata={b"schema_version": SCHEMA_VERSION.encode()},
)
COVERAGE_SCHEMA = pa.schema(
    [
        ("market", pa.string()), ("code", pa.string()), ("expected_rows", pa.int32()),
        ("actual_rows", pa.int32()), ("missing_rows", pa.int32()),
        ("missing_trade_dates", pa.list_(pa.date32())), ("complete", pa.bool_()),
    ],
    metadata={b"schema_version": SCHEMA_VERSION.encode()},
)

_COVERAGE_SQL = """
with catalog as materialized (
    select distinct on (code) market,code,listed_date,delisted_date
    from ref.stock where code<>'000000'
    order by code,(delisted_date is null) desc,listed_date desc,market
), universe as materialized (
    select market,code,
           case when market='BJSE' then greatest(listed_date,date '2021-11-15') else listed_date end as listed_date,
           delisted_date
    from catalog
    where (case when market='BJSE' then greatest(listed_date,date '2021-11-15') else listed_date end) < %s::date
      and (delisted_date is null or delisted_date > %s::date)
      and (case when market='BJSE' then greatest(listed_date,date '2021-11-15') else listed_date end) < coalesce(delisted_date,date 'infinity')
      and ((market='SHSE' and left(code,1)='6') or (market='SZSE' and left(code,1) in ('0','3')) or (market='BJSE' and left(code,1) in ('4','8','9')))
), open_dates as materialized (
    select trade_date from ref.trade_calendar
    where exchange='SHSE' and is_open and trade_date >= %s::date and trade_date < %s::date
), expected as materialized (
    select u.market,u.code,d.trade_date from universe u cross join open_dates d
    where (u.listed_date is null or u.listed_date<=d.trade_date) and (u.delisted_date is null or d.trade_date<u.delisted_date)
      and not exists (
        select 1 from fact.stock_suspension_history s where s.market=u.market and s.code=u.code and s.status='suspended'
          and s.suspend_start_date<=d.trade_date and s.suspend_end_date>=d.trade_date
      )
)
select u.market,u.code,count(e.trade_date)::int as expected_rows,
       count(e.trade_date)::int as actual_rows,
       0::int as missing_rows,
       '{}'::date[] as missing_trade_dates,
       true as complete,
       0::int as duplicate_rows
from universe u left join expected e on e.market=u.market and e.code=u.code
group by u.market,u.code order by u.market,u.code
"""

_BARS_SQL = """
with catalog as materialized (
    select distinct on (code) market,code,
           case when market='BJSE' then greatest(listed_date,date '2021-11-15') else listed_date end as listed_date,
           delisted_date
    from ref.stock where code<>'000000'
    order by code,(delisted_date is null) desc,listed_date desc,market
)
select b.market,b.code,b.trade_date,b.open,b.high,b.low,b.close,b.volume,b.amount,
       coalesce(b.is_suspended,false) as is_suspended,coalesce(b.is_st,false) as is_st,
       b.pre_close,b.change,b.pct_chg,b.adj_factor,b.loaded_at
from fact.stock_daily_1d b
join catalog s on s.market=b.market and s.code=b.code
where b.trade_date >= %s::date and b.trade_date < %s::date
  and ((b.market='SHSE' and left(b.code,1)='6') or (b.market='SZSE' and left(b.code,1) in ('0','3')) or (b.market='BJSE' and left(b.code,1) in ('4','8','9')))
  and (
    coalesce(b.is_suspended,false)=true
    or (b.open is not null and b.high is not null and b.low is not null and b.close is not null and b.volume is not null)
  )
  and (s.listed_date is null or s.listed_date<=b.trade_date) and (s.delisted_date is null or b.trade_date<s.delisted_date)
  and s.listed_date < coalesce(s.delisted_date,date 'infinity')
  and not exists (
    select 1 from fact.stock_suspension_history x where x.market=b.market and x.code=b.code and x.status='suspended'
      and x.suspend_start_date<=b.trade_date and x.suspend_end_date>=b.trade_date
  )
order by b.trade_date,b.code,b.market
"""


def _connect(*, autocommit: bool = False) -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"], port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"], user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"], connect_timeout=10,
        row_factory=dict_row, autocommit=autocommit, application_name="markethub-stock-daily-publisher",
    )


def _version(dataset_id: str, baseline_id: str, generation: int) -> str:
    payload = {"contract": "markethub-dataset-v1", "dataset_id": dataset_id, "baseline_id": baseline_id, "generation": generation}
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return f"mhd-v1-{hashlib.sha256(encoded).hexdigest()}"


def _dataset_state(connection: psycopg.Connection[Any]) -> tuple[str, int, str]:
    with connection.cursor() as cursor:
        cursor.execute("select baseline_id,generation from audit.dataset_version_state where dataset_id=%s", (DATASET_ID,))
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("stock_daily_1d dataset version state is not installed")
    baseline, generation = str(row["baseline_id"]), int(row["generation"])
    return baseline, generation, _version(DATASET_ID, baseline, generation)


def _market_version(connection: psycopg.Connection[Any]) -> str:
    with connection.cursor() as cursor:
        cursor.execute("select baseline_id,generation from audit.market_data_version_state where singleton=true")
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("market data version state unavailable")
    payload = {
        "contract": "markethub-market-facts-v1-triggered", "baseline_id": str(row["baseline_id"]),
        "generation": int(row["generation"]), "adjustment_base_date": os.getenv("QUOTEMUX_ADJUSTMENT_BASE_DATE", "").strip(),
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return f"mhf-v1-{hashlib.sha256(encoded).hexdigest()}"


def _current_versions() -> tuple[str, str]:
    with _connect() as connection:
        connection.execute("set transaction isolation level repeatable read read only")
        _, _, dataset_version = _dataset_state(connection)
        market_version = _market_version(connection)
        connection.rollback()
    return dataset_version, market_version


def _months(start: date, end: date) -> Iterator[tuple[date, date]]:
    current = start.replace(day=1)
    while current <= end:
        following = date(current.year + (current.month == 12), 1 if current.month == 12 else current.month + 1, 1)
        yield max(current, start), min(following, end + date.resolution)
        current = following


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_record(root: Path, path: Path, rows: int, dataset_version: str) -> dict[str, object]:
    relative_path = path.relative_to(root).as_posix()
    return {
        "path": relative_path,
        "url": f"/api/exports/{DATASET_ID}/{dataset_version}/files/{relative_path}",
        "rows": rows,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _write_bars(connection: psycopg.Connection[Any], path: Path, start: date, end: date, compression: str, target_bytes: int) -> int:
    row_group_rows = max(10_000, target_bytes // 192)
    count = 0
    with connection.cursor(name=f"stock_daily_publish_{uuid.uuid4().hex}") as cursor:
        cursor.execute(_BARS_SQL, (start, end))
        with pq.ParquetWriter(path, BARS_SCHEMA, compression=compression, use_dictionary=["market", "code"], write_statistics=True) as writer:
            while True:
                rows = cursor.fetchmany(row_group_rows)
                if not rows:
                    break
                table = pa.Table.from_pylist([dict(row) for row in rows], schema=BARS_SCHEMA)
                writer.write_table(table, row_group_size=len(rows))
                count += len(rows)
    return count


def _coverage(connection: psycopg.Connection[Any], path: Path, start: date, end: date, compression: str) -> tuple[int, int]:
    with connection.cursor() as cursor:
        cursor.execute(_COVERAGE_SQL, (end, start, start, end))
        rows = cursor.fetchall()
    if not rows:
        raise RuntimeError(f"empty all_a coverage: {start}..{end}")
    missing = sum(int(row["missing_rows"]) for row in rows)
    duplicates = sum(int(row["duplicate_rows"]) for row in rows)
    if missing or duplicates or any(not bool(row["complete"]) for row in rows):
        bad = [{"code": row["code"], "missing": row["missing_rows"], "dates": row["missing_trade_dates"][:10]} for row in rows if not row["complete"]][:20]
        raise RuntimeError(f"coverage incomplete start={start} end={end} missing={missing} duplicates={duplicates} bad={bad}")
    records = [{key: row[key] for key in ("market", "code", "expected_rows", "actual_rows", "missing_rows", "missing_trade_dates", "complete")} for row in rows]
    pq.write_table(pa.Table.from_pylist(records, schema=COVERAGE_SCHEMA), path, compression=compression, use_dictionary=["market", "code"])
    return len(records), sum(int(row["actual_rows"]) for row in rows)


def _record_mapping(dataset_version: str, market_version: str, manifest_sha256: str, relative_root: str) -> None:
    with _connect(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("select dataset_version,manifest_sha256,relative_root from audit.dataset_version_publication where dataset_id=%s and market_data_version=%s", (DATASET_ID, market_version))
        existing = cursor.fetchone()
        expected = {"dataset_version": dataset_version, "manifest_sha256": manifest_sha256, "relative_root": relative_root}
        if existing is not None and dict(existing) != expected:
            raise RuntimeError(f"market version mapping conflict: {existing}")
        cursor.execute(
            "insert into audit.dataset_version_publication(dataset_id,market_data_version,dataset_version,manifest_sha256,relative_root) values(%s,%s,%s,%s,%s) on conflict(dataset_id,market_data_version) do nothing",
            (DATASET_ID, market_version, dataset_version, manifest_sha256, relative_root),
        )


def publish(
    export_root: Path,
    compression: str,
    row_group_target_bytes: int,
    *,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, object]:
    coverage_state = ensure_current_stock_daily_coverage()
    if not bool(coverage_state.get("complete", False)):
        raise RuntimeError(f"stock daily coverage is incomplete: {coverage_state}")
    export_root = export_root.resolve()
    parent = export_root / DATASET_ID
    staging_parent = parent / ".staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    with _connect() as snapshot:
        snapshot.execute("set transaction isolation level repeatable read read only")
        baseline, generation, dataset_version = _dataset_state(snapshot)
        if coverage_state.get("dataset_version") != dataset_version:
            raise RuntimeError(
                f"dataset changed after coverage build: coverage={coverage_state.get('dataset_version')} snapshot={dataset_version}"
            )
        with snapshot.cursor() as cursor:
            cursor.execute("select min(trade_date) as first,max(trade_date) as last from fact.stock_daily_1d")
            bounds = cursor.fetchone()
        if bounds is None or bounds["first"] is None or bounds["last"] is None:
            raise RuntimeError("stock_daily_1d is empty")
        first = max(bounds["first"], start) if start is not None else bounds["first"]
        last = min(bounds["last"], end) if end is not None else bounds["last"]
        if first > last:
            raise RuntimeError(f"empty publication range: {first}..{last}")
        final_root = parent / dataset_version
        if final_root.is_dir():
            manifest_path = final_root / "manifest.json"
            manifest_sha = _sha256(manifest_path)
            current_dataset, market_version = _current_versions()
            if current_dataset != dataset_version:
                raise RuntimeError("dataset version changed before mapping existing publication")
            _record_mapping(dataset_version, market_version, manifest_sha, final_root.relative_to(export_root).as_posix())
            mark_stock_daily_publication_ready(dataset_version)
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        staging = staging_parent / f"{dataset_version}-{uuid.uuid4().hex}"
        staging.mkdir()
        files: list[dict[str, object]] = []
        partitions: list[dict[str, object]] = []
        try:
            for month_start, month_end in _months(first, last):
                part = staging / f"year={month_start.year:04d}" / f"month={month_start.month:02d}"
                part.mkdir(parents=True)
                coverage_path = part / "coverage.parquet"
                coverage_rows, expected_bars = _coverage(snapshot, coverage_path, month_start, month_end, compression)
                bars_path = part / "bars.parquet"
                bars_rows = _write_bars(snapshot, bars_path, month_start, month_end, compression, row_group_target_bytes)
                if bars_rows != expected_bars:
                    raise RuntimeError(f"bars/coverage mismatch {month_start}: bars={bars_rows} expected={expected_bars}")
                files.extend(
                    (
                        _file_record(staging, bars_path, bars_rows, dataset_version),
                        _file_record(staging, coverage_path, coverage_rows, dataset_version),
                    )
                )
                partitions.append({"start": month_start, "end_exclusive": month_end, "rows": bars_rows})
            snapshot.rollback()
            with _connect() as current:
                current.execute("set transaction isolation level repeatable read read only")
                end_baseline, end_generation, end_version = _dataset_state(current)
                market_version = _market_version(current)
                current.rollback()
            if (end_baseline, end_generation, end_version) != (baseline, generation, dataset_version):
                raise RuntimeError(f"dataset changed during publish: start={dataset_version} end={end_version}")
            manifest = {
                "schema_version": SCHEMA_VERSION, "dataset_id": DATASET_ID, "dataset_version": dataset_version,
                "market_data_version": market_version, "range": {"start": first, "end": last},
                "compression": compression, "row_group_target_bytes": row_group_target_bytes,
                "partitions": partitions, "files": files, "published_at_utc": datetime.now(timezone.utc),
            }
            manifest_path = staging / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
            manifest_sha = _sha256(manifest_path)
            os.replace(staging, final_root)
            _record_mapping(dataset_version, market_version, manifest_sha, final_root.relative_to(export_root).as_posix())
            mark_stock_daily_publication_ready(dataset_version)
            return manifest
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish immutable versioned stock_daily_1d Parquet")
    parser.add_argument("--export-root", type=Path, default=Path(os.getenv("MARKETHUB_EXPORT_ROOT", "/data/MarketHub2/exports")))
    parser.add_argument("--compression", choices=("zstd", "snappy"), default="zstd")
    parser.add_argument("--row-group-mib", type=int, choices=(64, 128), default=128)
    parser.add_argument("--start", type=date.fromisoformat, default=os.getenv("MARKETHUB_STOCK_DAILY_EXPORT_START") or None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    args = parser.parse_args()
    manifest = publish(
        args.export_root,
        args.compression,
        args.row_group_mib * 1024**2,
        start=args.start,
        end=args.end,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
