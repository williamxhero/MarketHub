from __future__ import annotations

import importlib.util
import hashlib
from pathlib import Path
import sys


SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "publisher" / "publish_stock_daily_parquet.py"
)
DAILY_COVERAGE_READ_MODEL = (
    Path(__file__).resolve().parents[1] / "src" / "services" / "daily_coverage_read_model.py"
)
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
    assert "changed during publish" in content
    assert "os.replace(staging, final_root)" in content
    assert "market version mapping conflict" in content
    assert "published manifest market version mismatch" in content
    assert "_read_published_manifest" in content
    assert '"url": f"/api/exports/{DATASET_ID}/{dataset_version}/files/{relative_path}"' in content
    assert "date '2021-11-15'" in content
    assert "ensure_current_stock_daily_coverage" in content
    assert "mark_stock_daily_publication_online" in content
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
        "market",
        "code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "is_suspended",
        "is_st",
        "pre_close",
        "change",
        "pct_chg",
        "adj_factor",
        "loaded_at",
    )


def test_publisher_only_promotes_complete_coverage_to_online() -> None:
    content = DAILY_COVERAGE_READ_MODEL.read_text(encoding="utf-8")

    assert "def mark_stock_daily_publication_online" in content
    assert "set status='online'" in content
    assert "where dataset_id=%s and dataset_version=%s and coverage_ready and complete" in content
    assert "mark_stock_daily_publication_ready = mark_stock_daily_publication_online" in content


def test_dataset_version_matches_api_contract() -> None:
    first = MODULE._version("stock_daily_1d", "baseline", 4)
    second = MODULE._version("stock_daily_1d", "baseline", 5)
    assert first.startswith("mhd-v1-") and len(first) == 71
    assert first != second


def test_publisher_rejects_market_version_drift_during_stable_dataset_publish() -> None:
    try:
        MODULE._require_version_unchanged(
            "market data version",
            "mhf-v1-" + "a" * 64,
            "mhf-v1-" + "b" * 64,
        )
    except RuntimeError as exc:
        assert str(exc) == (
            "market data version changed during publish: "
            "start=mhf-v1-" + "a" * 64 + " end=mhf-v1-" + "b" * 64
        )
    else:
        raise AssertionError("market version drift must fail closed")


def test_publisher_accepts_unchanged_market_version() -> None:
    version = "mhf-v1-" + "a" * 64
    MODULE._require_version_unchanged("market data version", version, version)


def test_publisher_uses_the_canonical_health_market_version(monkeypatch) -> None:
    version = "mhf-v1-" + "c" * 64
    monkeypatch.setattr(MODULE, "current_market_data_version", lambda: version)
    assert MODULE._market_version() == version


def test_existing_manifest_must_match_current_market_version(tmp_path: Path) -> None:
    dataset_version = "mhd-v1-" + "a" * 64
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        '{"dataset_id":"stock_daily_1d","dataset_version":"'
        + dataset_version
        + '","market_data_version":"mhf-v1-'
        + "b" * 64
        + '"}',
        encoding="utf-8",
    )

    try:
        MODULE._read_published_manifest(manifest_path, dataset_version, "mhf-v1-" + "c" * 64)
    except RuntimeError as exc:
        assert "market version mismatch" in str(exc)
    else:
        raise AssertionError("a manifest bound to another market version must be rejected")


def test_existing_frozen_publication_rechecks_source_identity(monkeypatch, tmp_path: Path) -> None:
    content = b"immutable bars"
    bars = tmp_path / "year=2024" / "month=01" / "bars.parquet"
    bars.parent.mkdir(parents=True)
    bars.write_bytes(content)
    manifest = {
        "dataset_id": "stock_daily_1d",
        "dataset_version": "mhd-v1-" + "a" * 64,
        "partitions": [
            {
                "partition_key": "2024-01-01:2024-02-01",
                "status": "frozen",
                "start": "2024-01-01",
                "end_exclusive": "2024-02-01",
                "rows": 1,
                "source_sha256": "source",
                "coverage_sha256": "coverage",
                "files": [
                    {
                        "path": "year=2024/month=01/bars.parquet",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "bytes": len(content),
                    }
                ],
            }
        ],
    }
    monkeypatch.setattr(MODULE, "_bars_identity", lambda *_: (1, "source"))
    monkeypatch.setattr(MODULE, "_coverage_identity", lambda *_: ([], 1, "coverage"))
    MODULE._verify_existing_publication(None, tmp_path, manifest)

    monkeypatch.setattr(MODULE, "_bars_identity", lambda *_: (1, "changed"))
    try:
        MODULE._verify_existing_publication(None, tmp_path, manifest)
    except RuntimeError as exc:
        assert "content identity changed" in str(exc)
    else:
        raise AssertionError("changed frozen source identity must fail closed")


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
