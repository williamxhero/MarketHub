"""Immutable monthly partition planning and filesystem cleanup for stock_daily_1d.

This module deliberately contains no database or HTTP code.  The publisher supplies
source and coverage identities; this module decides whether an already accepted
partition may be reused and the maintenance command uses the same identity rules
when producing a cleanup manifest.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATASET_ID = "stock_daily_1d"
PARTITION_SCHEMA_VERSION = "markethub-stock-daily-partition-v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def canonical_records_sha256(records: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(_canonical(record).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class PartitionPlan:
    action: str
    partition_key: str
    reason: str
    source: dict[str, Any] | None = None


def partition_key(start: Any, end_exclusive: Any) -> str:
    return f"{start}:{end_exclusive}"


def plan_partition(
    *,
    partition_key_value: str,
    source_sha256: str,
    coverage_sha256: str,
    rows: int,
    candidates: Iterable[dict[str, Any]],
) -> PartitionPlan:
    """Plan one month without ever silently replacing a frozen partition."""
    matching_key = [item for item in candidates if item.get("partition_key") == partition_key_value]
    for item in matching_key:
        identity_matches = (
            item.get("status") == "frozen"
            and item.get("schema_version") == PARTITION_SCHEMA_VERSION
            and item.get("source_sha256") == source_sha256
            and item.get("coverage_sha256") == coverage_sha256
            and int(item.get("rows", -1)) == rows
        )
        if identity_matches:
            return PartitionPlan(
                "reuse",
                partition_key_value,
                "accepted frozen content identity matches",
                item,
            )
    if matching_key:
        return PartitionPlan(
            "fail_closed",
            partition_key_value,
            "an accepted frozen partition changed content identity",
        )
    return PartitionPlan("build", partition_key_value, "no accepted partition exists")


def _manifest_candidates(dataset_root: Path, current_dataset_version: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for manifest_path in sorted(dataset_root.glob("*/manifest.json")):
        if manifest_path.parent.name in {current_dataset_version, ".staging"}:
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("dataset_id") != DATASET_ID:
            continue
        for partition in payload.get("partitions", []):
            if not isinstance(partition, dict):
                continue
            candidate = dict(partition)
            candidate["manifest_path"] = str(manifest_path)
            candidate["root"] = str(manifest_path.parent)
            candidates.append(candidate)
    return candidates


def discover_reusable_partitions(
    dataset_root: Path, current_dataset_version: str
) -> list[dict[str, Any]]:
    """Return only frozen partitions whose referenced files still verify."""
    candidates: list[dict[str, Any]] = []
    for item in _manifest_candidates(dataset_root, current_dataset_version):
        files = item.get("files", [])
        root = Path(str(item["root"]))
        valid = True
        for file_item in files:
            if not isinstance(file_item, dict):
                valid = False
                break
            path = root / str(file_item.get("path", ""))
            if not path.is_file() or sha256_file(path) != str(file_item.get("sha256", "")):
                valid = False
                break
        if valid and item.get("status") == "frozen":
            candidates.append(item)
    return candidates


def materialize_reused_partition(
    *,
    source_root: Path,
    target_root: Path,
    source_files: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Hard-link accepted files where possible; copy only across filesystems."""
    result: list[dict[str, Any]] = []
    for item in source_files:
        relative = Path(str(item["path"]))
        source = (source_root / relative).resolve()
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not source.is_file():
            raise RuntimeError(f"reusable partition file is missing: {source}")
        try:
            target.unlink(missing_ok=True)
            target.hardlink_to(source)
        except OSError:
            shutil.copy2(source, target)
        result.append(dict(item, path=relative.as_posix()))
    return result


def build_cleanup_plan(
    export_root: Path,
    *,
    active_version: str,
    rollback_versions: set[str] | None = None,
    pinned_versions: set[str] | None = None,
) -> dict[str, Any]:
    """Build a conservative dry-run plan from manifests already on disk."""
    rollback_versions = rollback_versions or set()
    pinned_versions = pinned_versions or set()
    dataset_root = export_root / DATASET_ID
    manifests: list[dict[str, Any]] = []
    for path in sorted(dataset_root.glob("*/manifest.json")):
        version = path.parent.name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("dataset_id") != DATASET_ID:
                raise ValueError("dataset identity mismatch")
            manifests.append({"version": version, "path": str(path), "payload": payload})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            manifests.append(
                {
                    "version": version,
                    "path": str(path),
                    "classification": "invalid",
                    "reason": str(exc),
                }
            )

    protected = {active_version, *rollback_versions, *pinned_versions}
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for entry in manifests:
        payload = entry.get("payload", {})
        for item in payload.get("partitions", []):
            if not isinstance(item, dict):
                continue
            key = (
                item.get("partition_key"), item.get("schema_version"), item.get("source_sha256"),
                item.get("coverage_sha256"), item.get("rows"),
                tuple(
                    sorted(
                        (
                            str(file.get("path", "")),
                            str(file.get("sha256", "")),
                            int(file.get("bytes", 0)),
                        )
                        for file in item.get("files", [])
                        if isinstance(file, dict)
                    )
                ),
            )
            grouped.setdefault(key, []).append({"manifest": entry, "partition": item})

    actions: list[dict[str, Any]] = []
    for entry in manifests:
        payload = entry.get("payload", {})
        invalid_partitions = [
            item for item in payload.get("partitions", [])
            if isinstance(item, dict) and item.get("status") == "invalid"
        ]
        if invalid_partitions:
            for item in invalid_partitions:
                actions.append({
                    "action": "delete_invalid",
                    "old_version": str(entry["version"]),
                    "partition_key": item.get("partition_key", ""),
                    "reason": "partition is explicitly marked invalid with retained error evidence",
                    "files": item.get("files", []),
                    "impact": item.get("impact", []),
                })
    for key, items in grouped.items():
        if not key[0] or not key[2] or not key[3]:
            continue
        ordered = sorted(
            items,
            key=lambda item: str(item["manifest"]["payload"].get("published_at_utc", "")),
            reverse=True,
        )
        keeper = ordered[0]
        for duplicate in ordered[1:]:
            old_version = str(duplicate["manifest"]["version"])
            if old_version in protected:
                continue
            actions.append({
                "action": "delete_duplicate",
                "old_version": old_version,
                "keep_version": str(keeper["manifest"]["version"]),
                "partition_key": key[0],
                "reason": "same dataset, schema, source/coverage identity and row count",
                "files": duplicate["partition"].get("files", []),
            })
    return {
        "schema_version": "markethub-stock-daily-cleanup-v1",
        "dataset_id": DATASET_ID,
        "active_version": active_version,
        "rollback_versions": sorted(rollback_versions),
        "pinned_versions": sorted(pinned_versions),
        "duplicates_detected": sum(
            1 for item in actions if item.get("action") == "delete_duplicate"
        ),
        "inventory": [
            {
                "version": entry["version"],
                "path": entry["path"],
                "classification": (
                    "active" if entry["version"] == active_version else
                    "rollback" if entry["version"] in rollback_versions else
                    "research_pinned" if entry["version"] in pinned_versions else
                    entry.get("classification", "retained_correct")
                ),
            }
            for entry in manifests
        ],
        "actions": actions,
    }


def apply_cleanup_plan(export_root: Path, plan: dict[str, Any], audit_dir: Path) -> dict[str, Any]:
    """Apply only a previously generated plan and write before/after evidence."""
    if plan.get("schema_version") != "markethub-stock-daily-cleanup-v1":
        raise RuntimeError("unsupported cleanup manifest")
    expected_sha = plan.get("plan_sha256")
    if expected_sha:
        unsigned = dict(plan)
        unsigned.pop("plan_sha256", None)
        encoded = json.dumps(
            unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != expected_sha:
            raise RuntimeError("cleanup manifest hash mismatch")
    dataset_root = (export_root / DATASET_ID).resolve()
    audit_dir.mkdir(parents=True, exist_ok=True)
    before = []
    deleted = 0
    bytes_deleted = 0
    reference_migrations = []
    for action in plan.get("actions", []):
        if action.get("action") not in {"delete_duplicate", "delete_invalid"}:
            continue
        old_root = (dataset_root / str(action["old_version"])).resolve()
        if old_root.parent != dataset_root or not old_root.is_dir():
            continue
        reference_migrations.append({
            "old_version": action["old_version"],
            "new_version": action.get("keep_version"),
            "partition_key": action["partition_key"],
            "status": (
                "invalidated_with_impact_report"
                if action.get("action") == "delete_invalid"
                else "retired_duplicate_manifest"
            ),
            "impact": action.get("impact", []),
        })
        for file_item in action.get("files", []):
            path = (old_root / str(file_item.get("path", ""))).resolve()
            if (path.parent == old_root or old_root in path.parents) and path.is_file():
                before.append(
                    {"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size}
                )
                bytes_deleted += path.stat().st_size
                path.unlink()
                deleted += 1
        for directory in sorted(
            (item for item in old_root.rglob("*") if item.is_dir()), reverse=True
        ):
            with suppress(OSError):
                directory.rmdir()
        manifest = old_root / "manifest.json"
        if manifest.is_file():
            manifest.unlink()
        with suppress(OSError):
            old_root.rmdir()
    after = {
        "remaining_versions": sorted(
            path.parent.name for path in dataset_root.glob("*/manifest.json")
        )
    }
    result = dict(
        plan,
        status="applied",
        duplicates_deleted=len(reference_migrations),
        references_migrated=0,
        invalidated=sum(
            1 for entry in plan.get("inventory", []) if entry.get("classification") == "invalid"
        ),
        deleted=deleted,
        bytes_deleted=bytes_deleted,
        reference_migrations=reference_migrations,
        before=before,
        after=after,
    )
    (audit_dir / "cleanup-after.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result
