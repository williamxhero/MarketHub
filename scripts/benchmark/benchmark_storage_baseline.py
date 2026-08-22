from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import http.client
import json
import math
from pathlib import Path
import sqlite3
import statistics
import subprocess
import time
from typing import Any
from urllib.parse import urlencode, urlsplit


DEFAULT_SNAPSHOT = Path(
    r"D:\WILL\STOCK\apex_proj\market-hub-adapter\runtime\formal-2021-2025"
    r"\quarters-pre-calendar-completeness\2021-01\final\stock_trade_stat_snapshot.sqlite"
)
CONCURRENCY_LEVELS = (1, 4, 8)


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    parsed = urlsplit(base_url)
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=180)
    started_at = time.perf_counter()
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    headers_at = time.perf_counter()
    chunks: list[bytes] = []
    while chunk := response.read(1024 * 1024):
        chunks.append(chunk)
    completed_at = time.perf_counter()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    wire_body = b"".join(chunks)
    decoded_body = gzip.decompress(wire_body) if response_headers.get("content-encoding", "").lower() == "gzip" else wire_body
    decode_started_at = time.perf_counter()
    payload = json.loads(decoded_body)
    decode_ms = (time.perf_counter() - decode_started_at) * 1_000
    if not 200 <= response.status < 300:
        raise RuntimeError(f"HTTP {response.status} {path}: {decoded_body[:2_000].decode(errors='replace')}")
    return {
        "status": response.status,
        "payload": payload,
        "headers": response_headers,
        "ttfb_ms": round((headers_at - started_at) * 1_000, 3),
        "elapsed_ms": round((completed_at - started_at) * 1_000, 3),
        "download_ms": round((completed_at - headers_at) * 1_000, 3),
        "decode_ms": round(decode_ms, 3),
        "wire_bytes": len(wire_body),
        "decoded_bytes": len(decoded_body),
        "server_timing_ms": _parse_server_timing(response_headers.get("server-timing", "")),
    }


def _parse_server_timing(value: str) -> dict[str, float]:
    timings: dict[str, float] = {}
    for part in value.split(","):
        fields = [field.strip() for field in part.split(";") if field.strip()]
        if not fields:
            continue
        duration = next((field[4:] for field in fields[1:] if field.startswith("dur=")), "")
        try:
            timings[fields[0]] = float(duration)
        except ValueError:
            continue
    return timings


def _load_snapshot_codes(path: Path, start_date: str, end_date: str) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """
            select distinct security_code
            from stock_daily_bars
            where trading_day between ? and ?
            order by security_code
            """,
            (int(start_date.replace("-", "")), int(end_date.replace("-", ""))),
        ).fetchall()
    codes = [str(row[0]) for row in rows]
    if len(codes) != 4_148:
        raise RuntimeError(f"daily benchmark corpus must contain exactly 4,148 codes, got {len(codes)}")
    return codes


def _scenario_requests(
    data_version: str,
    daily_codes: list[str],
    minute_codes: list[str],
    *,
    minute_date: str,
    include_admin_async: bool,
    selected_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    quote_headers = {"Content-Type": "application/json", "Accept-Encoding": "gzip"}

    def quote_payload(selected_codes: list[str]) -> bytes:
        return json.dumps(
            {
                "data_version": data_version,
                "codes": selected_codes,
                "freq": "1m",
                "start_time": f"{minute_date} 09:30:00",
                "end_time": f"{minute_date} 15:00:00",
                "adjust": "none",
                "meta_detail": "summary",
            },
            separators=(",", ":"),
        ).encode()

    daily_body = json.dumps(
        {
            "data_version": data_version,
            "freq": "1d",
            "universe": "codes",
            "codes": daily_codes,
            "start_date": "2021-01-01",
            "end_date": "2021-01-31",
            "page_size": 100_000,
        },
        separators=(",", ":"),
    ).encode()
    scenarios: list[dict[str, Any]] = [
        {"name": "health", "method": "GET", "path": "/api/health"},
        {
            "name": "catalog",
            "method": "GET",
            "path": "/api/stocks/catalog?" + urlencode({"limit": 5000, "data_version": data_version}),
        },
        {
            "name": "ranking_aggregate",
            "method": "GET",
            "path": "/api/rankings/research/reports?" + urlencode({"start_date": "2021-01-01", "end_date": "2025-12-31", "limit": 5000}),
        },
    ]
    if selected_names is None or "stock_1m_single" in selected_names:
        scenarios.append({"name": "stock_1m_single", "method": "POST", "path": "/api/stocks/quotes/query", "body": quote_payload([minute_codes[0]]), "headers": quote_headers})
    if selected_names is None or "stock_1m_200" in selected_names:
        scenarios.append({"name": "stock_1m_200", "method": "POST", "path": "/api/stocks/quotes/query", "body": quote_payload(minute_codes), "headers": quote_headers})
    if selected_names is None or "daily_window_4148" in selected_names:
        scenarios.append({"name": "daily_window_4148", "method": "POST", "path": "/api/stocks/quotes/daily-window/query", "body": daily_body, "headers": quote_headers})
    if include_admin_async:
        scenarios.append({"name": "admin_run_due_async", "method": "POST", "path": "/api/admin/capture/run-due-async?dry_run=true"})
    return scenarios


def _validate_payload(name: str, payload: Any, data_version: str) -> str:
    if name == "health":
        if payload.get("status") != "ok" or payload.get("data_version") != data_version:
            raise AssertionError("health contract failed")
        payload = {key: value for key, value in payload.items() if key != "updated_at"}
    elif name == "catalog":
        if not isinstance(payload, list) or not payload:
            raise AssertionError("catalog response must be a non-empty list")
    elif name.startswith("stock_1m_"):
        if not isinstance(payload.get("items"), list) or not isinstance(payload.get("meta"), dict):
            raise AssertionError("minute quote contract failed")
        if payload["meta"].get("truncated"):
            raise AssertionError("minute quote response was truncated")
    elif name == "daily_window_4148":
        meta = payload.get("meta", {})
        if len(meta.get("coverage", [])) != 4_148 or not meta.get("delivery_complete"):
            raise AssertionError("daily window completeness contract failed")
        if meta.get("data_version") != data_version:
            raise AssertionError("daily window data version drifted")
    elif name == "ranking_aggregate" and not isinstance(payload, list):
        raise AssertionError("ranking response must be a list")
    elif name == "admin_run_due_async" and payload != {"accepted": False, "dry_run": True}:
        raise AssertionError("admin async acceptance contract failed")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _run_scenario(base_url: str, scenario: dict[str, Any], data_version: str) -> dict[str, Any]:
    result = _request(
        base_url,
        scenario["method"],
        scenario["path"],
        body=scenario.get("body"),
        headers=scenario.get("headers"),
    )
    result["result_sha256"] = _validate_payload(scenario["name"], result.pop("payload"), data_version)
    result.pop("headers")
    return result


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return ordered[index]


def _summarize_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed = [float(run["elapsed_ms"]) for run in runs]
    hashes = {str(run["result_sha256"]) for run in runs}
    if len(hashes) != 1:
        raise AssertionError(f"response drifted across warm runs: {sorted(hashes)}")
    return {
        "request_count": len(runs),
        "p50_ms": round(statistics.median(elapsed), 3),
        "p95_ms": round(_percentile(elapsed, 0.95), 3),
        "max_ms": round(max(elapsed), 3),
        "wire_bytes": sum(int(run["wire_bytes"]) for run in runs),
        "result_sha256": hashes.pop(),
        "runs": runs,
    }


def _remote_snapshot(host: str, interface: str, env_path: str) -> dict[str, Any]:
    if not all(character.isalnum() or character in "_.:-/" for character in interface + env_path):
        raise ValueError("invalid remote snapshot argument")
    command = f"""set -euo pipefail
systemctl show markethub-api.service -p ActiveState -p SubState -p NRestarts -p CPUUsageNSec -p MemoryCurrent -p MemoryPeak -p MemorySwapCurrent
printf 'NetRxBytes='; cat /sys/class/net/{interface}/statistics/rx_bytes
printf 'NetTxBytes='; cat /sys/class/net/{interface}/statistics/tx_bytes
awk '/^(SwapTotal|SwapFree):/ {{gsub(\" kB\",\"\"); printf \"%s=%s\\n\", $1, $2}}' /proc/meminfo
printf 'DataAvailableBytes='; df -B1 --output=avail /data | tail -n 1 | tr -d ' '
set -a
. {env_path}
set +a
export PGPASSWORD="$MARKETHUB_DB_PASSWORD"
db_json=$(psql -h "$MARKETHUB_DB_HOST" -p "$MARKETHUB_DB_PORT" -U "$MARKETHUB_DB_USER" -d "$MARKETHUB_DB_NAME" -Atqc "select json_build_object('temp_files',temp_files,'temp_bytes',temp_bytes,'blk_read_time',blk_read_time,'blk_write_time',blk_write_time,'xact_commit',xact_commit,'xact_rollback',xact_rollback) from pg_stat_database where datname=current_database()")
printf 'DatabaseStats=%s\\n' "$db_json"
if [[ $(psql -h "$MARKETHUB_DB_HOST" -p "$MARKETHUB_DB_PORT" -U "$MARKETHUB_DB_USER" -d "$MARKETHUB_DB_NAME" -Atqc "select to_regclass('public.pg_stat_statements') is not null") == t ]]; then
  pgss_json=$(psql -h "$MARKETHUB_DB_HOST" -p "$MARKETHUB_DB_PORT" -U "$MARKETHUB_DB_USER" -d "$MARKETHUB_DB_NAME" -Atqc "select json_build_object('calls',coalesce(sum(calls),0),'total_exec_time',coalesce(sum(total_exec_time),0),'temp_blks_read',coalesce(sum(temp_blks_read),0),'temp_blks_written',coalesce(sum(temp_blks_written),0)) from pg_stat_statements")
  printf 'PgStatStatements=%s\\n' "$pgss_json"
fi
"""
    completed = subprocess.run(["ssh", host, command], check=True, capture_output=True, text=True, timeout=60)
    snapshot: dict[str, Any] = {}
    for line in completed.stdout.splitlines():
        key, separator, raw_value = line.partition("=")
        if not separator:
            continue
        value = raw_value.strip()
        if value.startswith("{"):
            snapshot[key] = json.loads(value)
        else:
            try:
                snapshot[key] = int(value)
            except ValueError:
                snapshot[key] = value
    return snapshot


def _numeric_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    for key, after_value in after.items():
        before_value = before.get(key)
        if isinstance(before_value, (int, float)) and isinstance(after_value, (int, float)):
            delta[key] = after_value - before_value
        elif isinstance(before_value, dict) and isinstance(after_value, dict):
            delta[key] = _numeric_delta(before_value, after_value)
    return delta


def main() -> int:
    parser = argparse.ArgumentParser(description="MarketHub storage architecture baseline benchmark")
    parser.add_argument("--base-url", default="http://yosef-server:8803")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--minute-date", default="2026-07-15")
    parser.add_argument("--server-host", default="yosef-server")
    parser.add_argument("--server-interface", default="eno1")
    parser.add_argument("--server-env-path", default="/data/markethub/env/markethub.env")
    parser.add_argument("--include-admin-async", action="store_true", help="Include the non-mutating admin async dry-run probe")
    parser.add_argument("--scenarios", help="Comma-separated scenario names; default runs every scenario")
    parser.add_argument("--concurrency-levels", default="1,4,8", help="Comma-separated positive concurrency levels")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")

    requested_scenarios = None
    if args.scenarios:
        requested_scenarios = {item.strip() for item in args.scenarios.split(",") if item.strip()}
        known_scenarios = {"health", "catalog", "stock_1m_single", "stock_1m_200", "daily_window_4148", "ranking_aggregate"}
        if args.include_admin_async:
            known_scenarios.add("admin_run_due_async")
        unknown_scenarios = requested_scenarios - known_scenarios
        if unknown_scenarios:
            parser.error(f"unknown scenarios: {', '.join(sorted(unknown_scenarios))}")

    health = _request(args.base_url, "GET", "/api/health")
    data_version = str(health["payload"]["data_version"])
    needs_daily_codes = requested_scenarios is None or bool(requested_scenarios & {"stock_1m_single", "stock_1m_200", "daily_window_4148"})
    daily_codes = _load_snapshot_codes(args.snapshot, "2021-01-01", "2021-01-31") if needs_daily_codes else []
    needs_minute_codes = requested_scenarios is None or bool(requested_scenarios & {"stock_1m_single", "stock_1m_200"})
    minute_codes: list[str] = []
    if needs_minute_codes:
        catalog = _request(
            args.base_url,
            "GET",
            "/api/stocks/catalog?" + urlencode({"limit": 5000, "data_version": data_version}),
        )["payload"]
        daily_code_set = set(daily_codes)
        minute_codes = [str(item["code"]) for item in catalog if str(item.get("code", "")) in daily_code_set][:200]
        if len(minute_codes) != 200:
            raise RuntimeError(f"minute benchmark corpus must contain 200 current 2021-era codes, got {len(minute_codes)}")
    scenarios = _scenario_requests(
        data_version,
        daily_codes,
        minute_codes,
        minute_date=args.minute_date,
        include_admin_async=args.include_admin_async,
        selected_names=requested_scenarios,
    )
    if requested_scenarios is not None:
        scenarios = [scenario for scenario in scenarios if scenario["name"] in requested_scenarios]
    try:
        concurrency_levels = tuple(int(item.strip()) for item in args.concurrency_levels.split(",") if item.strip())
    except ValueError:
        parser.error("--concurrency-levels must contain integers")
    if not concurrency_levels or any(level < 1 for level in concurrency_levels):
        parser.error("--concurrency-levels must contain positive integers")
    server_before = _remote_snapshot(args.server_host, args.server_interface, args.server_env_path)
    results: dict[str, Any] = {}
    for scenario in scenarios:
        results[scenario["name"]] = {}
        for concurrency in concurrency_levels:
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                runs = list(executor.map(lambda _: _run_scenario(args.base_url, scenario, data_version), range(args.iterations)))
            results[scenario["name"]][str(concurrency)] = _summarize_runs(runs)
    server_after = _remote_snapshot(args.server_host, args.server_interface, args.server_env_path)
    report = {
        "base_url": args.base_url,
        "data_version": data_version,
        "snapshot": str(args.snapshot),
        "iterations_per_concurrency": args.iterations,
        "concurrency_levels": list(concurrency_levels),
        "minute_date": args.minute_date,
        "admin_async_included": args.include_admin_async,
        "server": {"before": server_before, "after": server_after, "delta": _numeric_delta(server_before, server_after)},
        "scenarios": results,
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
