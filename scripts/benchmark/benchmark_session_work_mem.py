from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

import psycopg


WORK_MEM_VALUES = (16, 32, 64)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(fraction * len(ordered) + 0.999999) - 1))
    return ordered[index]


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        connect_timeout=10,
    )


def _row_fingerprint(rows: list[tuple[Any, ...]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(repr(row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _run_profile(work_mem_mb: int, iterations: int, start_time: str, end_time: str) -> dict[str, Any]:
    if work_mem_mb not in WORK_MEM_VALUES:
        raise ValueError(f"unsupported work_mem: {work_mem_mb}")
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"set work_mem = '{work_mem_mb}MB'")
            cursor.execute(
                """
                select s.code
                from ref.stock s
                where exists (
                    select 1
                    from fact.stock_bar_1m b
                    where b.code = s.code
                      and b.bar_time >= %s::timestamp
                      and b.bar_time <= %s::timestamp
                )
                order by s.code
                limit 200
                """,
                (start_time, end_time),
            )
            codes = [row[0] for row in cursor.fetchall()]
            if len(codes) != 200:
                raise RuntimeError(f"expected 200 benchmark codes, got {len(codes)}")
            query = """
                select code, bar_time, open, high, low, close, volume, amount
                from fact.stock_bar_1m
                where code = any(%s)
                  and bar_time >= %s::timestamp
                  and bar_time <= %s::timestamp
                order by code, bar_time
            """
            timings: list[float] = []
            fingerprints: set[str] = set()
            row_count = 0
            for _ in range(iterations):
                started = time.perf_counter()
                cursor.execute(query, (codes, start_time, end_time))
                rows = cursor.fetchall()
                timings.append((time.perf_counter() - started) * 1_000)
                row_count = len(rows)
                fingerprints.add(_row_fingerprint(rows))
            if len(fingerprints) != 1:
                raise AssertionError("session work_mem query result drifted")
            cursor.execute(
                "explain (analyze, buffers, format json) " + query,
                (codes, start_time, end_time),
            )
            plan = cursor.fetchone()[0][0]
            return {
                "work_mem_mb": work_mem_mb,
                "iterations": iterations,
                "row_count": row_count,
                "result_sha256": next(iter(fingerprints)),
                "p50_ms": round(statistics.median(timings), 3),
                "p95_ms": round(_percentile(timings, 0.95), 3),
                "max_ms": round(max(timings), 3),
                "plan_execution_ms": plan.get("Execution Time"),
                "plan_planning_ms": plan.get("Planning Time"),
                "plan": plan["Plan"],
            }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark known 200-stock SQL with session-level work_mem")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--start-time", default="2026-07-15 09:30:00")
    parser.add_argument("--end-time", default="2026-07-15 15:00:00")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    profiles = [_run_profile(value, args.iterations, args.start_time, args.end_time) for value in WORK_MEM_VALUES]
    fingerprints = {profile["result_sha256"] for profile in profiles}
    if len(fingerprints) != 1:
        raise AssertionError("result changed between work_mem profiles")
    report = {
        "query": "200-stock stock_bar_1m ordered window",
        "start_time": args.start_time,
        "end_time": args.end_time,
        "profiles": profiles,
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
