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
        initialize_partition_catalog,
    )
except ModuleNotFoundError:  # pragma: no cover - standalone deployed operator copy
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "publisher"))
    from stock_daily_partition_lifecycle import (
        apply_cleanup_plan,
        build_cleanup_plan,
        initialize_partition_catalog,
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run/apply stock_daily_1d partition cleanup")
    parser.add_argument("--export-root", type=Path, default=Path("/data/MarketHub2/exports"))
    parser.add_argument("--active-version", required=True)
    parser.add_argument("--rollback-version", action="append", default=[])
    parser.add_argument("--pin-version", action="append", default=[])
    parser.add_argument("--research-pin-version", action="append", default=[])
    parser.add_argument("--audit-pin-version", action="append", default=[])
    parser.add_argument("--referenced-version", action="append", default=[])
    parser.add_argument("--staging-ttl-hours", type=float, default=24)
    parser.add_argument("--output", type=Path, help="dry-run manifest path")
    parser.add_argument(
        "--initialize", action="store_true", help="initialize an immutable partition catalog"
    )
    parser.add_argument("--dataset-version", help="dataset version for --initialize")
    parser.add_argument("--catalog-output", type=Path, help="optional partition catalog path")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest", type=Path, help="approved dry-run manifest for --apply")
    parser.add_argument(
        "--audit-dir",
        type=Path,
        default=Path("/data/markethub/audit/stock-daily-cleanup"),
    )
    args = parser.parse_args()

    if args.initialize:
        if not args.dataset_version:
            parser.error("--initialize requires --dataset-version")
        result = initialize_partition_catalog(
            args.export_root.resolve(), args.dataset_version, args.catalog_output
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.apply:
        if args.manifest is None:
            parser.error("--apply requires --manifest")
        plan = json.loads(args.manifest.read_text(encoding="utf-8"))
        result = apply_cleanup_plan(args.export_root.resolve(), plan, args.audit_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.output is None:
        parser.error("dry-run requires --output")

    plan = build_cleanup_plan(
        args.export_root.resolve(),
        active_version=args.active_version,
        rollback_versions=set(args.rollback_version),
        pinned_versions=set(args.pin_version),
        research_pinned_versions=set(args.research_pin_version),
        audit_pinned_versions=set(args.audit_pin_version),
        referenced_versions=set(args.referenced_version),
        staging_ttl_hours=args.staging_ttl_hours,
    )
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    plan["plan_sha256"] = _sha256(encoded)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
