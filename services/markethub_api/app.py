from __future__ import annotations

from runtime_paths import configure_python_path


configure_python_path()

from core.config import HOST, PORT
from main import app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
