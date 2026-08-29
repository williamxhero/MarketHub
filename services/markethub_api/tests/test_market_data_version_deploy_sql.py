from __future__ import annotations

from pathlib import Path


SQL = Path(__file__).resolve().parents[3] / "scripts" / "deploy" / "market-data-version-state.sql"


def test_ddl_version_trigger_ignores_timescale_internal_and_shadow_ddl() -> None:
    content = SQL.read_text(encoding="utf-8")

    assert "pg_event_trigger_ddl_commands()" in content
    assert "_timescaledb_internal" in content
    assert "_timescaledb_catalog" in content
    assert "_(ts_shadow|ts_shadow_failed|legacy)" in content
    assert "if not should_bump" in content


def test_ddl_version_trigger_ignores_session_temporary_schema_aliases() -> None:
    content = SQL.read_text(encoding="utf-8")

    # PostgreSQL reports CREATE TEMP TABLE through the pg_temp alias on some
    # versions, rather than the physical pg_temp_<backend-id> schema name.
    assert "'pg_temp'" in content
    assert "'pg_toast_temp'" in content
    assert "not like 'pg_temp_%'" in content
    assert "not like 'pg_toast_temp_%'" in content
