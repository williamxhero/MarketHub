from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "migrations" / "query_read_v3_20260823"


def test_query_read_v3_migration_is_versioned_generic_and_reuses_formal_tools() -> None:
    manifest = json.loads((MIGRATION / "manifest.json").read_text(encoding="utf-8"))
    readme = (MIGRATION / "README.md").read_text(encoding="utf-8")
    deploy = (MIGRATION / "deploy_and_migrate_remote.ps1").read_text(encoding="utf-8")
    migration = (MIGRATION / "release_migration.py").read_text(encoding="utf-8")

    assert manifest["migration_id"] == "markethub-query-read-v3-20260823"
    assert manifest["requires_environment_discovery"] is True
    assert "discover_environment.py" in deploy
    assert "manage-formal-export-freeze.sh" in deploy
    assert "freeze '$freezeOwner'" in deploy
    assert "acquire '$freezeOwner'" not in deploy
    assert "ResumeFreezeOwner" in deploy
    assert 'Where-Object { $_.Trim() -ceq "lease=$freezeOwner" }' in deploy
    assert "拒绝接管" in deploy
    assert "deploy_yosef_server.ps1" in deploy
    assert "if ($migrationSucceeded)" in deploy
    assert "保持启用" in deploy
    assert "build_current_stock_daily_coverage" in migration
    assert "build_stock_1m_daily_coverage" in migration
    assert "daily_coverage\"].get(\"complete\")" in migration
    assert "yosef-server" not in deploy.lower()
    assert "手工" in readme and "幂等" in readme


def test_ai_readme_routes_upgrade_threads_to_query_read_migration() -> None:
    content = (ROOT / "AIREADME.md").read_text(encoding="utf-8")
    assert "migrations/query_read_v3_20260823/README.md" in content
    assert "-PreflightOnly" in content
