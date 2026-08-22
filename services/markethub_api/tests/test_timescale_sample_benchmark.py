from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "benchmark" / "benchmark_timescale_sample.py"
SPEC = importlib.util.spec_from_file_location("benchmark_timescale_sample", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_candidate_matrix_and_complete_months_are_fixed() -> None:
    assert tuple(MODULE.PROFILES) == ("7d-asc", "7d-desc", "14d-asc", "14d-desc", "1month-asc", "1month-desc")
    assert MODULE.MONTHS == (("2022-01-01", "2022-02-01"), ("2024-01-01", "2024-02-01"), ("2026-04-01", "2026-05-01"))


def test_sample_script_has_safety_correctness_and_required_benchmarks() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert "100 * 1024**3" in content
    assert "stock_bar_1m_ts_sample" in content
    assert "call convert_to_columnstore" in content
    assert "hypertable_detailed_size" in content
    assert '"size_before_columnstore"' in content
    assert '"size_after_columnstore"' in content
    assert "stable_hash_sum" in content
    assert "already_complete" in content
    assert "partial month requires explicit sample rebuild" in content
    assert "primary_key_count" in content
    for scenario in ("point", "single_long", "codes_200", "market_range", "upsert_1000_rollback"):
        assert scenario in content
    assert "drop table if exists fact.stock_bar_1m_ts_sample" in content
    assert 'drop table if exists fact.stock_bar_1m"' not in content


def test_percentile_uses_nearest_rank() -> None:
    assert MODULE._percentile([float(value) for value in range(1, 21)], 0.95) == 19.0
