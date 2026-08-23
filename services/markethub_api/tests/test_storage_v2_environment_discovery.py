from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[3] / "migrations" / "storage_v2_20260823" / "discover_environment.py"
SPEC = importlib.util.spec_from_file_location("storage_v2_environment_discovery", SCRIPT)
assert SPEC and SPEC.loader
discovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discovery)

ENV_SYNC_SCRIPT = SCRIPT.parent / "sync_runtime_env.py"
ENV_SYNC_SPEC = importlib.util.spec_from_file_location("storage_v2_sync_runtime_env", ENV_SYNC_SCRIPT)
assert ENV_SYNC_SPEC and ENV_SYNC_SPEC.loader
env_sync = importlib.util.module_from_spec(ENV_SYNC_SPEC)
ENV_SYNC_SPEC.loader.exec_module(env_sync)


def test_storage_state_recognizes_all_supported_transitions() -> None:
    ordinary = [{"present": True, "hypertable": False} for _ in discovery.TABLES]
    hypertables = [{"present": True, "hypertable": True} for _ in discovery.TABLES]
    absent = [{"present": False, "hypertable": False} for _ in discovery.TABLES]
    mixed = ordinary.copy()
    mixed[0] = {"present": True, "hypertable": True}

    assert discovery._storage_state(ordinary, True) == "storage-v1-postgresql-ordinary-bars"
    assert discovery._storage_state(hypertables, True) == "storage-v2.0.0-timescale-parquet-arrow"
    assert discovery._storage_state(absent, True) == "fresh"
    assert discovery._storage_state(mixed, True) == "intermediate-resumable"
    assert discovery._storage_state(absent, False) == "unknown"


def test_discovery_uses_real_unit_and_env_without_exposing_password(tmp_path, monkeypatch) -> None:
    app_root = tmp_path / "custom-app"
    (app_root / "current").mkdir(parents=True)
    runtime_root = tmp_path / "custom-runtime"
    env_path = tmp_path / "custom.env"
    env_path.write_text(
        "MARKETHUB_RUNTIME_ROOT=" + str(runtime_root) + "\n"
        "MARKETHUB_HOST=0.0.0.0\n"
        "MARKETHUB_PORT=9911\n"
        "MARKETHUB_DB_PASSWORD=top-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        discovery,
        "_discover_service",
        lambda hint: (
            "custom-market-api",
            {
                "ActiveState": "active",
                "User": "alice",
                "WorkingDirectory": (app_root / "current" / "MarketHub" / "services" / "markethub_api").as_posix(),
                "EnvironmentFiles": str(env_path),
            },
        ),
    )
    monkeypatch.setattr(
        discovery,
        "_database_details",
        lambda values: (
            {
                "reachable": True,
                "postgresql_version": "18.1",
                "postgresql_major": 18,
                "data_directory": "/var/lib/postgresql/18/main",
                "timescaledb_version": "2.27.2",
            },
            [{"name": name, "present": True, "hypertable": True} for name in discovery.TABLES],
        ),
    )
    args = argparse.Namespace(app_root="/wrong", runtime_root="/wrong", env_path="/wrong", service_name=None, health_url=None, output=None)

    payload = discovery.discover(args)

    assert payload["deployment"]["app_root"] == str(app_root)
    assert payload["deployment"]["runtime_root"] == str(runtime_root)
    assert payload["deployment"]["env_path"] == str(env_path)
    assert payload["deployment"]["service_name"] == "custom-market-api"
    assert payload["deployment"]["service_user"] == "alice"
    assert payload["deployment"]["package_venv_root"] == str(runtime_root / "package_venvs" / "storage-v2.0.0-timescale-parquet-arrow")
    assert payload["deployment"]["health_url"] == "http://127.0.0.1:9911/api/health"
    assert payload["storage"]["detected_version"] == "storage-v2.0.0-timescale-parquet-arrow"
    assert "top-secret" not in repr(payload)


def test_remote_migration_discovers_environment_before_deploying() -> None:
    migration_root = SCRIPT.parent
    content = (migration_root / "deploy_and_migrate_yosef.ps1").read_text(encoding="utf-8")
    generic_deploy = (SCRIPT.parents[2] / "scripts" / "local" / "deploy_yosef_server.ps1").read_text(encoding="utf-8")

    assert content.index("discover_environment.py") < content.index("deploy_yosef_server.ps1")
    assert "preflight.json" in content
    assert "PreflightOnly" in content
    assert "base64 --decode --ignore-garbage | sudo" in content
    assert "inspect-before-apply.json" in content
    assert "base64 --decode" in content
    assert "bash '$migrationRoot/cleanup_after_migration.sh'" in content
    assert "-ServiceUser $ServiceUser" in content
    assert "User=yosef" not in generic_deploy
    assert "yosef:yosef" not in generic_deploy
    assert "User=$service_user" in generic_deploy
    assert "base64 --decode" in generic_deploy
    assert "find \"$release_root/MarketHub\" -type f -name '*.sh' -exec chmod 0755 {} +" in generic_deploy
    assert "sync_runtime_env.py" in generic_deploy
    assert generic_deploy.index("sync_runtime_env.py") < generic_deploy.index(". \"$env_path\"")
    assert "'$HealthUrl'" in generic_deploy
    governance = (SCRIPT.parents[2] / "scripts" / "maintenance" / "storage-governance.sh").read_text(encoding="utf-8")
    assert 'systemctl show "$SERVICE_NAME.service"' in governance
    assert 'for venv in "$package_venv_root"/*' in governance
    assert "不是 package_venvs 的直接子目录" in governance


def test_package_installer_uses_the_discovered_runtime_root() -> None:
    installer = (SCRIPT.parents[2] / "scripts" / "deploy" / "install_all_packages.py").read_text(encoding="utf-8")
    assert 'os.getenv("QUOTEMUX_PACKAGE_VENV_ROOT"' in installer
    assert "find_spec('playwright')" in installer


def test_release_installer_rewrites_release_scoped_env_without_losing_secrets(tmp_path: Path) -> None:
    env_path = tmp_path / "markethub.env"
    env_path.write_text(
        "# keep this comment\n"
        "MARKETHUB_DB_PASSWORD=top-secret\n"
        "QUOTEMUX_PACKAGE_REPO_SPEC=/old/release/QuoteMux_Packages\n"
        "QUOTEMUX_PACKAGE_VENV_ROOT=/old/runtime/package_venvs/old-release\n"
        "CUSTOM_SETTING=preserved\n",
        encoding="utf-8",
    )

    env_sync.sync_runtime_env(
        env_path=env_path,
        app_root=tmp_path / "app",
        runtime_root=tmp_path / "runtime",
        release_root=tmp_path / "app" / "releases" / "new-release",
        package_venv_root=tmp_path / "runtime" / "package_venvs" / "new-release",
    )

    content = env_path.read_text(encoding="utf-8")
    assert "MARKETHUB_DB_PASSWORD=top-secret" in content
    assert "CUSTOM_SETTING=preserved" in content
    assert "# keep this comment" in content
    assert "QUOTEMUX_PACKAGE_REPO_SPEC=" + str(tmp_path / "app" / "releases" / "new-release" / "QuoteMux_Packages") in content
    assert "QUOTEMUX_PACKAGE_VENV_ROOT=" + str(tmp_path / "runtime" / "package_venvs" / "new-release") in content
    assert "/old/" not in content

    installer = (SCRIPT.parent / "install_release_linux.sh").read_text(encoding="utf-8")
    assert "sync_runtime_env.py" in installer
