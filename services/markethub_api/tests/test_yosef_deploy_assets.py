from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


DEPLOY_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "local" / "deploy_yosef_server.ps1"
PARTIAL_RUNBOOK = Path(__file__).resolve().parents[3] / "scripts" / "local" / "run_quotemux_futures_partial_release.ps1"


def _quotemux_source_root() -> Path:
    spec = importlib.util.find_spec("quotemux")
    if spec is None or spec.origin is None:
        pytest.skip("QuoteMux is not installed; CLI contract test requires the release dependency")
    return Path(spec.origin).resolve().parents[1]


def test_deploy_installs_health_gated_parquet_publisher() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert 'mkdir -p "$runtime_root/scripts" "$runtime_root/publisher"' in source
    assert (
        'install -m 0755 "$remote_root/current/MarketHub/scripts/publisher/publish_stock_daily_parquet.py" '
        '"$runtime_root/publisher/publish_stock_daily_parquet.py"'
    ) in source


def test_deploy_refreshes_root_owned_storage_governance_entrypoint() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert (
        'sudo -n install -m 0755 "$remote_root/current/MarketHub/scripts/maintenance/storage-governance.sh" '
        "/usr/local/sbin/markethub-storage-governance"
    ) in source


def test_deploy_restores_previous_current_release_when_new_health_check_fails() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert 'previous_current="$(readlink -f "$remote_root/current" 2>/dev/null || true)"' in source
    assert 'if [ "$current_switched" = 1 ] && [ -n "$previous_current" ]; then' in source
    assert 'curl -fsS "$health_url" >/dev/null' in source
    assert "'$ServiceUser' '$HealthUrl'" in source
    assert 'shared_backup="/tmp/${service_name}-${release_name}.shared.bak"' in source
    assert 'rm -rf "$runtime_root/scripts" "$runtime_root/publisher"' in source
    assert 'markethub-storage-governance; else sudo -n rm -f' in source


def test_deploy_accepts_a_pinned_quotemux_worktree_and_records_release_inputs() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "[string]$QuoteMuxSourceRoot" in source
    assert "Get-PinnedCommit" in source
    assert '"部署输入仓库不能有已跟踪的未提交改动' in source
    assert 'Invoke-NativeCommand -FilePath "git" -Arguments @("-C", $source.Path, "archive"' in source
    assert 'Copy-Item -LiteralPath $quoteMuxRoot' not in source
    assert 'release-inputs.json' in source
    assert "'$marketHubCommit' '$quoteMuxCommit' '$quoteMuxPackagesCommit'" in source


def test_partial_runbook_has_explicit_stages_and_deploy_never_publishes_data() -> None:
    source = PARTIAL_RUNBOOK.read_text(encoding="utf-8")

    assert 'ValidateSet("deploy", "migrate", "classify", "import", "partial-plan", "partial-publish", "verify")' in source
    assert 'staged migration/role provisioning 已完成，未执行数据 import 或 partial publish。' in source
    assert 'quotemux-futures-partial-publisher.env' in source
    assert 'quotemux-futures-partial-migration.env' in source
    assert 'RemoteEnvPath = "/data/markethub/env/markethub.env"' in source


def test_deploy_keeps_old_service_alive_until_migration_creates_required_reader_env() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert 'MARKETHUB_HEALTH_URL="$health_url" "$runtime_root/.venv/bin/python" "$release_root/MarketHub/migrations/quotemux_futures_partial_v1_20260826/release_migration.py"' in source
    assert 'EnvironmentFile=$reader_env_path' in source
    assert 'EnvironmentFile=-$reader_env_path' not in source
    assert source.index('release_migration.py') < source.index('systemctl stop "$service_name.service"')
    assert 'api_base="${health_url%/api/health}"' in source
    assert 'json.load(sys.stdin).get("data_version")' in source
    assert 'urllib.parse.quote(value, safe="")' in source
    assert '/api/stocks/quotes?code=600000&freq=1d&count=1&data_version=$stock_data_version' in source
    assert 'strict futures readiness expected HTTP 409' in source


def test_runner_arguments_match_real_quotemux_cli_contract() -> None:
    environment = os.environ | {"PYTHONPATH": str(_quotemux_source_root())}
    importer = subprocess.run(
        [sys.executable, "-m", "quotemux.store.futures_pyramid_import", "--help"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    partial = subprocess.run(
        [sys.executable, "-m", "quotemux.store.futures_partial_publication", "--help"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert "--bundle BUNDLE" in importer.stdout
    assert "--plan PLAN" in importer.stdout
    assert "--plan PLAN" in partial.stdout
    assert "--qmi-id" in partial.stdout
    assert "--catalog-identity" in partial.stdout
    assert "--manifest" not in importer.stdout + partial.stdout


def test_stock_smoke_uses_the_health_discovered_required_data_version() -> None:
    from main import app

    parameters = app.openapi()["paths"]["/api/stocks/quotes"]["get"]["parameters"]
    data_version = next(parameter for parameter in parameters if parameter["name"] == "data_version")

    assert data_version["required"] is True
    assert data_version["schema"]["minLength"] == 1
