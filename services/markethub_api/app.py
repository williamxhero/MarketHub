from __future__ import annotations

from runtime_paths import configure_python_path


configure_python_path()

from core.config import HOST, PORT
from main import app


if __name__ == "__main__":
    import os
    import uvicorn

    workers = max(1, int(os.getenv("MHK_UVICORN_WORKERS", "1")))
    uvicorn.run(app if workers == 1 else "main:app", host=HOST, port=PORT, workers=workers)
