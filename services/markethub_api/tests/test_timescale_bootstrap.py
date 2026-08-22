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


def test_fresh_stock_bar_1m_is_selected_timescale_layout() -> None:
    schema = "\n".join(MODULE.BASE_SCHEMA_SQL).lower()
    assert "if to_regclass('fact.stock_bar_1m') is null" in schema
    assert "by_range('bar_time',interval '14 days')" in schema
    assert "timescaledb.segmentby='market,code'" in schema
    assert "timescaledb.orderby='bar_time asc'" in schema
    assert "after => interval '30 days'" in schema
    assert "call add_columnstore_policy" in schema
    assert "stock_bar_1m_code_time_idx" in schema
    assert "stock_bar_1m_time_idx" in schema
    assert "migrate_data" not in schema
