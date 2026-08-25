from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import pytest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from services.futures_repair_evidence import ManagedBackAdjustedRepairEvidenceRegistry


def _manifest(artifact: bytes) -> dict[str, object]:
    return {
        "schema_version": "futures_back_adjusted_1m_derivation_v1",
        "series_type": "back_adjusted_continuous",
        "frozen_dataset_version": "mhd-v1-frozen",
        "staged_artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "ruleset_sha256": "a" * 64,
        "gap_ranges_artifact_sha256": "b" * 64,
        "source_capture": {"capture_id": "source-001"},
        "contract_mapping_capture": {"capture_id": "mapping-001"},
        "exact_missing_keys": [{"product_code": "ag", "bar_time": "2026-02-02 10:46:00"}],
    }


def test_registry_persists_verified_immutable_artifact_and_manifest(tmp_path: Path) -> None:
    artifact = b"authoritative-staged-bytes"
    registry = ManagedBackAdjustedRepairEvidenceRegistry(tmp_path / "managed")

    first = registry.register("ag-repair-001", artifact, _manifest(artifact))
    second = registry.register("ag-repair-001", artifact, _manifest(artifact))
    restored_artifact, restored_manifest = registry.resolve("ag-repair-001")

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert restored_artifact == artifact
    assert restored_manifest == _manifest(artifact)
    assert (tmp_path / "managed" / "ag-repair-001" / "artifact.bin").is_file()
    assert not list((tmp_path / "managed").glob(".ag-repair-001.*"))


def test_registry_rejects_hash_mismatch_and_registry_mutation(tmp_path: Path) -> None:
    artifact = b"authoritative-staged-bytes"
    registry = ManagedBackAdjustedRepairEvidenceRegistry(tmp_path / "managed")
    with pytest.raises(ValueError, match="staged_artifact_sha256"):
        registry.register("ag-repair-001", artifact, {**_manifest(artifact), "staged_artifact_sha256": "0" * 64})
    registry.register("ag-repair-001", artifact, _manifest(artifact))
    with pytest.raises(RuntimeError, match="immutable"):
        registry.register("ag-repair-001", b"different", _manifest(b"different"))


@pytest.mark.parametrize("registry_id", ("../escape", "UPPER", "ab", "bad/path"))
def test_registry_rejects_ids_that_could_select_a_path(tmp_path: Path, registry_id: str) -> None:
    artifact = b"authoritative-staged-bytes"
    with pytest.raises(ValueError, match="repair_registry_id"):
        ManagedBackAdjustedRepairEvidenceRegistry(tmp_path / "managed").register(registry_id, artifact, _manifest(artifact))


def test_registry_resolution_rechecks_disk_hash(tmp_path: Path) -> None:
    artifact = b"authoritative-staged-bytes"
    root = tmp_path / "managed"
    registry = ManagedBackAdjustedRepairEvidenceRegistry(root)
    registry.register("ag-repair-001", artifact, _manifest(artifact))
    (root / "ag-repair-001" / "artifact.bin").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="staged_artifact_sha256"):
        registry.resolve("ag-repair-001")
