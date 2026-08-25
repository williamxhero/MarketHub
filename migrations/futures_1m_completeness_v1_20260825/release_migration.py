from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.futures_1m_completeness import bootstrap_futures_1m_completeness_schema, publish_validated_futures_1m_completeness_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap or publish immutable futures 1m completeness state.")
    parser.add_argument("action", choices=("bootstrap", "publish"))
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.action == "bootstrap":
        bootstrap_futures_1m_completeness_schema()
        result: dict[str, object] = {"status": "bootstrapped"}
    else:
        if args.manifest is None:
            raise SystemExit("publish requires --manifest")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise SystemExit("manifest root must be an object")
        result = publish_validated_futures_1m_completeness_manifest(manifest)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
