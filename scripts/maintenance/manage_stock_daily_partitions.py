"""Operator-only dry-run/apply cleanup for immutable stock daily partitions."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

try:
    from scripts.publisher.stock_daily_partition_lifecycle import (
        apply_cleanup_plan,
        build_cleanup_plan,
    )
except ModuleNotFoundError:  # pragma: no cover - standalone deployed operator copy
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "publisher"))
    from stock_daily_partition_lifecycle import apply_cleanup_plan, build_cleanup_plan


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run/apply stock_daily_1d partition cleanup")
    parser.add_argument("--export-root", type=Path, default=Path("/data/MarketHub2/exports"))
    parser.add_argument("--active-version", required=True)
    parser.add_argument("--rollback-version", action="append", default=[])
    parser.add_argument("--pin-version", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True, help="dry-run manifest path")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest", type=Path, help="approved dry-run manifest for --apply")
    parser.add_argument(
        "--audit-dir",
        type=Path,
        default=Path("/data/markethub/audit/stock-daily-cleanup"),
    )
    args = parser.parse_args()

    if args.apply:
        if args.manifest is None:
            parser.error("--apply requires --manifest")
        plan = json.loads(args.manifest.read_text(encoding="utf-8"))
        result = apply_cleanup_plan(args.export_root.resolve(), plan, args.audit_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    plan = build_cleanup_plan(
        args.export_root.resolve(),
        active_version=args.active_version,
        rollback_versions=set(args.rollback_version),
        pinned_versions=set(args.pin_version),
    )
    encoded = json.dumps(
        plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    plan["plan_sha256"] = _sha256(encoded)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
