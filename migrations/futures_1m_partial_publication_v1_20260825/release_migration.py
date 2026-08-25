from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.futures_partial_publication import (
    bootstrap_futures_1m_partial_publication_schema,
    publish_futures_1m_partial_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap or publish immutable source-specific futures 1m partial data.")
    parser.add_argument("action", choices=("bootstrap", "publish"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--bundle-root", type=Path)
    args = parser.parse_args()
    if args.action == "bootstrap":
        bootstrap_futures_1m_partial_publication_schema()
        result: dict[str, object] = {"status": "bootstrapped"}
    else:
        if args.manifest is None or args.bundle_root is None:
            raise SystemExit("publish requires --manifest and --bundle-root")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise SystemExit("manifest root must be an object")
        result = publish_futures_1m_partial_manifest(manifest, args.bundle_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
