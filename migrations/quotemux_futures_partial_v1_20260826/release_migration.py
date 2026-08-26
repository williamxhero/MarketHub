"""Privileged entrypoint for the QuoteMux-owned S000012 partial migration.

This wrapper deliberately contains no MarketHub DDL, data publication, or
secret values. Operators supply its privileged connection through the
out-of-repository environment file; the public API never imports this module.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import tempfile
from typing import Any

import psycopg
from psycopg.rows import dict_row

from quotemux.store.futures_partial_migration import (
    apply_futures_partial_migration,
    provision_futures_partial_roles,
)


_PREFIX = "MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_"
_PUBLISHER_ENV = Path("/data/markethub/env/quotemux-futures-partial-publisher.env")
_READER_ENV = Path("/data/markethub/env/quotemux-public-reader.env")


def _required(name: str) -> str:
    value = os.getenv(_PREFIX + name, "").strip()
    if value == "":
        raise RuntimeError(f"{_PREFIX}{name} is required")
    return value


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=_required("HOST"),
        port=int(_required("PORT")),
        dbname=_required("NAME"),
        user=_required("USER"),
        password=_required("PASSWORD"),
        connect_timeout=10,
        row_factory=dict_row,
        application_name="markethub-quotemux-futures-partial-migration",
    )


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name, "").strip()
    return Path(value) if value else default


def _secret_from_env_file(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    values = dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    value = values.get(key, "")
    if value == "":
        raise RuntimeError(f"existing {path} lacks {key}; refusing to rotate an unknown credential")
    return value


def _write_secret_env(path: Path, lines: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    publisher_env = _env_path("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_PUBLISHER_ENV", _PUBLISHER_ENV)
    reader_env = _env_path("MARKETHUB_QUOTEMUX_PUBLIC_READER_ENV", _READER_ENV)
    publisher_secret = _secret_from_env_file(publisher_env, "QUOTEMUX_FUTURES_PARTIAL_PUBLISHER_DB_PASSWORD") or secrets.token_urlsafe(48)
    reader_secret = _secret_from_env_file(reader_env, "QUOTEMUX_READ_DB_PASSWORD") or secrets.token_urlsafe(48)

    # Persist generated secrets before changing PostgreSQL. A process crash can
    # therefore be resumed with the same credentials instead of rotating a role
    # to a value that was never recoverably stored. Secrets are never printed.
    if not publisher_env.exists():
        _write_secret_env(publisher_env, (
            "QUOTEMUX_FUTURES_PARTIAL_PUBLISHER_DB_USER=quotemux_futures_partial_publisher",
            f"QUOTEMUX_FUTURES_PARTIAL_PUBLISHER_DB_PASSWORD={publisher_secret}",
        ))
    if not reader_env.exists():
        _write_secret_env(reader_env, (
            "QUOTEMUX_READ_DB_USER=quotemux_public_reader",
            f"QUOTEMUX_READ_DB_PASSWORD={reader_secret}",
        ))
    connection = _connect()
    try:
        apply_futures_partial_migration(connection_factory=lambda: connection)
        provision_futures_partial_roles(publisher_secret, reader_secret, connection_factory=lambda: connection)
    finally:
        connection.close()
    print(json.dumps({"status": "provisioned", "reader_env": str(reader_env), "publisher_env": str(publisher_env)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
