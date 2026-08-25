from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = ROOT / "migrations" / "calendar_freeze_20260825" / "release_migration.py"
SPEC = importlib.util.spec_from_file_location("calendar_freeze_release_migration", MIGRATION_PATH)
assert SPEC is not None and SPEC.loader is not None
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


def test_calendar_snapshot_reproduces_original_incomplete_fact_failure() -> None:
    rows = [
        {"trade_date": date(2012, 1, 1), "is_open": False},
        {"trade_date": date(2012, 1, 4), "is_open": True},
    ]

    with pytest.raises(RuntimeError, match="facts are incomplete"):
        migration.canonical_snapshot(rows, start=date(2012, 1, 1), end=date(2012, 1, 4))


def test_calendar_snapshot_accepts_persisted_closed_days_and_is_deterministic() -> None:
    rows = [
        {"trade_date": date(2012, 1, 1), "is_open": False},
        {"trade_date": date(2012, 1, 2), "is_open": False},
        {"trade_date": date(2012, 1, 3), "is_open": False},
        {"trade_date": date(2012, 1, 4), "is_open": True},
    ]

    first = migration.canonical_snapshot(rows, start=date(2012, 1, 1), end=date(2012, 1, 4))
    second = migration.canonical_snapshot(reversed(rows), start=date(2012, 1, 1), end=date(2012, 1, 4))

    assert first == second
    assert first[0] == "cee282ceafaa6f93d69b8a7bb2b60a4b736144062faf261f7158cbdc5ddd6c3f"


def test_psycopg_bulk_insert_uses_a_cursor() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "with connection.cursor() as cursor:" in source
    assert "cursor.executemany(" in source
    assert "connection.executemany(" not in source


def test_apply_accepts_explicit_read_only_api_role() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'parser.add_argument(\n        "--reader-role",' in source
    assert "grant usage on schema audit, readmodel" in source
    assert "grant select on table audit.trade_calendar_publication" in source
    assert "sql.Identifier(reader_role)" in source
