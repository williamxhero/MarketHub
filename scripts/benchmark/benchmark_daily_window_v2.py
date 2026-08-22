from __future__ import annotations

import argparse
import gzip
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

import pyarrow as pa


DEFAULT_SNAPSHOT = Path(
    r"D:\WILL\STOCK\apex_proj\market-hub-adapter\runtime\formal-2021-2025"
    r"\quarters-pre-calendar-completeness\2021-01\final\stock_trade_stat_snapshot.sqlite"
)


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes, dict[str, float | int]]:
    parsed = urlsplit(base_url)
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=120)
    started = time.perf_counter()
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    headers_received = time.perf_counter()
    chunks: list[bytes] = []
    wire_bytes = 0
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
        wire_bytes += len(chunk)
    downloaded = time.perf_counter()
    connection.close()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    wire_body = b"".join(chunks)
    if response_headers.get("content-encoding", "").lower() == "gzip":
        decoded_body = gzip.decompress(wire_body)
    else:
        decoded_body = wire_body
    decompressed = time.perf_counter()
    metrics: dict[str, float | int] = {
        "ttfb_ms": round((headers_received - started) * 1000, 3),
        "download_ms": round((downloaded - headers_received) * 1000, 3),
        "decompress_ms": round((decompressed - downloaded) * 1000, 3),
        "wire_bytes": wire_bytes,
        "decoded_bytes": len(decoded_body),
    }
    return response.status, response_headers, decoded_body, metrics


def _decode_json(body: bytes) -> tuple[Any, float]:
    started = time.perf_counter()
    value = json.loads(body)
    return value, round((time.perf_counter() - started) * 1000, 3)


def _load_snapshot(path: Path, start_date: str, end_date: str) -> tuple[list[str], dict[tuple[str, str], tuple[float, ...]]]:
    start_day = int(start_date.replace("-", ""))
    end_day = int(end_date.replace("-", ""))
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """
            select security_code, trading_day, open, high, low, close, volume
            from stock_daily_bars
            where trading_day between ? and ?
            order by trading_day, security_code
            """,
            (start_day, end_day),
        ).fetchall()
    expected: dict[tuple[str, str], tuple[float, ...]] = {}
    for code, trading_day, open_, high, low, close, volume in rows:
        day_text = str(trading_day)
        trade_date = f"{day_text[:4]}-{day_text[4:6]}-{day_text[6:8]}"
        expected[(trade_date, str(code))] = tuple(float(value) for value in (open_, high, low, close, volume))
    return sorted({code for _, code in expected}), expected


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


def _server_snapshot(host: str, interface: str) -> dict[str, str | int]:
    if not interface or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for character in interface):
        raise ValueError(f"invalid server interface: {interface!r}")
    command = "\n".join(
        (
            "set -euo pipefail",
            "systemctl show markethub-api.service "
            "-p ActiveState -p SubState -p NRestarts -p CPUUsageNSec "
            "-p MemoryCurrent -p MemoryPeak -p MemorySwapCurrent",
            f"printf 'NetRxBytes='; cat /sys/class/net/{interface}/statistics/rx_bytes",
            f"printf 'NetTxBytes='; cat /sys/class/net/{interface}/statistics/tx_bytes",
        )
    )
    completed = subprocess.run(
        ["ssh", host, command],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    snapshot: dict[str, str | int] = {}
    for line in completed.stdout.splitlines():
        key, separator, raw_value = line.partition("=")
        if not separator:
            continue
        value = raw_value.strip()
        try:
            snapshot[key] = int(value)
        except ValueError:
            snapshot[key] = value
    return snapshot


def _server_delta(before: dict[str, str | int], after: dict[str, str | int]) -> dict[str, int]:
    delta: dict[str, int] = {}
    for key in ("CPUUsageNSec", "NetRxBytes", "NetTxBytes", "NRestarts"):
        before_value = before.get(key)
        after_value = after.get(key)
        if isinstance(before_value, int) and isinstance(after_value, int):
            delta[key] = after_value - before_value
    return delta


def _assert_success(status: int, body: bytes) -> None:
    if status < 200 or status >= 300:
        preview = body[:2000].decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {status}: {preview}")


def _legacy_run(base_url: str, start_date: str, end_date: str, page_size: int, codes: set[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    offset = 0
    selected: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    started = time.perf_counter()
    while True:
        query = urlencode(
            {
                "start_date": start_date,
                "end_date": end_date,
                "limit": page_size,
                "offset": offset,
            }
        )
        status, headers, body, metrics = _request(
            base_url,
            "GET",
            f"/api/stocks/quotes/daily-local-window?{query}",
            headers={"Accept-Encoding": "identity"},
        )
        _assert_success(status, body)
        page, decode_ms = _decode_json(body)
        metrics["decode_ms"] = decode_ms
        metrics["status"] = status
        metrics["server_timing_ms"] = _parse_server_timing(headers.get("server-timing", ""))
        requests.append(metrics)
        selected.extend(item for item in page if str(item.get("code", "")) in codes)
        if len(page) < page_size:
            break
        offset += len(page)
    total_download_ms = sum(float(request["download_ms"]) for request in requests)
    total_wire_bytes = sum(int(request["wire_bytes"]) for request in requests)
    total_decoded_bytes = sum(int(request["decoded_bytes"]) for request in requests)
    return selected, {
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "request_count": len(requests),
        "wire_bytes": total_wire_bytes,
        "decoded_bytes": total_decoded_bytes,
        "network_download_mib_per_second": round(
            total_wire_bytes / (1024 * 1024) / (total_download_ms / 1000), 3
        ) if total_download_ms > 0 else None,
        "client_decode_ms": round(sum(float(request["decode_ms"]) for request in requests), 3),
        "requests": requests,
    }


def _v2_run(
    base_url: str,
    data_version: str,
    start_date: str,
    end_date: str,
    page_size: int,
    codes: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cursor: str | None = None
    items: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    metas: list[dict[str, Any]] = []
    meta_summaries: list[dict[str, Any]] = []
    started = time.perf_counter()
    while True:
        payload = {
            "data_version": data_version,
            "freq": "1d",
            "universe": "codes",
            "codes": codes,
            "start_date": start_date,
            "end_date": end_date,
            "page_size": page_size,
            "cursor": cursor,
        }
        request_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        status, headers, body, metrics = _request(
            base_url,
            "POST",
            "/api/stocks/quotes/daily-window/query",
            body=request_body,
            headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"},
        )
        _assert_success(status, body)
        response, decode_ms = _decode_json(body)
        page_items = response["items"]
        meta = response["meta"]
        metrics["decode_ms"] = decode_ms
        metrics["status"] = status
        metrics["server_timing_ms"] = _parse_server_timing(headers.get("server-timing", ""))
        requests.append(metrics)
        metas.append(meta)
        if meta["data_version"] != data_version:
            raise AssertionError("response data_version drifted")
        if not meta["complete"] or not meta["request_complete"] or not meta["page_complete"]:
            raise AssertionError("V2 completeness contract failed")
        if meta["truncated"]:
            raise AssertionError("V2 response reported truncation")
        if len(meta["coverage"]) != len(codes):
            raise AssertionError("V2 coverage code count mismatch")
        if any(not coverage["complete"] or coverage["missing_rows"] for coverage in meta["coverage"]):
            raise AssertionError("V2 per-code coverage failed")
        meta_summaries.append(
            {
                "data_version": meta["data_version"],
                "total_rows": meta["total_rows"],
                "returned_rows": meta["returned_rows"],
                "complete": meta["complete"],
                "truncated": meta["truncated"],
                "page_complete": meta["page_complete"],
                "request_complete": meta["request_complete"],
                "delivery_complete": meta["delivery_complete"],
                "has_next_cursor": meta.get("next_cursor") is not None,
                "universe_kind": meta["universe_kind"],
                "universe_size": meta["universe_size"],
                "coverage": {
                    "code_count": len(meta["coverage"]),
                    "expected_rows": sum(int(item["expected_rows"]) for item in meta["coverage"]),
                    "actual_rows": sum(int(item["actual_rows"]) for item in meta["coverage"]),
                    "missing_rows": sum(int(item["missing_rows"]) for item in meta["coverage"]),
                    "incomplete_codes": sum(not bool(item["complete"]) for item in meta["coverage"]),
                },
            }
        )
        items.extend(page_items)
        cursor = meta.get("next_cursor")
        if cursor is None:
            if not meta["delivery_complete"]:
                raise AssertionError("final page is not delivery_complete")
            break
        if meta["delivery_complete"]:
            raise AssertionError("non-final page reported delivery_complete")
    if sum(meta["returned_rows"] for meta in metas) != len(items):
        raise AssertionError("returned_rows does not match delivered items")
    if any(meta["total_rows"] != len(items) for meta in metas):
        raise AssertionError("total_rows is not stable across pages")
    total_download_ms = sum(float(request["download_ms"]) for request in requests)
    total_wire_bytes = sum(int(request["wire_bytes"]) for request in requests)
    total_decoded_bytes = sum(int(request["decoded_bytes"]) for request in requests)
    return items, {
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "request_count": len(requests),
        "wire_bytes": total_wire_bytes,
        "decoded_bytes": total_decoded_bytes,
        "network_download_mib_per_second": round(
            total_wire_bytes / (1024 * 1024) / (total_download_ms / 1000), 3
        ) if total_download_ms > 0 else None,
        "client_decode_ms": round(sum(float(request["decode_ms"]) for request in requests), 3),
        "requests": requests,
        "metas": meta_summaries,
    }


def _v2_arrow_run(
    base_url: str,
    data_version: str,
    start_date: str,
    end_date: str,
    page_size: int,
    codes: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cursor: str | None = None
    items: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    meta_summaries: list[dict[str, Any]] = []
    started = time.perf_counter()
    while True:
        payload = {
            "data_version": data_version, "freq": "1d", "universe": "codes", "codes": codes,
            "start_date": start_date, "end_date": end_date, "page_size": page_size, "cursor": cursor,
        }
        status, headers, body, metrics = _request(
            base_url,
            "POST",
            "/api/stocks/quotes/daily-window/query",
            body=json.dumps(payload, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/vnd.apache.arrow.stream", "Accept-Encoding": "identity"},
        )
        _assert_success(status, body)
        if headers.get("content-type", "").split(";", 1)[0] != "application/vnd.apache.arrow.stream":
            raise AssertionError(f"unexpected Arrow content type: {headers.get('content-type')}")
        decode_started = time.perf_counter()
        reader = pa.ipc.open_stream(body)
        page_items = reader.read_all().to_pylist()
        schema_metadata = reader.schema.metadata or {}
        meta = json.loads(schema_metadata[b"markethub.meta"])
        metrics["decode_ms"] = round((time.perf_counter() - decode_started) * 1000, 3)
        metrics["status"] = status
        metrics["server_timing_ms"] = _parse_server_timing(headers.get("server-timing", ""))
        requests.append(metrics)
        if schema_metadata.get(b"markethub.schema_version") != b"markethub-daily-window-arrow-v1":
            raise AssertionError("Arrow schema version mismatch")
        if meta["data_version"] != data_version or not meta["complete"] or not meta["request_complete"] or not meta["page_complete"]:
            raise AssertionError("Arrow completeness/version contract failed")
        if int(headers.get("x-markethub-returned-rows", "-1")) != len(page_items) or meta["returned_rows"] != len(page_items):
            raise AssertionError("Arrow returned row count mismatch")
        items.extend(page_items)
        meta_summaries.append({
            "data_version": meta["data_version"], "total_rows": meta["total_rows"], "returned_rows": meta["returned_rows"],
            "delivery_complete": meta["delivery_complete"], "has_next_cursor": meta.get("next_cursor") is not None,
            "coverage_code_count": len(meta["coverage"]),
        })
        cursor = meta.get("next_cursor")
        if cursor is None:
            if not meta["delivery_complete"]:
                raise AssertionError("final Arrow page is not delivery_complete")
            break
    total_download_ms = sum(float(request["download_ms"]) for request in requests)
    total_wire_bytes = sum(int(request["wire_bytes"]) for request in requests)
    return items, {
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "request_count": len(requests),
        "wire_bytes": total_wire_bytes,
        "decoded_bytes": total_wire_bytes,
        "network_download_mib_per_second": round(total_wire_bytes / (1024 * 1024) / (total_download_ms / 1000), 3) if total_download_ms > 0 else None,
        "client_decode_ms": round(sum(float(request["decode_ms"]) for request in requests), 3),
        "requests": requests,
        "metas": meta_summaries,
    }


def _compare(
    items: list[dict[str, Any]],
    expected: dict[tuple[str, str], tuple[float, ...]],
    *,
    require_sorted: bool,
) -> dict[str, Any]:
    actual: dict[tuple[str, str], tuple[float, ...]] = {}
    duplicate_rows = 0
    order_errors = 0
    previous_key: tuple[str, str] | None = None
    for item in items:
        key = (str(item["trade_time"])[:10], str(item["code"]))
        if previous_key is not None and key <= previous_key:
            order_errors += 1
        previous_key = key
        if key in actual:
            duplicate_rows += 1
        actual[key] = tuple(float(item[field]) for field in ("open", "high", "low", "close", "volume"))
    missing = set(expected) - set(actual)
    extra = set(actual) - set(expected)
    value_differences = 0
    for key in set(expected) & set(actual):
        if any(not math.isclose(left, right, rel_tol=0.0, abs_tol=1e-9) for left, right in zip(expected[key], actual[key])):
            value_differences += 1
    result: dict[str, Any] = {
        "expected_rows": len(expected),
        "actual_rows": len(actual),
        "expected_codes": len({code for _, code in expected}),
        "actual_codes": len({code for _, code in actual}),
        "missing_rows": len(missing),
        "extra_rows": len(extra),
        "duplicate_rows": duplicate_rows,
        "order_errors": order_errors,
        "ohlcv_differences": value_differences,
        "missing_sample": [list(key) for key in sorted(missing)[:10]],
        "extra_sample": [list(key) for key in sorted(extra)[:10]],
    }
    required_zero_fields = ["missing_rows", "extra_rows", "duplicate_rows", "ohlcv_differences"]
    if require_sorted:
        required_zero_fields.append("order_errors")
    if any(result[field] for field in required_zero_fields):
        raise AssertionError(f"snapshot comparison failed: {result}")
    return result


def _percentile(values: list[float], percentile: float) -> float:
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reproducible MarketHub daily-window benchmark")
    parser.add_argument("--mode", choices=("legacy", "v2", "v2-arrow"), required=True)
    parser.add_argument("--base-url", default="http://yosef-server:8803")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--start-date", default="2021-01-01")
    parser.add_argument("--end-date", default="2021-01-31")
    parser.add_argument("--page-size", type=int, default=50000)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--max-p50-seconds", type=float)
    parser.add_argument("--max-p95-seconds", type=float)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--server-host", default="yosef-server")
    parser.add_argument("--server-interface", default="eno1")
    args = parser.parse_args()

    codes, expected = _load_snapshot(args.snapshot, args.start_date, args.end_date)
    status, _, health_body, _ = _request(args.base_url, "GET", "/api/health")
    _assert_success(status, health_body)
    health, _ = _decode_json(health_body)
    data_version = str(health["data_version"])

    server_before = _server_snapshot(args.server_host, args.server_interface)
    runs: list[dict[str, Any]] = []
    for _ in range(args.iterations):
        if args.mode == "legacy":
            items, metrics = _legacy_run(args.base_url, args.start_date, args.end_date, args.page_size, set(codes))
        elif args.mode == "v2":
            items, metrics = _v2_run(args.base_url, data_version, args.start_date, args.end_date, args.page_size, codes)
        else:
            items, metrics = _v2_arrow_run(args.base_url, data_version, args.start_date, args.end_date, args.page_size, codes)
        metrics["comparison"] = _compare(items, expected, require_sorted=args.mode in {"v2", "v2-arrow"})
        runs.append(metrics)
    server_after = _server_snapshot(args.server_host, args.server_interface)

    elapsed_seconds = [float(run["elapsed_ms"]) / 1000 for run in runs]
    summary = {
        "mode": args.mode,
        "base_url": args.base_url,
        "snapshot": str(args.snapshot),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "data_version": data_version,
        "iterations": args.iterations,
        "page_size": args.page_size,
        "p50_seconds": round(statistics.median(elapsed_seconds), 6),
        "p95_seconds": round(_percentile(elapsed_seconds, 0.95), 6),
        "server": {
            "host": args.server_host,
            "interface": args.server_interface,
            "before": server_before,
            "after": server_after,
            "delta": _server_delta(server_before, server_after),
        },
        "runs": runs,
    }
    encoded = json.dumps(summary, ensure_ascii=False, indent=2)
    print(encoded)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    if args.max_p50_seconds is not None and summary["p50_seconds"] > args.max_p50_seconds:
        return 2
    if args.max_p95_seconds is not None and summary["p95_seconds"] > args.max_p95_seconds:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
