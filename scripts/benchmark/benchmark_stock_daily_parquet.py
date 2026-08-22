from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any

import pyarrow.parquet as pq


PUBLISHER_PATH = Path(__file__).resolve().parents[1] / "publisher" / "publish_stock_daily_parquet.py"
SPEC = importlib.util.spec_from_file_location("publish_stock_daily_parquet_for_benchmark", PUBLISHER_PATH)
assert SPEC is not None and SPEC.loader is not None
PUBLISHER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PUBLISHER
SPEC.loader.exec_module(PUBLISHER)

PROFILES = (("zstd-64", "zstd", 64), ("zstd-128", "zstd", 128), ("snappy-64", "snappy", 64), ("snappy-128", "snappy", 128))


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(fraction * len(ordered) + 0.999999) - 1))]


def _logical_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for batch in pq.ParquetFile(path).iter_batches(batch_size=8192):
        digest.update(batch.serialize().to_pybytes())
    return digest.hexdigest()


def _read_benchmark(path: Path, iterations: int, sample_codes: list[str]) -> dict[str, object]:
    scenarios = {
        "projection": {"columns": ["code", "trade_date", "close", "volume"]},
        "filter_200_codes": {"columns": ["code", "trade_date", "close", "volume"], "filters": [("code", "in", sample_codes)]},
    }
    output: dict[str, object] = {}
    for name, kwargs in scenarios.items():
        timings: list[float] = []
        row_counts: set[int] = set()
        for _ in range(iterations):
            started = time.perf_counter()
            table = pq.read_table(path, **kwargs)
            timings.append((time.perf_counter() - started) * 1000)
            row_counts.add(table.num_rows)
        if len(row_counts) != 1:
            raise AssertionError(f"read row count drifted: {name}")
        output[name] = {
            "rows": next(iter(row_counts)),
            "p50_ms": round(statistics.median(timings), 3),
            "p95_ms": round(_percentile(timings, 0.95), 3),
            "max_ms": round(max(timings), 3),
        }
    return output


def run(start: date, end: date, output_root: Path, iterations: int) -> dict[str, object]:
    if output_root.exists():
        raise FileExistsError(output_root)
    output_root.mkdir(parents=True)
    candidates: list[dict[str, Any]] = []
    hashes: set[str] = set()
    with PUBLISHER._connect() as connection:
        connection.execute("set transaction isolation level repeatable read read only")
        for name, compression, row_group_mib in PROFILES:
            root = output_root / name
            root.mkdir()
            coverage_path, bars_path = root / "coverage.parquet", root / "bars.parquet"
            started = time.perf_counter()
            coverage_rows, expected_rows = PUBLISHER._coverage(connection, coverage_path, start, end, compression)
            bars_rows = PUBLISHER._write_bars(connection, bars_path, start, end, compression, row_group_mib * 1024**2)
            write_ms = (time.perf_counter() - started) * 1000
            if bars_rows != expected_rows:
                raise AssertionError(f"bars/coverage mismatch for {name}: {bars_rows}/{expected_rows}")
            logical_hash = _logical_hash(bars_path)
            hashes.add(logical_hash)
            code_column = pq.read_table(bars_path, columns=["code"])["code"]
            sample_codes = sorted(set(code_column.to_pylist()))[:200]
            parquet = pq.ParquetFile(bars_path)
            candidates.append(
                {
                    "name": name, "compression": compression, "row_group_mib": row_group_mib,
                    "bars_rows": bars_rows, "coverage_rows": coverage_rows,
                    "bars_bytes": bars_path.stat().st_size, "coverage_bytes": coverage_path.stat().st_size,
                    "row_groups": parquet.metadata.num_row_groups,
                    "logical_sha256": logical_hash, "write_ms": round(write_ms, 3),
                    "reads": _read_benchmark(bars_path, iterations, sample_codes),
                }
            )
        connection.rollback()
    if len(hashes) != 1:
        raise AssertionError("candidate logical rows differ")
    return {
        "contract": "markethub-stock-daily-parquet-benchmark-v1",
        "scope": "representative_month",
        "start_date": start, "end_date_exclusive": end,
        "iterations": iterations, "created_at_utc": datetime.now(timezone.utc),
        "candidates": candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2021, 1, 1))
    parser.add_argument("--end-exclusive", type=date.fromisoformat, default=date(2021, 2, 1))
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.start >= args.end_exclusive or args.iterations < 1:
        parser.error("invalid date range or iterations")
    report = run(args.start, args.end_exclusive, args.output_root.resolve(), args.iterations)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
