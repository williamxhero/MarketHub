from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "deploy" / "bootstrap_database.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_database_for_timescale_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_all_fresh_bar_tables_use_selected_timescale_layout() -> None:
    schema = "\n".join(MODULE.BASE_SCHEMA_SQL).lower()
    for table_name in ("stock_bar_1m", "stock_bar_5m", "stock_bar_30m", "future_bar_1m"):
        assert f"if to_regclass('fact.{table_name}') is null" in schema
    assert "by_range('bar_time',interval '14 days')" in schema
    assert "timescaledb.segmentby='market,code'" in schema
    assert "timescaledb.orderby='bar_time asc'" in schema
    assert "after => interval '30 days'" in schema
    assert "call add_columnstore_policy" in schema
    assert "stock_bar_1m_code_time_idx" in schema
    assert "stock_bar_1m_time_idx" in schema
    assert "stock_bar_5m_code_time_idx" in schema
    assert "stock_bar_30m_code_time_idx" in schema
    assert "future_bar_1m_time_idx" in schema
    assert "migrate_data" not in schema


def test_local_database_repair_cannot_touch_a_remote_postgres(monkeypatch) -> None:
    monkeypatch.setattr(MODULE.shutil, "which", lambda _: "psql")
    assert MODULE._create_database_with_local_postgres_user(
        {"host": "db.example.invalid", "port": "5432", "dbname": "markethub", "user": "markethub", "password": "secret"}
    ) is False


def test_timescaledb_update_runs_as_first_statement_of_a_fresh_connection(monkeypatch) -> None:
    statements: list[list[str]] = []

    class Cursor:
        def __init__(self, connection_statements: list[str]) -> None:
            self._statements = connection_statements

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, statement: str) -> None:
            self._statements.append(statement)

        def fetchone(self):
            return ("2.27.2", "2.29.2")

    class Connection:
        def __init__(self) -> None:
            self._statements: list[str] = []
            statements.append(self._statements)

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def cursor(self) -> Cursor:
            return Cursor(self._statements)

    monkeypatch.setattr(MODULE, "_connect", lambda _config: Connection())

    MODULE._ensure_extension({"host": "127.0.0.1", "port": "5432", "dbname": "markethub", "user": "markethub", "password": "secret"})

    assert len(statements) == 3
    assert statements[2] == ["alter extension timescaledb update"]
