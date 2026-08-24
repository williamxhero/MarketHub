from __future__ import annotations

from pathlib import Path


DEPLOY_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "local" / "deploy_yosef_server.ps1"


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
