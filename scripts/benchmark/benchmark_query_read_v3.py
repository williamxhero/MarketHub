from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import statistics
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * quantile + 0.999999) - 1))]


def request(url: str, *, method: str = "GET", body: dict[str, object] | None = None, accept: str = "application/json") -> dict[str, object]:
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {"Accept": accept}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    started = time.perf_counter()
    first_byte_at = started
    try:
        with urlopen(Request(url, data=payload, method=method, headers=headers), timeout=120) as response:
            first = response.read(1)
            first_byte_at = time.perf_counter()
            content = first + response.read()
            status = response.status
            response_headers = dict(response.headers.items())
    except HTTPError as exc:
        first = exc.read(1)
        first_byte_at = time.perf_counter()
        content = first + exc.read()
        status = exc.code
        response_headers = dict(exc.headers.items())
    finished = time.perf_counter()
    return {
        "status": status,
        "elapsed_ms": (finished - started) * 1000,
        "ttfb_ms": (first_byte_at - started) * 1000,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "server_timing": response_headers.get("Server-Timing", ""),
    }


def summarize(rows: list[dict[str, object]]) -> dict[str, object]:
    elapsed = [float(row["elapsed_ms"]) for row in rows]
    ttfb = [float(row["ttfb_ms"]) for row in rows]
    return {
        "requests": len(rows), "statuses": sorted({int(row["status"]) for row in rows}),
        "p50_ms": round(statistics.median(elapsed), 3), "p95_ms": round(percentile(elapsed, .95), 3), "p99_ms": round(percentile(elapsed, .99), 3),
        "ttfb_p95_ms": round(percentile(ttfb, .95), 3), "bytes": sorted({int(row["bytes"]) for row in rows}),
        "hashes": sorted({str(row["sha256"]) for row in rows}), "server_timing": sorted({str(row["server_timing"]) for row in rows if row["server_timing"]}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reproducible MarketHub query-read-v3 HTTP benchmark")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--concurrency", type=int, nargs="+", default=(1, 6))
    parser.add_argument("--codes", default="600000,000001,000002,000063,000333,000651")
    parser.add_argument("--output")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    health = json.loads(urlopen(f"{base}/api/health", timeout=30).read())
    market_version = str(health["data_version"])
    datasets = health.get("dataset_versions", {})
    codes = [value.strip() for value in args.codes.split(",") if value.strip()]
    cases = {
        "daily_window": (f"{base}/api/stocks/quotes/daily-window/query", "POST", {"dataset_version": datasets.get("stock_daily_1d", ""), "universe": "all_a", "start_date": args.trade_date, "end_date": args.trade_date, "page_size": 100000, "meta_detail": "summary"}, "application/json"),
        "daily_snapshot": (f"{base}/api/stocks/quotes/daily-snapshot?trade_date={args.trade_date}&limit=10000&dataset_version={datasets.get('stock_daily_1d','')}", "GET", None, "application/json"),
        "daily_local_10000": (f"{base}/api/stocks/quotes/daily-local-window?start_date={args.trade_date}&end_date={args.trade_date}&limit=10000&dataset_version={datasets.get('stock_daily_1d','')}", "GET", None, "application/json"),
        "catalog": (f"{base}/api/stocks/catalog?limit=5000&data_version={market_version}", "GET", None, "application/json"),
        "profile": (f"{base}/api/stocks/600000/profile/basic?dataset_version={datasets.get('stock_reference','')}", "GET", None, "application/json"),
        "futures_coverage": (f"{base}/api/futures/coverage", "GET", None, "application/json"),
        "stock_1m_arrow": (f"{base}/api/stocks/quotes/query", "POST", {"codes": codes, "freq": "1m", "trade_date": args.trade_date, "dataset_version": datasets.get("stock_bar_1m", "")}, "application/vnd.apache.arrow.stream"),
    }
    results: dict[str, object] = {}
    for concurrency in args.concurrency:
        for name, (url, method, body, accept) in cases.items():
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                rows = list(pool.map(lambda _index: request(url, method=method, body=body, accept=accept), range(args.iterations)))
            results[f"{name}_c{concurrency}"] = summarize(rows)
    report = {"contract": "markethub-query-read-v3-benchmark-v1", "trade_date": args.trade_date, "codes": codes, "health_version": health.get("version"), "data_version": market_version, "dataset_versions": datasets, "results": results}
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(encoded)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
