"""Immutable monthly partition planning and filesystem cleanup for stock_daily_1d.

This module deliberately contains no database or HTTP code.  The publisher supplies
source and coverage identities; this module decides whether an already accepted
partition may be reused and the maintenance command uses the same identity rules
when producing a cleanup manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DATASET_ID = "stock_daily_1d"
PARTITION_SCHEMA_VERSION = "markethub-stock-daily-partition-v1"
CLEANUP_SCHEMA_VERSION = "markethub-stock-daily-cleanup-v2"
CLEANUP_POLICY_VERSION = "stock-daily-retention-v1"
PARTITION_STATUSES = frozenset(
    {"unfrozen", "staging", "candidate", "frozen", "invalid", "rejected"}
)
CLEANUP_CLASSIFICATIONS = frozenset(
    {
        "active",
        "rollback",
        "research_pinned",
        "audit_pinned",
        "referenced_correct",
        "retained_correct",
        "expired_unreferenced",
        "failed_staging",
        "duplicate_old_correct",
        "invalid",
    }
)


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


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise RuntimeError(f"path escapes dataset root: {relative}")
    return candidate


def _file_evidence(root: Path, file_item: Any) -> dict[str, Any]:
    if not isinstance(file_item, dict):
        raise RuntimeError("partition file record is not an object")
    relative = str(file_item.get("path", ""))
    if not relative or Path(relative).is_absolute():
        raise RuntimeError("partition file path is invalid")
    path = _safe_child(root, relative)
    if not path.is_file():
        raise RuntimeError(f"partition file is missing: {path}")
    actual_sha = sha256_file(path)
    expected_sha = str(file_item.get("sha256", ""))
    actual_bytes = path.stat().st_size
    expected_bytes = int(file_item.get("bytes", -1))
    if actual_sha != expected_sha or actual_bytes != expected_bytes:
        raise RuntimeError(f"partition file identity mismatch: {path}")
    return {
        "path": relative,
        "sha256": actual_sha,
        "bytes": actual_bytes,
        "rows": int(file_item.get("rows", 0)),
    }


def _partition_evidence(root: Path, item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise RuntimeError("partition record is not an object")
    status = str(item.get("status", ""))
    if status not in PARTITION_STATUSES:
        raise RuntimeError(f"unsupported partition status: {status}")
    if status != "frozen":
        raise RuntimeError(f"partition is not accepted/frozen: {status}")
    required = ("partition_key", "schema_version", "source_sha256", "coverage_sha256", "rows")
    if any(not item.get(field) for field in required):
        raise RuntimeError("frozen partition identity is incomplete")
    files = [_file_evidence(root, file_item) for file_item in item.get("files", [])]
    if not files:
        raise RuntimeError("frozen partition has no files")
    return {
        "partition_key": str(item["partition_key"]),
        "schema_version": str(item["schema_version"]),
        "source_sha256": str(item["source_sha256"]),
        "coverage_sha256": str(item["coverage_sha256"]),
        "rows": int(item["rows"]),
        "files": files,
        "content_identity_sha256": _canonical_sha256(
            {
                "partition_key": item["partition_key"],
                "schema_version": item["schema_version"],
                "source_sha256": item["source_sha256"],
                "coverage_sha256": item["coverage_sha256"],
                "rows": int(item["rows"]),
                "files": files,
            }
        ),
    }


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
        root = Path(str(item["root"]))
        try:
            _partition_evidence(root, item)
        except (OSError, RuntimeError, ValueError):
            continue
        if item.get("status") == "frozen":
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
        storage_mode = "hardlink"
        try:
            target.unlink(missing_ok=True)
            target.hardlink_to(source)
        except OSError:
            storage_mode = "copy"
            shutil.copy2(source, target)
        result.append(
            dict(item, path=relative.as_posix(), storage_mode=storage_mode, source_path=str(source))
        )
    return result


def _entry_size(path: Path) -> int:
    return (
        sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        if path.is_dir()
        else path.stat().st_size
    )


def _load_manifest_entries(dataset_root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(dataset_root.glob("*/manifest.json")):
        version = path.parent.name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("dataset_id") != DATASET_ID:
                raise ValueError("dataset identity mismatch")
            entries.append(
                {"version": version, "path": str(path), "root": path.parent, "payload": payload}
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            entries.append(
                {
                    "version": version,
                    "path": str(path),
                    "root": path.parent,
                    "classification": "invalid",
                    "reason": str(exc),
                    "payload": None,
                }
            )
    return entries


def _staging_entries(dataset_root: Path, staging_ttl_hours: float) -> list[dict[str, Any]]:
    staging_root = dataset_root / ".staging"
    if not staging_root.is_dir():
        return []
    cutoff = datetime.now(UTC).timestamp() - staging_ttl_hours * 3600
    result = []
    for path in sorted(item for item in staging_root.iterdir() if item.is_dir()):
        old = path.stat().st_mtime < cutoff
        result.append(
            {
                "version": path.name,
                "path": str(path),
                "classification": "failed_staging",
                "reason": "staging has no published manifest"
                if old
                else "staging is inside retention window",
                "size_bytes": _entry_size(path),
                "expired": old,
            }
        )
    return result


def initialize_partition_catalog(
    export_root: Path, dataset_version: str, output: Path | None = None
) -> dict[str, Any]:
    """Create an idempotent, auditable catalog from an accepted publication."""
    dataset_root = (export_root / DATASET_ID).resolve()
    manifest_path = _safe_child(dataset_root, f"{dataset_version}/manifest.json")
    if not manifest_path.is_file():
        raise RuntimeError(f"published manifest does not exist: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("dataset_id") != DATASET_ID or payload.get("dataset_version") != dataset_version:
        raise RuntimeError("published manifest identity mismatch")
    partitions = []
    for item in payload.get("partitions", []):
        evidence = _partition_evidence(manifest_path.parent, item)
        partitions.append(
            {
                "partition_key": evidence["partition_key"],
                "status": "frozen",
                "schema_version": evidence["schema_version"],
                "source_sha256": evidence["source_sha256"],
                "coverage_sha256": evidence["coverage_sha256"],
                "rows": evidence["rows"],
                "content_identity_sha256": evidence["content_identity_sha256"],
                "source_lineage": item.get("source_lineage", {"dataset_version": dataset_version}),
                "frozen_at_utc": item.get("frozen_at_utc"),
                "files": evidence["files"],
            }
        )
    before = [
        {
            "path": str(manifest_path),
            "sha256": sha256_file(manifest_path),
            "bytes": manifest_path.stat().st_size,
        }
    ]
    after_inventory = [
        {
            "partition_key": partition["partition_key"],
            "files": partition["files"],
            "content_identity_sha256": partition["content_identity_sha256"],
        }
        for partition in partitions
    ]
    catalog = {
        "schema_version": "markethub-stock-daily-partition-catalog-v1",
        "policy_version": "stock-daily-partition-freeze-v1",
        "dataset_id": DATASET_ID,
        "dataset_version": dataset_version,
        "manifest_sha256": before[0]["sha256"],
        "before_inventory": before,
        "after_inventory": after_inventory,
        "partitions": partitions,
        "after_inventory_sha256": _canonical_sha256(after_inventory),
    }
    target = output or dataset_root / f"{dataset_version}.partition-catalog.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(catalog, ensure_ascii=False, indent=2, default=str) + "\n"
    if target.exists() and target.read_text(encoding="utf-8") != encoded:
        raise RuntimeError(f"partition catalog drift requires explicit correction: {target}")
    if not target.exists():
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, target)
    catalog["catalog_path"] = str(target)
    catalog["catalog_sha256"] = sha256_file(target)
    return catalog


def build_cleanup_plan(
    export_root: Path,
    *,
    active_version: str,
    rollback_versions: set[str] | None = None,
    pinned_versions: set[str] | None = None,
    research_pinned_versions: set[str] | None = None,
    audit_pinned_versions: set[str] | None = None,
    referenced_versions: set[str] | None = None,
    staging_ttl_hours: float = 24,
) -> dict[str, Any]:
    """Build a stable, conservative dry-run manifest from immutable files on disk."""
    rollback_versions = rollback_versions or set()
    pinned_versions = pinned_versions or set()
    research_pinned_versions = research_pinned_versions or set()
    audit_pinned_versions = audit_pinned_versions or set()
    referenced_versions = referenced_versions or set()
    dataset_root = (export_root / DATASET_ID).resolve()
    entries = _load_manifest_entries(dataset_root)
    protected = {
        active_version,
        *rollback_versions,
        *pinned_versions,
        *research_pinned_versions,
        *audit_pinned_versions,
        *referenced_versions,
    }
    inventory: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    actions: list[dict[str, Any]] = []

    for entry in entries:
        version = str(entry["version"])
        payload = entry.get("payload")
        base_class = (
            "active"
            if version == active_version
            else "rollback"
            if version in rollback_versions
            else "research_pinned"
            if version in research_pinned_versions
            else "audit_pinned"
            if version in audit_pinned_versions
            else "referenced_correct"
            if version in referenced_versions
            else "retained_correct"
        )
        if payload is None:
            inventory.append(
                {
                    "version": version,
                    "path": entry["path"],
                    "classification": "invalid",
                    "reason": entry.get("reason", "manifest unreadable"),
                    "evidence": {},
                }
            )
            actions.append(
                {
                    "action": "delete_invalid",
                    "old_version": version,
                    "partition_key": "manifest",
                    "reason": entry.get("reason", "manifest unreadable"),
                    "files": [],
                    "impact": [],
                }
            )
            continue
        partition_items = payload.get("partitions", [])
        has_explicit_invalid = any(
            isinstance(item, dict) and item.get("status") == "invalid"
            for item in partition_items
        )
        if has_explicit_invalid:
            base_class = "invalid"
        for item in partition_items:
            if not isinstance(item, dict):
                continue
            if item.get("status") == "invalid":
                actions.append(
                    {
                        "action": "delete_invalid",
                        "old_version": version,
                        "partition_key": item.get("partition_key", ""),
                        "reason": (
                            "partition is explicitly marked invalid with retained error evidence"
                        ),
                        "files": item.get("files", []),
                        "impact": item.get("impact", []),
                        "evidence": {
                            "status": "invalid",
                            "error": item.get("error"),
                            "source": item.get("source_lineage"),
                        },
                    }
                )
                continue
            try:
                evidence = _partition_evidence(entry["root"], item)
            except (OSError, RuntimeError, ValueError) as exc:
                inventory.append(
                    {
                        "version": version,
                        "path": entry["path"],
                        "classification": "invalid",
                        "reason": str(exc),
                        "evidence": {"partition_key": item.get("partition_key")},
                    }
                )
                continue
            grouped.setdefault(evidence["content_identity_sha256"], []).append(
                {"entry": entry, "item": item, "evidence": evidence}
            )
        inventory.append(
            {
                "version": version,
                "path": entry["path"],
                "classification": base_class,
                "reason": "protected release"
                if version in protected
                else "correct publication retained by default",
                "references": [{"kind": "manifest", "version": version}],
                "size_bytes": _entry_size(entry["root"]),
                "evidence": {
                    "published_at_utc": payload.get("published_at_utc"),
                    "partition_count": len(partition_items),
                },
            }
        )

    for identity, items in grouped.items():
        if len(items) < 2:
            continue
        ordered = sorted(
            items,
            key=lambda item: (
                str(item["entry"]["version"] in protected),
                str(item["entry"]["payload"].get("published_at_utc", "")),
            ),
            reverse=True,
        )
        keeper = ordered[0]
        for duplicate in ordered[1:]:
            old_version = str(duplicate["entry"]["version"])
            if old_version in protected:
                continue
            actions.append(
                {
                    "action": "delete_duplicate",
                    "old_version": old_version,
                    "keep_version": str(keeper["entry"]["version"]),
                    "partition_key": duplicate["evidence"]["partition_key"],
                    "reason": (
                        "all dataset, partition, schema, source/coverage, row and file "
                        "identities match"
                    ),
                    "files": duplicate["evidence"]["files"],
                    "evidence": {
                        "content_identity_sha256": identity,
                        "old": duplicate["evidence"],
                        "keep": keeper["evidence"],
                    },
                    "references": [
                        {
                            "from": old_version,
                            "to": str(keeper["entry"]["version"]),
                            "partition_key": duplicate["evidence"]["partition_key"],
                        }
                    ],
                    "estimated_bytes_reclaimed": sum(
                        int(item["bytes"]) for item in duplicate["evidence"]["files"]
                    ),
                }
            )

    for item in _staging_entries(dataset_root, staging_ttl_hours):
        inventory.append(item)
        if item["expired"]:
            actions.append(
                {
                    "action": "delete_failed_staging",
                    "old_path": item["path"],
                    "reason": "staging exceeded retention window",
                    "estimated_bytes_reclaimed": item["size_bytes"],
                }
            )

    duplicate_versions = {
        str(action["old_version"])
        for action in actions
        if action.get("action") == "delete_duplicate"
    }
    for item in inventory:
        if (
            item.get("version") in duplicate_versions
            and item.get("classification") == "retained_correct"
        ):
            item["classification"] = "duplicate_old_correct"
    inventory_sha = _canonical_sha256(inventory)
    return {
        "schema_version": CLEANUP_SCHEMA_VERSION,
        "policy_version": CLEANUP_POLICY_VERSION,
        "dataset_id": DATASET_ID,
        "active_version": active_version,
        "rollback_versions": sorted(rollback_versions),
        "pinned_versions": sorted(pinned_versions),
        "research_pinned_versions": sorted(research_pinned_versions),
        "audit_pinned_versions": sorted(audit_pinned_versions),
        "referenced_versions": sorted(referenced_versions),
        "staging_ttl_hours": staging_ttl_hours,
        "before_inventory_sha256": inventory_sha,
        "duplicates_detected": sum(
            action.get("action") == "delete_duplicate" for action in actions
        ),
        "inventory": inventory,
        "actions": actions,
        "mode": "dry-run",
    }


def _write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def apply_cleanup_plan(export_root: Path, plan: dict[str, Any], audit_dir: Path) -> dict[str, Any]:
    """Apply only a hash-verified dry-run plan, preserving idempotent audit evidence."""
    if plan.get("schema_version") != CLEANUP_SCHEMA_VERSION or plan.get("mode") != "dry-run":
        raise RuntimeError("cleanup apply requires an approved v2 dry-run manifest")
    expected_sha = plan.get("plan_sha256")
    if not expected_sha:
        raise RuntimeError("cleanup manifest approval hash is missing")
    unsigned = dict(plan)
    unsigned.pop("plan_sha256", None)
    encoded = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != expected_sha:
        raise RuntimeError("cleanup manifest hash mismatch")
    dataset_root = (export_root / DATASET_ID).resolve()
    protected_versions = {
        str(version)
        for version in (
            plan.get("active_version"),
            *plan.get("rollback_versions", []),
            *plan.get("pinned_versions", []),
            *plan.get("research_pinned_versions", []),
            *plan.get("audit_pinned_versions", []),
            *plan.get("referenced_versions", []),
        )
        if version
    }
    audit_dir.mkdir(parents=True, exist_ok=True)
    before: list[dict[str, Any]] = []
    migrations: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    deleted = 0
    bytes_deleted = 0

    for action in plan.get("actions", []):
        kind = action.get("action")
        if kind == "delete_failed_staging":
            path = Path(str(action["old_path"])).resolve()
            if path.exists() and path.parent == dataset_root / ".staging":
                for child in sorted(path.rglob("*"), reverse=True):
                    if child.is_file():
                        before.append(
                            {
                                "path": str(child),
                                "sha256": sha256_file(child),
                                "bytes": child.stat().st_size,
                            }
                        )
                        bytes_deleted += child.stat().st_size
                        child.unlink()
                    elif child.is_dir():
                        with suppress(OSError):
                            child.rmdir()
                with suppress(OSError):
                    path.rmdir()
            else:
                skipped.append({"action": kind, "path": str(path), "reason": "already absent"})
            continue
        if kind not in {"delete_duplicate", "delete_invalid"}:
            continue
        old_version = str(action.get("old_version", ""))
        if kind == "delete_duplicate" and old_version in protected_versions:
            raise RuntimeError(
                f"cleanup plan attempts to delete a protected version: {old_version}"
            )
        old_root = _safe_child(dataset_root, old_version)
        manifest_path = old_root / "manifest.json"
        if not manifest_path.is_file():
            skipped.append(
                {
                    "action": kind,
                    "old_version": old_version,
                    "partition_key": action.get("partition_key"),
                    "reason": "already applied or absent",
                }
            )
            continue
        if kind == "delete_invalid" and action.get("partition_key") == "manifest":
            before.append(
                {
                    "path": str(manifest_path),
                    "sha256": sha256_file(manifest_path),
                    "bytes": manifest_path.stat().st_size,
                }
            )
            bytes_deleted += manifest_path.stat().st_size
            manifest_path.unlink()
            deleted += 1
            tombstone = {
                "action": kind,
                "old_version": old_version,
                "partition_key": "manifest",
                "reason": action.get("reason"),
                "impact": action.get("impact", []),
                "files": [],
            }
            _write_json_atomic(
                audit_dir / f"tombstone-{old_version}-{_canonical_sha256(tombstone)[:16]}.json",
                tombstone,
            )
            migrations.append(
                {
                    "old_version": old_version,
                    "new_version": None,
                    "partition_key": "manifest",
                    "status": "invalidated_with_impact_report",
                    "impact": action.get("impact", []),
                }
            )
            continue
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        partitions = payload.get("partitions", [])
        partition_key_value = action.get("partition_key")
        selected = next(
            (
                item
                for item in partitions
                if isinstance(item, dict) and item.get("partition_key") == partition_key_value
            ),
            None,
        )
        if selected is None:
            skipped.append(
                {
                    "action": kind,
                    "old_version": old_version,
                    "partition_key": partition_key_value,
                    "reason": "already migrated",
                }
            )
            continue
        if kind == "delete_duplicate":
            keep_root = _safe_child(dataset_root, str(action["keep_version"]))
            keep_manifest = keep_root / "manifest.json"
            if not keep_manifest.is_file():
                raise RuntimeError(f"duplicate keeper manifest is missing: {keep_manifest}")
            keep_payload = json.loads(keep_manifest.read_text(encoding="utf-8"))
            keep_item = next(
                (
                    item
                    for item in keep_payload.get("partitions", [])
                    if isinstance(item, dict) and item.get("partition_key") == partition_key_value
                ),
                None,
            )
            if keep_item is None:
                raise RuntimeError("duplicate keeper partition is missing")
            old_evidence = _partition_evidence(old_root, selected)
            keep_evidence = _partition_evidence(keep_root, keep_item)
            if (
                old_evidence["content_identity_sha256"] != keep_evidence["content_identity_sha256"]
                or old_evidence["content_identity_sha256"]
                != action["evidence"]["content_identity_sha256"]
            ):
                raise RuntimeError("duplicate identity changed since dry-run")
        else:
            if selected.get("status") != "invalid":
                raise RuntimeError("invalid cleanup action no longer targets an invalid partition")
            old_evidence = {
                "files": [
                    _file_evidence(old_root, file_item) for file_item in selected.get("files", [])
                ]
            }
        for file_item in old_evidence.get("files", []):
            path = _safe_child(old_root, str(file_item["path"]))
            if path.is_file():
                before.append(
                    {"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size}
                )
                bytes_deleted += path.stat().st_size
                path.unlink()
                deleted += 1
        remaining = [item for item in partitions if item is not selected]
        tombstone = {
            "action": kind,
            "old_version": old_version,
            "keep_version": action.get("keep_version"),
            "partition_key": partition_key_value,
            "reason": action.get("reason"),
            "impact": action.get("impact", []),
            "files": old_evidence.get("files", []),
        }
        _write_json_atomic(
            audit_dir / f"tombstone-{old_version}-{_canonical_sha256(tombstone)[:16]}.json",
            tombstone,
        )
        if remaining:
            payload["partitions"] = remaining
            payload["retired_partitions"] = [*payload.get("retired_partitions", []), tombstone]
            _write_json_atomic(manifest_path, payload)
        else:
            manifest_path.unlink()
        migrations.append(
            {
                "old_version": old_version,
                "new_version": action.get("keep_version"),
                "partition_key": partition_key_value,
                "status": "reference_migrated"
                if kind == "delete_duplicate"
                else "invalidated_with_impact_report",
                "impact": action.get("impact", []),
            }
        )

    remaining_versions = sorted(path.parent.name for path in dataset_root.glob("*/manifest.json"))
    result = dict(
        plan,
        status="applied",
        applied_at_utc=datetime.now(UTC).isoformat(),
        duplicates_deleted=sum(item["status"] == "reference_migrated" for item in migrations),
        references_migrated=sum(item["status"] == "reference_migrated" for item in migrations),
        invalidated=sum(item["status"] == "invalidated_with_impact_report" for item in migrations),
        deleted=deleted,
        bytes_deleted=bytes_deleted,
        skipped=skipped,
        reference_migrations=migrations,
        before=before,
        after={
            "remaining_versions": remaining_versions,
            "inventory_sha256": _canonical_sha256(remaining_versions),
        },
    )
    _write_json_atomic(audit_dir / "cleanup-after.json", result)
    return result
