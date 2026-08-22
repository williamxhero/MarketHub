from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import statistics
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pyarrow.parquet as pq


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(fraction * len(ordered) + 0.999999) - 1))]


def _request(url: str, *, range_header: str | None = None) -> tuple[int, dict[str, str], bytes, float]:
    headers = {"Range": range_header} if range_header is not None else {}
    started = time.perf_counter()
    try:
        with urlopen(Request(url, headers=headers), timeout=60) as response:
            body = response.read()
            return response.status, {key.lower(): value for key, value in response.headers.items()}, body, (time.perf_counter() - started) * 1000
    except HTTPError as exc:
        return exc.code, {key.lower(): value for key, value in exc.headers.items()}, exc.read(), (time.perf_counter() - started) * 1000


def _json(url: str) -> tuple[dict[str, Any], float]:
    status, _, body, elapsed = _request(url)
    if status != 200:
        raise RuntimeError(f"GET {url} failed: {status} {body[:200]!r}")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected JSON body: {url}")
    return payload, elapsed


def _timings(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "max_ms": round(max(values), 3),
    }


def run(base_url: str, year: int, month: int, iterations: int) -> dict[str, object]:
    health, _ = _json(f"{base_url}/api/health")
    market_version = str(health["data_version"])
    resolve_url = f"{base_url}/api/exports/stock_daily_1d/resolve/{market_version}"
    resolved, _ = _json(resolve_url)
    dataset_version = str(resolved["dataset_version"])
    manifest_url = f"{base_url}{resolved['manifest_url']}"
    manifest_timings: list[float] = []
    manifest: dict[str, Any] = {}
    for _ in range(iterations):
        manifest, elapsed = _json(manifest_url)
        manifest_timings.append(elapsed)
    relative = f"year={year:04d}/month={month:02d}/bars.parquet"
    record = next((item for item in manifest["files"] if item["path"] == relative), None)
    if record is None:
        raise RuntimeError(f"manifest has no partition: {relative}")
    file_url = f"{base_url}{record['url']}"
    download: list[float] = []
    projection: list[float] = []
    filtered: list[float] = []
    payload: bytes = b""
    projected_rows: set[int] = set()
    filtered_rows: set[int] = set()
    sample_codes: list[str] | None = None
    for _ in range(iterations):
        status, headers, payload, elapsed = _request(file_url)
        if status != 200 or int(headers["content-length"]) != len(payload):
            raise RuntimeError("full file response contract failed")
        download.append(elapsed)
        started = time.perf_counter()
        projected = pq.read_table(io.BytesIO(payload), columns=["code", "trade_date", "close", "volume"])
        projection.append((time.perf_counter() - started) * 1000)
        projected_rows.add(projected.num_rows)
        if sample_codes is None:
            sample_codes = sorted(set(projected["code"].to_pylist()))[:200]
        started = time.perf_counter()
        selected = pq.read_table(
            io.BytesIO(payload),
            columns=["code", "trade_date", "close", "volume"],
            filters=[("code", "in", sample_codes)],
        )
        filtered.append((time.perf_counter() - started) * 1000)
        filtered_rows.add(selected.num_rows)
    if len(projected_rows) != 1 or len(filtered_rows) != 1:
        raise RuntimeError("read row count drifted")
    expected_sha = str(record["sha256"])
    actual_sha = hashlib.sha256(payload).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError("full file checksum mismatch")
    midpoint = len(payload) // 2
    first_status, first_headers, first, _ = _request(file_url, range_header=f"bytes=0-{midpoint - 1}")
    second_status, second_headers, second, _ = _request(file_url, range_header=f"bytes={midpoint}-")
    if first_status != 206 or second_status != 206 or hashlib.sha256(first + second).hexdigest() != expected_sha:
        raise RuntimeError("Range resume/checksum contract failed")
    duckdb_result: dict[str, object]
    try:
        import duckdb

        query = "select count(*) from read_parquet(?, hive_partitioning=false) where code=any(?)"
        connection = duckdb.connect()
        count = connection.execute(query, [file_url, sample_codes]).fetchone()[0]
        timings: list[float] = []
        for _ in range(iterations):
            started = time.perf_counter()
            repeated = connection.execute(query, [file_url, sample_codes]).fetchone()[0]
            timings.append((time.perf_counter() - started) * 1000)
            if repeated != count:
                raise RuntimeError("DuckDB remote row count drifted")
        plan = connection.execute("explain " + query, [file_url, sample_codes]).fetchall()
        duckdb_result = {
            "available": True,
            "filtered_rows": int(count),
            "warm": _timings(timings),
            "plan": plan,
        }
    except Exception as exc:
        duckdb_result = {"available": False, "error": str(exc)}
    return {
        "contract": "markethub-stock-daily-parquet-http-benchmark-v1",
        "base_url": base_url,
        "market_data_version": market_version,
        "dataset_version": dataset_version,
        "partition": relative,
        "iterations": iterations,
        "manifest": _timings(manifest_timings),
        "download": {**_timings(download), "wire_bytes": len(payload)},
        "pyarrow_projection": {**_timings(projection), "rows": next(iter(projected_rows))},
        "pyarrow_filter_200_codes": {**_timings(filtered), "rows": next(iter(filtered_rows))},
        "checksum": {"expected": expected_sha, "actual": actual_sha, "match": True},
        "range_resume": {
            "match": True,
            "first_content_range": first_headers.get("content-range"),
            "second_content_range": second_headers.get("content-range"),
        },
        "duckdb_remote_filter": duckdb_result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark a published monthly Parquet over the 1 Gbps client path")
    parser.add_argument("--base-url", default="http://yosef-server:8803")
    parser.add_argument("--year", type=int, default=2021)
    parser.add_argument("--month", type=int, default=1)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.iterations < 1 or not 1 <= args.month <= 12:
        parser.error("invalid iterations/month")
    report = run(args.base_url.rstrip("/"), args.year, args.month, args.iterations)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, args.output)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
