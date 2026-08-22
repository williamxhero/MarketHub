from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "maintenance" / "migrate_stock_bar_1m_timescale_shadow.py"
SPEC = importlib.util.spec_from_file_location("migrate_stock_bar_1m_timescale_shadow", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_fixed_contract_and_allowlists() -> None:
    assert MODULE.SOURCE == "fact.stock_bar_1m"
    assert MODULE.SHADOW == "fact.stock_bar_1m_ts_shadow"
    assert MODULE.LEGACY == "fact.stock_bar_1m_legacy"
    assert MODULE.ALLOWED_CHUNKS == {"7 days", "14 days", "1 month"}
    assert MODULE.ALLOWED_ORDERS == {"ASC", "DESC"}
    assert MODULE.PK == ("market", "code", "bar_time")
    assert "call add_columnstore_policy" in SCRIPT.read_text(encoding="utf-8").lower()


def test_forward_mirror_uses_three_statement_transition_table_triggers() -> None:
    statements = MODULE._mirror_sql(MODULE.SOURCE, MODULE.SHADOW, "stock_bar_1m_ts_forward")
    combined = "\n".join(statements).lower()
    assert "referencing new table as new_rows" in combined
    assert "referencing old table as old_rows" in combined
    assert "for each statement" in combined
    assert "on conflict(market,code,bar_time) do update" in combined
    assert len(statements) == 6


def test_reverse_mirror_uses_row_delete_for_timescale_compatibility() -> None:
    statements = MODULE._mirror_sql(MODULE.SOURCE, MODULE.LEGACY, "stock_bar_1m_ts_reverse")
    combined = "\n".join(statements).lower()
    assert "after delete on fact.stock_bar_1m for each row" in combined
    assert "(old.market,old.code,old.bar_time)" in combined
    assert "after insert on fact.stock_bar_1m referencing new table as new_rows for each statement" in combined
    assert "after update on fact.stock_bar_1m referencing old table as old_rows new table as new_rows for each statement" in combined


def test_key_journal_captures_only_keys_with_statement_transition_tables() -> None:
    statements = MODULE._journal_sql("forward")
    combined = "\n".join(statements).lower()
    assert MODULE.FORWARD_JOURNAL == "audit.stock_bar_1m_ts_forward_delta"
    assert "select market,code,bar_time from new_rows" in combined
    assert "select market,code,bar_time from old_rows" in combined
    assert "except select market,code,bar_time from new_rows" in combined
    assert "referencing new table as new_rows" in combined
    assert "referencing old table as old_rows" in combined
    assert "fact.stock_bar_1m_ts_shadow" not in combined
    assert len(statements) == 7
    reverse = "\n".join(MODULE._journal_sql("reverse")).lower()
    assert "after delete on fact.stock_bar_1m for each row" in reverse
    assert "values(old.market,old.code,old.bar_time)" in reverse


def test_journal_reconciler_is_idempotent_and_current_state_based() -> None:
    content = SCRIPT.read_text(encoding="utf-8").lower()
    assert "for update skip locked" in content
    assert "select distinct market,code,bar_time from stock_bar_1m_delta_batch" in content
    assert "not exists" in content
    assert "on conflict(market,code,bar_time) do update" in content
    assert "journal delete mismatch" in content


def test_migration_is_resumable_fail_closed_and_keeps_legacy() -> None:
    content = SCRIPT.read_text(encoding="utf-8").lower()
    assert "already_verified" in content
    assert "on conflict(market,code,bar_time) do nothing" in content
    assert "monthly evidence mismatch" in content
    assert "access exclusive" in content
    assert "seven full natural days" not in content
    assert "accelerated acceptance requires a lowercase sha-256 evidence digest" in content
    assert "forward journal backlog must be zero before cutover" in content
    assert "migration ledger is incomplete" in content
    assert "double-read mismatch" in content
    assert "reconcile evidence mismatch" in content
    assert "cutover monthly evidence mismatch" in content
    assert "reverse transition probe mismatch" in content
    assert "stock_bar_1m_ts_shadow_failed" in content
    assert "oid-bound dependents" in content
    assert "explicit_grants_restored" in content
    assert '"legacy": "retained_read_only"' in content
    assert "drop table" not in content
