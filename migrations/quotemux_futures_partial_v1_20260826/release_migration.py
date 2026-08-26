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
from urllib.request import urlopen

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
    return values.get(key) or None


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


def _database_env_lines(prefix: str, user: str, password: str) -> tuple[str, ...]:
    return (
        f"{prefix}_HOST={_required('HOST')}",
        f"{prefix}_PORT={_required('PORT')}",
        f"{prefix}_NAME={_required('NAME')}",
        f"{prefix}_USER={user}",
        f"{prefix}_PASSWORD={password}",
    )


def _health_snapshot() -> dict[str, object]:
    url = os.getenv("MARKETHUB_HEALTH_URL", "http://127.0.0.1:8803/api/health")
    with urlopen(url, timeout=5) as response:  # nosec B310 -- operator-configured private health endpoint
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise RuntimeError("MarketHub health check did not return status=ok")
    return {"release": os.getenv("MARKETHUB_RELEASE", ""), "data_version": payload.get("data_version", "")}


def main() -> int:
    publisher_env = _env_path("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_PUBLISHER_ENV", _PUBLISHER_ENV)
    reader_env = _env_path("MARKETHUB_QUOTEMUX_PUBLIC_READER_ENV", _READER_ENV)
    publisher_secret = _secret_from_env_file(publisher_env, "QUOTEMUX_PUBLISH_DB_PASSWORD") or secrets.token_urlsafe(48)
    reader_secret = _secret_from_env_file(reader_env, "QUOTEMUX_READ_DB_PASSWORD") or secrets.token_urlsafe(48)

    # Persist generated secrets before changing PostgreSQL. A process crash can
    # therefore be resumed with the same credentials instead of rotating a role
    # to a value that was never recoverably stored. Rewrite every metadata key
    # atomically on retries, retaining an extant secret rather than leaving a
    # truncated or stale DSN that the strict QuoteMux clients will reject.
    _write_secret_env(publisher_env, _database_env_lines(
        "QUOTEMUX_PUBLISH_DB", "quotemux_futures_partial_publisher", publisher_secret,
    ))
    _write_secret_env(reader_env, _database_env_lines(
        "QUOTEMUX_READ_DB", "quotemux_public_reader", reader_secret,
    ))
    before = _health_snapshot()
    connection = _connect()
    try:
        apply_futures_partial_migration(connection_factory=lambda: connection)
        provision_futures_partial_roles(publisher_secret, reader_secret, connection_factory=lambda: connection)
    finally:
        connection.close()
    after = _health_snapshot()
    print(json.dumps({"status": "provisioned", "reader_env": str(reader_env), "publisher_env": str(publisher_env), "health_before": before, "health_after": after}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
