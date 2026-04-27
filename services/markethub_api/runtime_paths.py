from __future__ import annotations

import sys
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOTS = [SERVICE_ROOT / "src"]


def configure_python_path() -> None:
    for path in reversed(SOURCE_ROOTS):
        path_text = str(path)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)
