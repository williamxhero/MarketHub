from __future__ import annotations

"""Discover an existing MarketHub deployment without exposing its secrets."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
try:
    import pwd
except ImportError:  # pragma: no cover - the discovery command itself runs on Linux
    pwd = None  # type: ignore[assignment]
import shutil
import socket
import subprocess
import sys
from typing import Iterable


TABLES = (
    "fact.stock_bar_1m",
    "fact.stock_bar_5m",
    "fact.stock_bar_30m",
    "fact.future_bar_1m",
)


def _run(arguments: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(arguments, capture_output=True, text=True, env=env, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(arguments, 127, "", str(exc))


def _read_env(path: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if path is None or not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def _running_service_env(properties: dict[str, str]) -> dict[str, str]:
    try:
        main_pid = int(properties.get("MainPID", "0"))
    except ValueError:
        return {}
    environ_path = Path(f"/proc/{main_pid}/environ")
    if main_pid <= 0 or not environ_path.is_file():
        return {}
    try:
        entries = environ_path.read_bytes().split(b"\0")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for entry in entries:
        if b"=" not in entry:
            continue
        name, value = entry.split(b"=", 1)
        key = name.decode("utf-8", errors="replace")
        if key.startswith(("MARKETHUB_", "QUOTEMUX_")):
            values[key] = value.decode("utf-8", errors="replace")
    return values


def _systemd_properties(service_name: str) -> dict[str, str]:
    result = _run(
        [
            "systemctl",
            "show",
            f"{service_name}.service",
            "--property=LoadState,ActiveState,MainPID,User,Group,WorkingDirectory,EnvironmentFiles,ExecStart,FragmentPath",
        ]
    )
    if result.returncode != 0:
        return {}
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            name, value = line.split("=", 1)
            properties[name] = value
    return properties if properties.get("LoadState") == "loaded" else {}


def _service_candidates(hint: str | None) -> Iterable[str]:
    if hint:
        yield hint.removesuffix(".service")
    result = _run(["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"])
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            name = line.split(maxsplit=1)[0].removesuffix(".service") if line.strip() else ""
            if "markethub" in name.lower():
                yield name


def _discover_service(hint: str | None) -> tuple[str, dict[str, str]]:
    seen: set[str] = set()
    for candidate in _service_candidates(hint):
        if candidate in seen:
            continue
        seen.add(candidate)
        properties = _systemd_properties(candidate)
        if properties:
            return candidate, properties
    return hint or "markethub-api", {}


def _environment_file(properties: dict[str, str], hint: str | None) -> Path | None:
    for token in properties.get("EnvironmentFiles", "").split():
        candidate = Path(token.lstrip("-").split("(", 1)[0])
        if candidate.is_file():
            return candidate
    if hint and Path(hint).is_file():
        return Path(hint)
    if hint:
        return Path(hint)
    return None


def _infer_app_root(properties: dict[str, str], hint: str | None) -> Path:
    working = properties.get("WorkingDirectory", "")
    marker = "/current/"
    if marker in working:
        return Path(working.split(marker, 1)[0])
    return Path(hint or "/data/MarketHub2")


def _infer_runtime_root(properties: dict[str, str], env_values: dict[str, str], hint: str | None) -> Path:
    configured = env_values.get("MARKETHUB_RUNTIME_ROOT")
    if configured:
        return Path(configured)
    executable = properties.get("ExecStart", "")
    marker = "/.venv/bin/python"
    if marker in executable:
        prefix = executable.split(marker, 1)[0].split()[-1].lstrip("{")
        return Path(prefix)
    return Path(hint or "/data/markethub")


def _infer_package_venv_root(runtime_root: Path, env_values: dict[str, str]) -> tuple[Path, int]:
    configured = Path(env_values["QUOTEMUX_PACKAGE_VENV_ROOT"]) if env_values.get("QUOTEMUX_PACKAGE_VENV_ROOT") else None
    candidates: list[Path] = []
    if configured is not None:
        candidates.append(configured)
    parent = runtime_root / "package_venvs"
    if parent.is_dir():
        candidates.extend(path for path in parent.iterdir() if path.is_dir())
    unique = list(dict.fromkeys(candidates))
    if not unique:
        return parent / "storage-v2.0.0-timescale-parquet-arrow", 0
    scored = [(sum(1 for _ in candidate.glob("*/.quotemux-installed.json")), candidate) for candidate in unique]
    score, selected = max(scored, key=lambda item: (item[0], item[1] == configured))
    return selected, score


def _default_service_user(properties: dict[str, str], app_root: Path) -> str:
    configured = properties.get("User")
    if configured:
        return configured
    if pwd is not None and os.getuid() != 0:
        return pwd.getpwuid(os.getuid()).pw_name
    if pwd is not None and app_root.exists():
        try:
            return pwd.getpwuid(app_root.stat().st_uid).pw_name
        except KeyError:
            pass
    candidates = [] if pwd is None else [entry for entry in pwd.getpwall() if entry.pw_uid >= 1000 and entry.pw_shell not in ("/usr/sbin/nologin", "/bin/false")]
    if candidates:
        return sorted(candidates, key=lambda item: item.pw_uid)[0].pw_name
    if os.environ.get("SUDO_USER"):
        return os.environ["SUDO_USER"]
    return pwd.getpwuid(os.getuid()).pw_name if pwd is not None else os.environ.get("USERNAME", "markethub")


def _psql(env_values: dict[str, str], sql: str) -> subprocess.CompletedProcess[str]:
    required = ("MARKETHUB_DB_HOST", "MARKETHUB_DB_PORT", "MARKETHUB_DB_NAME", "MARKETHUB_DB_USER")
    if not all(env_values.get(name) for name in required) or not shutil.which("psql"):
        return subprocess.CompletedProcess(["psql"], 127, "", "database environment or psql is unavailable")
    environment = os.environ.copy()
    environment["PGPASSWORD"] = env_values.get("MARKETHUB_DB_PASSWORD", "")
    arguments = [
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        env_values["MARKETHUB_DB_HOST"],
        "-p",
        env_values["MARKETHUB_DB_PORT"],
        "-U",
        env_values["MARKETHUB_DB_USER"],
        "-d",
        env_values["MARKETHUB_DB_NAME"],
        "-At",
        "-F",
        "\t",
        "-c",
        sql,
    ]
    return _run(arguments, env=environment)


def _local_postgresql_cluster() -> dict[str, object] | None:
    result = _run(["pg_lsclusters", "--json"])
    if result.returncode != 0:
        return None
    try:
        clusters = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(clusters, list) or not clusters:
        return None
    cluster = max(clusters, key=lambda item: (int(bool(item.get("running"))), int(item.get("version", 0))))
    return {
        "major": int(cluster["version"]),
        "cluster": str(cluster["cluster"]),
        "port": int(cluster["port"]),
        "data_directory": str(cluster["pgdata"]),
        "running": bool(cluster.get("running")),
    }


def _database_details(env_values: dict[str, str]) -> tuple[dict[str, object], list[dict[str, object]]]:
    cluster = _local_postgresql_cluster()
    database_is_local = env_values.get("MARKETHUB_DB_HOST", "") in ("", "127.0.0.1", "localhost", "::1")
    installed_timescaledb_version = None
    if cluster:
        control = Path(f"/usr/share/postgresql/{cluster['major']}/extension/timescaledb.control")
        if control.is_file():
            for line in control.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("default_version") and "=" in line:
                    installed_timescaledb_version = line.split("=", 1)[1].strip().strip("'").strip('"')
                    break
    details: dict[str, object] = {
        "reachable": False,
        "postgresql_version": None,
        "postgresql_major": cluster["major"] if cluster else None,
        "data_directory": cluster["data_directory"] if cluster and database_is_local else None,
        "timescaledb_version": None,
        "installed_timescaledb_version": installed_timescaledb_version,
        "local_cluster": cluster,
        "connection_error": None,
    }
    version = _psql(
        env_values,
        "SELECT current_setting('server_version'), current_setting('server_version_num'), COALESCE((SELECT extversion FROM pg_extension WHERE extname='timescaledb'), '');",
    )
    if version.returncode != 0 or not version.stdout.strip():
        details["connection_error"] = version.stderr.strip().splitlines()[-1] if version.stderr.strip() else "psql connection failed"
        return details, [{"name": table, "present": False, "hypertable": False, "total_bytes": 0} for table in TABLES]
    fields = version.stdout.strip().split("\t")
    details.update(
        {
            "reachable": True,
            "postgresql_version": fields[0],
            "postgresql_major": int(fields[1]) // 10000,
            "data_directory": cluster["data_directory"] if cluster and database_is_local else None,
            "timescaledb_version": fields[2] or None,
            "connection_error": None,
        }
    )
    values = ",".join("('%s')" % table for table in TABLES)
    table_query = _psql(
        env_values,
        "WITH wanted(name) AS (VALUES "
        + values
        + ") SELECT name, to_regclass(name) IS NOT NULL, EXISTS (SELECT 1 FROM timescaledb_information.hypertables h WHERE h.hypertable_schema=split_part(name,'.',1) AND h.hypertable_name=split_part(name,'.',2)), COALESCE(pg_total_relation_size(to_regclass(name)), 0) FROM wanted ORDER BY name;",
    )
    if table_query.returncode != 0:
        return details, [{"name": table, "present": False, "hypertable": False, "total_bytes": 0} for table in TABLES]
    tables = []
    for line in table_query.stdout.splitlines():
        name, present, hypertable, total_bytes = line.split("\t")
        tables.append({"name": name, "present": present == "t", "hypertable": hypertable == "t", "total_bytes": int(total_bytes)})
    return details, tables


def _storage_state(tables: list[dict[str, object]], database_reachable: bool) -> str:
    if not database_reachable:
        return "unknown"
    present = sum(bool(table["present"]) for table in tables)
    hypertables = sum(bool(table["hypertable"]) for table in tables)
    if present == 0:
        return "fresh"
    if present == len(TABLES) and hypertables == 0:
        return "storage-v1-postgresql-ordinary-bars"
    if present == len(TABLES) and hypertables == len(TABLES):
        return "storage-v2.0.0-timescale-parquet-arrow"
    return "intermediate-resumable"


def discover(args: argparse.Namespace) -> dict[str, object]:
    service_name, properties = _discover_service(args.service_name)
    env_path = _environment_file(properties, args.env_path)
    env_values = _read_env(env_path)
    env_values.update(_running_service_env(properties))
    app_root = _infer_app_root(properties, args.app_root)
    runtime_root = _infer_runtime_root(properties, env_values, args.runtime_root)
    package_venv_root, ready_package_environments = _infer_package_venv_root(runtime_root, env_values)
    service_user = _default_service_user(properties, app_root)
    current = app_root / "current"
    current_release = None
    if current.exists():
        try:
            current_release = str(current.resolve(strict=True))
        except OSError:
            current_release = None
    incomplete_release = any(
        (candidate / "MarketHub" / "migrations" / "storage_v2_20260823" / "manifest.json").is_file()
        for candidate in (app_root / "releases").glob("*_storage_v2")
    ) if (app_root / "releases").is_dir() else False
    health_host = env_values.get("MARKETHUB_HOST", "127.0.0.1")
    if health_host in ("0.0.0.0", "::"):
        health_host = "127.0.0.1"
    health_url = args.health_url or f"http://{health_host}:{env_values.get('MARKETHUB_PORT', '8803')}/api/health"
    database, tables = _database_details(env_values)
    state = _storage_state(tables, bool(database["reachable"]))
    ordinary_bytes = sum(int(table.get("total_bytes", 0)) for table in tables if table["present"] and not table["hypertable"])
    minimum_free_bytes = int(ordinary_bytes * 1.2) + (10 * 1024**3 if ordinary_bytes else 0)
    existing = bool(properties or current_release or (env_path and env_path.is_file()))
    mode = "existing" if existing else "fresh-install"
    if existing and current_release is None and incomplete_release:
        mode = "incomplete-storage-v2-install"
    database_host = env_values.get("MARKETHUB_DB_HOST", "")
    database_is_local = database_host in ("", "127.0.0.1", "localhost", "::1")
    database_data_path = Path(str(database["data_directory"])) if database["reachable"] and database["data_directory"] else None
    migration_volume_verified = bool(database_is_local and database_data_path and database_data_path.exists())
    disk_path = database_data_path if migration_volume_verified else (app_root if app_root.exists() else app_root.parent)
    while not disk_path.exists() and disk_path != disk_path.parent:
        disk_path = disk_path.parent
    disk = shutil.disk_usage(disk_path)
    warnings: list[str] = []
    if existing and not database["reachable"]:
        warnings.append("existing deployment database could not be inspected")
    if database["reachable"] and not migration_volume_verified:
        warnings.append("database storage volume is remote or unavailable; verify its free space separately")
    if properties and properties.get("ActiveState") != "active":
        warnings.append(f"service is {properties.get('ActiveState', 'unknown')}")
    return {
        "schema_version": 1,
        "discovered_at_utc": datetime.now(timezone.utc).isoformat(),
        "host": {"hostname": socket.gethostname(), "platform": sys.platform},
        "deployment": {
            "mode": mode,
            "app_root": str(app_root),
            "runtime_root": str(runtime_root),
            "package_venv_root": str(package_venv_root),
            "ready_package_environments": ready_package_environments,
            "env_path": str(env_path) if env_path else str(Path(args.env_path or runtime_root / "env/markethub.env")),
            "service_name": service_name,
            "service_user": service_user,
            "service_active_state": properties.get("ActiveState", "not-installed"),
            "current_release": current_release,
            "health_url": health_url,
        },
        "database": database,
        "storage": {
            "detected_version": state,
            "tables": tables,
            "ordinary_relation_bytes": ordinary_bytes,
            "minimum_free_bytes_for_migration": minimum_free_bytes,
        },
        "filesystem": {
            "probe_path": str(disk_path),
            "migration_volume_verified": migration_volume_verified,
            "total_bytes": disk.total,
            "free_bytes": disk.free,
        },
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root")
    parser.add_argument("--runtime-root")
    parser.add_argument("--env-path")
    parser.add_argument("--service-name")
    parser.add_argument("--health-url")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = discover(args)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, args.output)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
