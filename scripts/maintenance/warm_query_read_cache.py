from __future__ import annotations

import argparse
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _get(url: str) -> dict[str, object]:
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=60) as response:
        content = response.read()
        return {"url": url, "status": response.status, "bytes": len(content)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Warm versioned MarketHub query caches without provider or database writes")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--profile-code", default="600000")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    health = json.loads(urlopen(f"{base}/api/health", timeout=30).read())
    market_version = str(health["data_version"])
    versions = health["dataset_versions"]
    urls = (
        f"{base}/api/stocks/catalog?{urlencode({'limit': 5000, 'data_version': market_version})}",
        f"{base}/api/stocks/{args.profile_code}/profile/basic?{urlencode({'dataset_version': versions['stock_reference']})}",
        f"{base}/api/stocks/quotes/daily-snapshot?{urlencode({'trade_date': args.trade_date, 'limit': 10000, 'dataset_version': versions['stock_daily_1d']})}",
        f"{base}/api/stocks/quotes/daily-local-window?{urlencode({'start_date': args.trade_date, 'end_date': args.trade_date, 'limit': 10000, 'dataset_version': versions['stock_daily_1d']})}",
    )
    results = [_get(url) for url in urls]
    print(json.dumps({"contract": "markethub-query-cache-warm-v1", "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
