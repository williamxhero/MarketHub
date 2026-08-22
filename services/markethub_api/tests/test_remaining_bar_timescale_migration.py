from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[3] / "migrations" / "storage_v2_20260823" / "timescale_tables.py"
SPEC = importlib.util.spec_from_file_location("migrate_remaining_bar_tables_timescale", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_only_allowlisted_remaining_tables_are_supported() -> None:
    assert set(MODULE.SPECS) == {"stock_bar_1m", "stock_bar_5m", "stock_bar_30m", "future_bar_1m"}
    for spec in MODULE.SPECS.values():
        assert spec.source.startswith("fact.")
        assert spec.shadow.endswith("_ts_shadow")
        assert spec.legacy.endswith("_legacy")
        assert "bar_time" in spec.keys
        assert spec.columns[-1] == "loaded_at"


def test_key_journals_do_not_write_the_target_synchronously() -> None:
    for spec in MODULE.SPECS.values():
        statements = MODULE._journal_sql(spec, "forward")
        combined = "\n".join(statements).lower()
        assert "referencing new table as new_rows" in combined
        assert "referencing old table as old_rows" in combined
        assert "for each statement" in combined
        assert "range_start" in combined
        assert "range_end" in combined
        assert "min(bar_time),max(bar_time)" in combined
        assert "after update of bar_time" in combined
        assert "old.bar_time is distinct from new.bar_time" in combined
        assert spec.shadow not in combined
        assert len(statements) == 9
        reverse = "\n".join(MODULE._journal_sql(spec, "reverse")).lower()
        assert f"after delete on {spec.source} for each row" in reverse


def test_future_foreign_key_and_accelerated_rollback_contract_are_explicit() -> None:
    future = MODULE.SPECS["future_bar_1m"]
    assert future.columnstore is False
    assert all(spec.columnstore for name, spec in MODULE.SPECS.items() if name != "future_bar_1m")
    assert future.foreign_key == (
        "product_code,exchange,series_type",
        "ref.future_series(product_code,exchange,series_type)",
    )
    content = SCRIPT.read_text(encoding="utf-8").lower()
    assert "validate constraint" in content
    assert "rowstore_required_for_foreign_key" in content
    assert "remove_columnstore_policy" in content
    assert "forward journal backlog must be zero" in content
    assert "prepare-retry" in content
    assert "accelerated_acceptance_sha256" in content
    assert "timescale_shadow_verification" in content
    assert "rollback drill verification evidence is missing" in content
    assert "verification_evidence_sha256" in content
    assert content.index("current full verification evidence is required before cutover") < content.index("lock table {spec.source},{spec.shadow} in access exclusive mode")
    assert "bar_delta_ranges" in content
    assert "cross join lateral" in content
    assert "where bar_time between r.range_start and r.range_end offset 0" in content
    assert "set local enable_seqscan=off" in content
    assert "cursor.executemany" in content
    assert "tiny journal batch into a scan of a billion-row legacy table" in content
    assert "contype='p' and convalidated" in content
    assert "count(distinct" not in content
    assert 'cursor.execute(f"drop table {journal}")' in content
    assert "cleanup_legacy" in content
    assert "acceptance sha-256 does not match the cutover ledger" in content
    assert "legacy relation still has oid-bound dependents" in content
    assert "migration journal still exists" in content


def test_legacy_cleanup_is_version_gated_and_ledgered() -> None:
    content = SCRIPT.read_text(encoding="utf-8").lower()
    assert "legacy cleanup requires --apply and lowercase acceptance sha-256" in content
    assert "legacy_removed_at_utc" in content
    assert "legacy_removed_bytes" in content
    assert 'sql.identifier("fact", f"{spec.name}_legacy")' in content
