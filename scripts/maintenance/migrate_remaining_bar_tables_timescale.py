from __future__ import annotations

"""Compatibility entry point for the versioned storage-v2 migration core."""

import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[2] / "migrations" / "storage_v2_20260823" / "timescale_tables.py"
SPEC = importlib.util.spec_from_file_location("markethub_storage_v2_timescale_tables", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load migration core: {SCRIPT}")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

for name in dir(MODULE):
    if not name.startswith("__"):
        globals()[name] = getattr(MODULE, name)


if __name__ == "__main__":
    raise SystemExit(MODULE.main())
