from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.futures_repair_evidence import ManagedBackAdjustedRepairEvidenceRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description="Register a server-managed immutable back-adjusted repair evidence bundle.")
    parser.add_argument("--registry-id", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--derivation-manifest", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.derivation_manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise SystemExit("derivation manifest root must be an object")
    result = ManagedBackAdjustedRepairEvidenceRegistry().register(args.registry_id, args.artifact.read_bytes(), manifest)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
