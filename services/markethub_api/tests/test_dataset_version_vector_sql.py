from __future__ import annotations

from pathlib import Path

from services.dataset_versions import DATASET_IDS


SQL = Path(__file__).resolve().parents[3] / "scripts" / "deploy" / "dataset-version-vector.sql"
BOOTSTRAP = Path(__file__).resolve().parents[3] / "scripts" / "deploy" / "bootstrap_database.py"


def test_dataset_vector_sql_covers_every_registered_dataset_and_uses_statement_triggers() -> None:
    content = SQL.read_text(encoding="utf-8")

    for dataset_id in DATASET_IDS:
        assert f"'{dataset_id}'" in content
    assert "for each statement execute function audit.bump_dataset_versions()" in content
    assert "readmodel.stock_daily_coverage_day" in content
    assert "readmodel.stock_daily_coverage_gap" in content
    assert "readmodel.stock_bar_1m_daily_coverage" in content
    assert "'future_contract_reference','ref','future_contract_catalog_publication'" in content
    assert "future_contract_catalog_snapshot_item" not in content
    assert "for each row" not in content.lower()


def test_database_bootstrap_installs_dataset_vector_after_quotemux_schema() -> None:
    content = BOOTSTRAP.read_text(encoding="utf-8")
    assert content.index("_ensure_quotemux_schema()") < content.index("_ensure_dataset_version_vector(database_config)")
    assert 'SCRIPT_ROOT / "dataset-version-vector.sql"' in content
    assert "_ensure_dataset_version_vector_with_admin" in content
    assert "grant select,insert,update,delete on all tables in schema readmodel" in content
    assert "sudo" in content and '"-u", "postgres"' in content
