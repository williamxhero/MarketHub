from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.futures_1m_completeness import (
    activate_futures_1m_completeness_revision,
    bootstrap_futures_1m_completeness_schema,
    publish_validated_futures_1m_completeness_manifest,
    publish_validated_futures_1m_completeness_revision,
    restore_legacy_futures_1m_completeness,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap or publish immutable futures 1m completeness state.")
    parser.add_argument("action", choices=("bootstrap", "publish", "publish-revision", "activate-revision", "restore-legacy"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--dataset-version")
    parser.add_argument("--revision-sha256")
    args = parser.parse_args()
    if args.action == "bootstrap":
        bootstrap_futures_1m_completeness_schema()
        result: dict[str, object] = {"status": "bootstrapped"}
    elif args.action in ("publish", "publish-revision"):
        if args.manifest is None:
            raise SystemExit("publish requires --manifest")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise SystemExit("manifest root must be an object")
        result = (publish_validated_futures_1m_completeness_manifest(manifest)
                  if args.action == "publish" else publish_validated_futures_1m_completeness_revision(manifest))
    elif args.action == "activate-revision":
        if not args.dataset_version or not args.revision_sha256:
            raise SystemExit("activate-revision requires --dataset-version and --revision-sha256")
        result = activate_futures_1m_completeness_revision(args.dataset_version, args.revision_sha256)
    else:
        if not args.dataset_version:
            raise SystemExit("restore-legacy requires --dataset-version")
        result = restore_legacy_futures_1m_completeness(args.dataset_version)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
