from __future__ import annotations

import argparse
from datetime import date
import json

from services.daily_coverage_read_model import build_current_stock_daily_coverage
from services.minute_coverage_read_model import build_stock_1m_daily_coverage


def main() -> int:
    parser = argparse.ArgumentParser(description="Build MarketHub query read models for the current dataset version")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--dataset", choices=("stock_daily_1d", "stock_bar_1m"), default="stock_daily_1d")
    args = parser.parse_args()
    result = (
        build_current_stock_daily_coverage(args.start, args.end)
        if args.dataset == "stock_daily_1d"
        else build_stock_1m_daily_coverage(args.start, args.end)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
