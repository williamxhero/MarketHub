from __future__ import annotations

import os
from pathlib import Path


def _get_host() -> str:
    return os.getenv("MARKETHUB_HOST", "0.0.0.0")


def _get_port() -> int:
    text = os.getenv("MARKETHUB_PORT", "8803")
    try:
        return int(text)
    except ValueError:
        return 8803


HOST = _get_host()
PORT = _get_port()

SERVER_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SERVER_ROOT.parent
DOCS_ROOT = PROJECT_ROOT / "docs"
INTEGRATION_DOCS_ROOT = DOCS_ROOT / "integration-api"
STATIC_INDEX_PATH = SERVER_ROOT / "static" / "index.html"
STATIC_FAVICON_PATH = SERVER_ROOT / "static" / "favicon.svg"

def _get_datalake_root() -> Path:
    path_text = os.getenv("DATALAKE_ROOT", "")
    if path_text:
        return Path(path_text)
    return Path("C:/STOCKS/datalake")


DATALAKE_ROOT = _get_datalake_root()


def _get_db_port() -> int:
    text = os.getenv("DL_DB_PORT", "55432")
    try:
        return int(text)
    except ValueError:
        return 55432


DL_DB_HOST = os.getenv("DL_DB_HOST", "localhost")
DL_DB_PORT = _get_db_port()
DL_DB_NAME = os.getenv("DL_DB_NAME", "datalake_dev")
DL_DB_USER = os.getenv("DL_DB_USER", "datalake")
DL_DB_PASSWORD = os.getenv("DL_DB_PASSWORD", "datalake_dev_password")
DL_DB_CONNECT_TIMEOUT = 3

DATE_FORMAT = "%Y%m%d"
DATETIME_FORMAT = "%Y%m%d %H:%M:%S"

DEFAULT_LIMIT = 200
MAX_LIMIT = 5000

TS_TOKEN = os.getenv("TS_TOKEN", "") or os.getenv("DL_TS_TOKEN", "")
