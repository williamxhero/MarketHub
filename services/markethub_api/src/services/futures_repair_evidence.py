from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any


_REGISTRY_ID = re.compile(r"[a-z0-9][a-z0-9._-]{2,127}$")
_SHA256 = re.compile(r"[0-9a-f]{64}$")


def _managed_root() -> Path:
    runtime = Path(os.getenv("MARKETHUB_RUNTIME_ROOT", Path(__file__).resolve().parents[4] / "runtime"))
    return Path(os.getenv("MARKETHUB_MANAGED_EVIDENCE_ROOT", runtime / "managed-evidence")).resolve() / "futures-back-adjusted"


def _checked_registry_id(registry_id: str) -> str:
    value = registry_id.strip()
    if not _REGISTRY_ID.fullmatch(value):
        raise ValueError("repair_registry_id must be 3-128 lowercase letters, digits, dots, underscores, or hyphens")
    return value


def _canonical_manifest(manifest: Mapping[str, object], artifact: bytes) -> bytes:
    payload = dict(manifest)
    digest = hashlib.sha256(artifact).hexdigest()
    if str(payload.get("staged_artifact_sha256", "")) != digest:
        raise ValueError("derivation manifest staged_artifact_sha256 does not match artifact bytes")
    if payload.get("schema_version") != "futures_back_adjusted_1m_derivation_v1":
        raise ValueError("unsupported derivation manifest schema_version")
    if payload.get("series_type") != "back_adjusted_continuous":
        raise ValueError("derivation manifest series_type must be back_adjusted_continuous")
    if str(payload.get("frozen_dataset_version", "")).strip() == "":
        raise ValueError("derivation manifest requires frozen_dataset_version")
    for field in ("ruleset_sha256", "gap_ranges_artifact_sha256"):
        if not _SHA256.fullmatch(str(payload.get(field, ""))):
            raise ValueError(f"derivation manifest requires {field}")
    for field in ("source_capture", "contract_mapping_capture"):
        if not isinstance(payload.get(field), Mapping) or not payload[field]:
            raise ValueError(f"derivation manifest requires {field}")
    if not isinstance(payload.get("exact_missing_keys"), list) or not payload["exact_missing_keys"]:
        raise ValueError("derivation manifest requires exact_missing_keys")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ManagedBackAdjustedRepairEvidenceRegistry:
    """Server-owned immutable evidence bundles; no API input can choose a file path."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or _managed_root()).resolve()

    def register(self, registry_id: str, artifact: bytes, derivation_manifest: Mapping[str, object]) -> dict[str, object]:
        identifier = _checked_registry_id(registry_id)
        if not isinstance(artifact, bytes) or artifact == b"":
            raise ValueError("artifact must be non-empty immutable bytes")
        canonical_manifest = _canonical_manifest(derivation_manifest, artifact)
        target = self._root / identifier
        self._root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            current_artifact, current_manifest = self.resolve(identifier)
            if current_artifact == artifact and _canonical_manifest(current_manifest, current_artifact) == canonical_manifest:
                return {"registry_id": identifier, "artifact_sha256": hashlib.sha256(artifact).hexdigest(), "idempotent": True}
            raise RuntimeError(f"repair evidence registry_id is immutable: {identifier}")
        temporary = Path(tempfile.mkdtemp(prefix=f".{identifier}.", dir=self._root))
        try:
            self._atomic_write(temporary / "artifact.bin", artifact)
            self._atomic_write(temporary / "derivation_manifest.json", canonical_manifest)
            try:
                os.replace(temporary, target)
            except FileExistsError:
                current_artifact, current_manifest = self.resolve(identifier)
                if current_artifact == artifact and _canonical_manifest(current_manifest, current_artifact) == canonical_manifest:
                    return {"registry_id": identifier, "artifact_sha256": hashlib.sha256(artifact).hexdigest(), "idempotent": True}
                raise RuntimeError(f"repair evidence registry_id is immutable: {identifier}")
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return {"registry_id": identifier, "artifact_sha256": hashlib.sha256(artifact).hexdigest(), "idempotent": False}

    def resolve(self, registry_id: str) -> tuple[bytes, dict[str, object]]:
        identifier = _checked_registry_id(registry_id)
        bundle = self._root / identifier
        artifact_path = bundle / "artifact.bin"
        manifest_path = bundle / "derivation_manifest.json"
        if not artifact_path.is_file() or not manifest_path.is_file():
            raise KeyError(f"unknown managed repair evidence: {identifier}")
        artifact = artifact_path.read_bytes()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"managed repair evidence manifest is invalid: {identifier}") from exc
        if not isinstance(manifest, dict):
            raise RuntimeError(f"managed repair evidence manifest is invalid: {identifier}")
        # Revalidate each resolution so disk corruption or a manual mutation never reaches QuoteMux.
        _canonical_manifest(manifest, artifact)
        return artifact, manifest

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
