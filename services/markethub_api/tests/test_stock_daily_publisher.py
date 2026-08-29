from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "publisher" / "publish_stock_daily_parquet.py"
SPEC = importlib.util.spec_from_file_location("publish_stock_daily_parquet", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_publisher_contract_is_immutable_streaming_and_fail_closed() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert MODULE.DATASET_ID == "stock_daily_1d"
    assert MODULE.SCHEMA_VERSION == "markethub-stock-daily-parquet-v1"
    assert "fetchmany(row_group_rows)" in content
    assert "coverage incomplete" in content
    assert "dataset changed during publish" in content
    assert "os.replace(staging, final_root)" in content
    assert "market version mapping conflict" in content
    assert '"url": f"/api/exports/{DATASET_ID}/{dataset_version}/files/{relative_path}"' in content
    assert "date '2021-11-15'" in content
    assert "ensure_current_stock_daily_coverage" in content
    assert "mark_stock_daily_publication_ready" in content
    assert "pg_try_advisory_lock" in content
    assert "Parquet publication lock timeout" in content
    assert "resuming stock daily Parquet publication" in content
    assert "retained resumable staging" in content
    assert "stock daily Parquet publication checkpoint" in content
    assert "part.mkdir(parents=True, exist_ok=True)" in content
    assert "ref.stock_code_migration migration" in content
    assert "left(code,3)='920'" in content
    assert "listed_date >= date '2024-04-22'" in content
    assert "MARKETHUB_STOCK_DAILY_EXPORT_START" in content
    assert "d.trade_date<u.delisted_date" in content
    assert tuple(field.name for field in MODULE.BARS_SCHEMA) == (
        "market", "code", "trade_date", "open", "high", "low", "close", "volume", "amount",
        "is_suspended", "is_st", "pre_close", "change", "pct_chg", "adj_factor", "loaded_at",
    )


def test_dataset_version_matches_api_contract() -> None:
    first = MODULE._version("stock_daily_1d", "baseline", 4)
    second = MODULE._version("stock_daily_1d", "baseline", 5)
    assert first.startswith("mhd-v1-") and len(first) == 71
    assert first != second


def test_months_preserve_partial_dataset_bounds() -> None:
    from datetime import date

    assert list(MODULE._months(date(2024, 1, 15), date(2024, 2, 10))) == [
        (date(2024, 1, 15), date(2024, 2, 1)),
        (date(2024, 2, 1), date(2024, 2, 11)),
    ]


def test_parquet_contract_reuses_precomputed_coverage_and_still_filters_fact_rows() -> None:
    assert "fact.stock_daily_1d" not in MODULE._COVERAGE_SQL
    assert "0::int as missing_rows" in MODULE._COVERAGE_SQL
    assert "coalesce(b.is_suspended,false)=true" in MODULE._BARS_SQL
    assert "stock_suspension_history x" in MODULE._BARS_SQL


def test_parquet_coverage_uses_catalog_identity_for_bjse_migrations() -> None:
    assert "migration.old_code=catalog.code" in MODULE._COVERAGE_SQL
    assert "migration.old_code=ref.stock.code" not in MODULE._COVERAGE_SQL
