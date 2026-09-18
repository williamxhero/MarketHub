from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from scripts.publisher.stock_daily_partition_lifecycle import (
    PARTITION_SCHEMA_VERSION,
    apply_cleanup_plan,
    build_cleanup_plan,
    discover_reusable_partitions,
    initialize_partition_catalog,
    materialize_reused_partition,
    plan_partition,
)


def _manifest(
    root: Path, version: str, *, published: str, source: str = "s", coverage: str = "c"
) -> None:
    version_root = root / "stock_daily_1d" / version
    part = version_root / "year=2024" / "month=01"
    part.mkdir(parents=True)
    content = b"accepted partition bytes"
    bars = part / "bars.parquet"
    bars.write_bytes(content)
    import hashlib

    manifest = {
        "dataset_id": "stock_daily_1d",
        "dataset_version": version,
        "published_at_utc": published,
        "partitions": [
            {
                "start": "2024-01-01",
                "end_exclusive": "2024-02-01",
                "partition_key": "2024-01-01:2024-02-01",
                "schema_version": PARTITION_SCHEMA_VERSION,
                "status": "frozen",
                "rows": 1,
                "source_sha256": source,
                "coverage_sha256": coverage,
                "files": [
                    {
                        "path": "year=2024/month=01/bars.parquet",
                        "rows": 1,
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
            }
        ],
    }
    (version_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _approve(plan: dict) -> dict:
    unsigned = dict(plan)
    unsigned.pop("plan_sha256", None)
    encoded = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    plan["plan_sha256"] = hashlib.sha256(encoded).hexdigest()
    return plan


def test_frozen_partition_reuses_only_when_every_identity_matches() -> None:
    candidates = [
        {
            "partition_key": "2024-01-01:2024-02-01",
            "schema_version": PARTITION_SCHEMA_VERSION,
            "status": "frozen",
            "source_sha256": "s",
            "coverage_sha256": "c",
            "rows": 1,
        }
    ]
    assert (
        plan_partition(
            partition_key_value="2024-01-01:2024-02-01",
            source_sha256="s",
            coverage_sha256="c",
            rows=1,
            candidates=candidates,
        ).action
        == "reuse"
    )
    assert (
        plan_partition(
            partition_key_value="2024-01-01:2024-02-01",
            source_sha256="changed",
            coverage_sha256="c",
            rows=1,
            candidates=candidates,
        ).action
        == "fail_closed"
    )


def test_discovery_rejects_tampered_reusable_bytes(tmp_path: Path) -> None:
    version = "mhd-v1-" + "a" * 64
    _manifest(tmp_path, version, published="2026-09-01")
    assert len(discover_reusable_partitions(tmp_path / "stock_daily_1d", "mhd-v1-" + "b" * 64)) == 1
    (tmp_path / "stock_daily_1d" / version / "year=2024/month=01/bars.parquet").write_bytes(
        b"tampered"
    )
    assert discover_reusable_partitions(tmp_path / "stock_daily_1d", "mhd-v1-" + "b" * 64) == []


def test_cleanup_dry_run_and_apply_delete_only_unprotected_duplicate(tmp_path: Path) -> None:
    old = "mhd-v1-" + "a" * 64
    new = "mhd-v1-" + "b" * 64
    _manifest(tmp_path, old, published="2026-09-01")
    _manifest(tmp_path, new, published="2026-09-02")
    plan = build_cleanup_plan(tmp_path, active_version=new)
    assert len(plan["actions"]) == 1
    assert plan["actions"][0]["action"] == "delete_duplicate"
    assert plan["actions"][0]["estimated_bytes_reclaimed"] > 0
    assert plan["inventory"][0]["classification"] == "duplicate_old_correct"
    result = apply_cleanup_plan(tmp_path, _approve(plan), tmp_path / "audit")
    assert result["deleted"] == 1
    assert result["references_migrated"] == 1
    assert not (tmp_path / "stock_daily_1d" / old / "manifest.json").exists()
    assert (tmp_path / "stock_daily_1d" / new / "manifest.json").exists()
    repeated = apply_cleanup_plan(tmp_path, _approve(plan), tmp_path / "audit")
    assert repeated["deleted"] == 0
    assert repeated["skipped"]


def test_cleanup_plan_is_stable_and_describes_all_retention_inputs(tmp_path: Path) -> None:
    version = "mhd-v1-" + "c" * 64
    _manifest(tmp_path, version, published="2026-09-01")
    plan = build_cleanup_plan(
        tmp_path,
        active_version=version,
        rollback_versions={"rollback"},
        research_pinned_versions={"research"},
        audit_pinned_versions={"audit"},
        referenced_versions={"referenced"},
    )
    repeat = build_cleanup_plan(
        tmp_path,
        active_version=version,
        rollback_versions={"rollback"},
        research_pinned_versions={"research"},
        audit_pinned_versions={"audit"},
        referenced_versions={"referenced"},
    )
    assert plan["schema_version"] == "markethub-stock-daily-cleanup-v2"
    assert plan["before_inventory_sha256"] == repeat["before_inventory_sha256"]
    assert plan["inventory"][0]["classification"] == "active"
    assert {"policy_version", "before_inventory_sha256", "actions"} <= plan.keys()


def test_partition_catalog_initialization_is_idempotent_and_does_not_rewrite_manifest(
    tmp_path: Path,
) -> None:
    version = "mhd-v1-" + "d" * 64
    _manifest(tmp_path, version, published="2026-09-01")
    manifest = tmp_path / "stock_daily_1d" / version / "manifest.json"
    before = manifest.read_bytes()
    catalog = initialize_partition_catalog(tmp_path, version)
    assert catalog["schema_version"] == "markethub-stock-daily-partition-catalog-v1"
    assert catalog["partitions"][0]["content_identity_sha256"]
    assert catalog["after_inventory"]
    assert catalog["after_inventory_sha256"]
    assert manifest.read_bytes() == before
    second = initialize_partition_catalog(tmp_path, version)
    assert second["catalog_sha256"] == catalog["catalog_sha256"]


def test_invalid_partition_cleanup_writes_impact_tombstone(tmp_path: Path) -> None:
    version = "mhd-v1-" + "e" * 64
    root = tmp_path / "stock_daily_1d" / version
    part = root / "year=2024" / "month=01"
    part.mkdir(parents=True)
    content = b"invalid"
    path = part / "bars.parquet"
    path.write_bytes(content)
    manifest = {
        "dataset_id": "stock_daily_1d",
        "dataset_version": version,
        "partitions": [
            {
                "partition_key": "2024-01-01:2024-02-01",
                "status": "invalid",
                "error": "bad source",
                "impact": [{"consumer": "research-1"}],
                "files": [
                    {
                        "path": "year=2024/month=01/bars.parquet",
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
            }
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    plan = build_cleanup_plan(tmp_path, active_version="mhd-v1-" + "f" * 64)
    assert plan["actions"][0]["action"] == "delete_invalid"
    result = apply_cleanup_plan(tmp_path, _approve(plan), tmp_path / "audit")
    assert result["invalidated"] == 1
    assert not path.exists()
    assert list((tmp_path / "audit").glob("tombstone-*.json"))


def test_invalid_manifest_cleanup_is_idempotent(tmp_path: Path) -> None:
    version = "mhd-v1-" + "1" * 64
    manifest = tmp_path / "stock_daily_1d" / version / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("not json", encoding="utf-8")
    plan = build_cleanup_plan(tmp_path, active_version="mhd-v1-" + "2" * 64)
    assert plan["actions"][0]["partition_key"] == "manifest"
    result = apply_cleanup_plan(tmp_path, _approve(plan), tmp_path / "audit")
    assert result["invalidated"] == 1
    assert not manifest.exists()
    repeated = apply_cleanup_plan(tmp_path, _approve(plan), tmp_path / "audit")
    assert repeated["skipped"]


def test_correct_unique_partitions_are_retained_and_reuse_hardlinks(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source = source_root / "month" / "bars.parquet"
    source.parent.mkdir(parents=True)
    content = b"one correct partition"
    source.write_bytes(content)
    records = materialize_reused_partition(
        source_root=source_root,
        target_root=target_root,
        source_files=[
            {
                "path": "month/bars.parquet",
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    )
    target = target_root / "month" / "bars.parquet"
    assert target.read_bytes() == content
    assert records[0]["storage_mode"] in {"hardlink", "copy"}
    if records[0]["storage_mode"] == "hardlink":
        assert os.path.samefile(source, target)

    version = "mhd-v1-" + "3" * 64
    _manifest(tmp_path, version, published="2026-09-01", source="unique")
    plan = build_cleanup_plan(tmp_path, active_version="mhd-v1-" + "4" * 64)
    retained = next(item for item in plan["inventory"] if item["version"] == version)
    assert retained["classification"] == "retained_correct"
    assert plan["actions"] == []
