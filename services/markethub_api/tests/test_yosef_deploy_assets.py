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

    assert 'ValidateSet("deploy", "classify", "import", "partial-plan", "partial-publish", "verify")' in source
    assert 'staged migration/role provisioning 已完成，未执行数据 import 或 partial publish。' in source
    assert 'quotemux-futures-partial-publisher.env' in source
    assert 'quotemux-futures-partial-migration.env' in source
    assert 'RemoteEnvPath = "/data/markethub/env/markethub.env"' in source
    assert '"migrate" {' not in source


def test_deploy_drains_then_stops_before_migration_creates_required_reader_env() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    capture_drain = source.index('capture_drain_deadline=')
    controlled_stop = source.index('if [ "$service_stopped" != 1 ]; then', capture_drain)
    migration_run = source.index('run_privileged_migration\n#')
    switch = source.index('ln -sfn "$release_root" "$remote_root/current.next"')

    assert 'run_privileged_migration() {' in source
    assert capture_drain < controlled_stop < migration_run < switch
    assert 'MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_SKIP_HEALTH_SNAPSHOT=1' in source
    assert 'EnvironmentFile=$reader_env_path' in source
    assert 'EnvironmentFile=-$reader_env_path' not in source
    assert 'api_base="${health_url%/api/health}"' in source
    assert 'json.load(sys.stdin).get("data_version")' in source
    assert 'urllib.parse.quote(value, safe="")' in source
    assert '/api/stocks/quotes?code=600000&freq=1d&count=1&data_version=$stock_data_version' in source
    assert 'strict futures readiness expected HTTP 409' in source


def test_authorized_capture_stop_skips_old_service_health_check_before_migration() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    old_health_check = source.index('curl -fsS "$health_url" >/dev/null\n  if ! sudo -n systemctl stop')
    migration_run = source.index('run_privileged_migration\n#')

    assert 'if [ "$service_stopped" != 1 ]; then\n  curl -fsS "$health_url" >/dev/null' in source
    assert old_health_check < migration_run


def test_deploy_peer_migration_never_requires_or_persists_admin_password() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert '[ValidateSet("peer", "env")][string]$PrivilegedMigrationMode = "peer"' in source
    assert 'sudo -n -u postgres true' in source
    assert 'MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_PEER=1' in source
    assert 'MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_SOCKET_DIR=/var/run/postgresql' in source
    assert 'MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_HOST="$MARKETHUB_DB_HOST"' in source
    assert 'MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_PUBLISHER_ENV="$publisher_stage"' in source
    assert 'MARKETHUB_QUOTEMUX_PUBLIC_READER_ENV="$reader_stage"' in source
    assert 'cp -a "$release_root/QuoteMux/src/quotemux" "$migration_stage/code/quotemux"' in source
    assert 'install -m 0644 "$release_root/MarketHub/migrations/quotemux_futures_partial_v1_20260826/release_migration.py" "$migration_stage/code/release_migration.py"' in source
    assert 'PYTHONPATH="$migration_stage/code"' in source
    assert '"$runtime_root/.venv/bin/python" "$migration_stage/code/release_migration.py"' in source
    assert 'install -o "$service_user" -g "$service_group" -m 0600 "$publisher_stage" "$publisher_target_stage"' in source
    assert 'mv -Tf "$reader_target_stage" "$reader_env_path"' in source
    assert 'test "$(stat -c %U "$reader_env_path")" = "$service_user"' in source


def test_deploy_waits_for_live_capture_locks_without_overriding_them() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert '[ValidateRange(30, 1800)][int]$CaptureDrainTimeoutSeconds = 300' in source
    assert '[ValidateRange(1, 60)][int]$CaptureDrainRetrySeconds = 10' in source
    assert 'capture_drain_deadline=$((SECONDS + capture_drain_timeout_seconds))' in source
    assert 'if [ "$capture_reconcile_status" != 20 ]; then' in source
    assert 'active QuoteMux capture locks did not drain within ${capture_drain_timeout_seconds}s; keeping old release active' in source
    assert 'sleep "$capture_drain_retry_seconds"' in source
    assert 'return "$capture_reconcile_status"' not in source
    assert 'reconcile_capture_once' not in source
    assert source.count('from quotemux.store import reconcile_stale_capture_runs') == 2
    assert source.count('if [ "$capture_reconcile_status" = 0 ]; then') == 2
    assert '${capture_drain_deadline:=$SECONDS}' in source
    assert '${capture_post_stop_deadline:=$((SECONDS + capture_drain_timeout_seconds))}' in source
    default_capture_gate = source[source.index('capture_drain_deadline='):source.index('if [ "$allow_capture_drain_service_stop" != 1 ]; then')]
    assert 'systemctl kill' not in default_capture_gate
    assert 'systemctl stop' not in default_capture_gate


def test_deploy_requires_explicit_authorization_before_stopping_for_capture_drain() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    runner = PARTIAL_RUNBOOK.read_text(encoding="utf-8")

    assert '[switch]$AllowCaptureDrainServiceStop' in source
    assert '$captureDrainServiceStopFlag = if ($AllowCaptureDrainServiceStop) { "1" } else { "0" }' in source
    assert 'if [ "$allow_capture_drain_service_stop" != 1 ]; then' in source
    assert 'operator-authorized controlled service stop begins' in source
    assert 'if ! sudo -n systemctl stop "$service_name.service"; then' in source
    assert 'capture locks persisted after controlled service stop; restoring old release' in source
    maintenance_gate = source[source.index('capture drain timeout reached'):source.index('run_privileged_migration\n#')]
    assert 'systemctl kill' not in maintenance_gate
    assert 'pg_terminate' not in source
    assert '[switch]$AllowCaptureDrainServiceStop' in runner
    assert '-AllowCaptureDrainServiceStop:$AllowCaptureDrainServiceStop' in runner


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
