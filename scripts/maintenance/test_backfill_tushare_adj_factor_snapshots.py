from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backfill_tushare_adj_factor_snapshots as mod  # noqa: E402
from platform_models import AdjFactorItem  # noqa: E402


def _database_configured() -> bool:
    return all(
        os.getenv(name, "")
        for name in ("MARKETHUB_DB_HOST", "MARKETHUB_DB_PORT", "MARKETHUB_DB_NAME", "MARKETHUB_DB_USER", "MARKETHUB_DB_PASSWORD")
    )


requires_db = pytest.mark.skipif(not _database_configured(), reason="requires an isolated PostgreSQL test database")


def _connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
    )


def _ensure_stock(connection: psycopg.Connection, code: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into ref.stock (market, code, name) values ('SHSE', %s, %s) "
            "on conflict (market, code) do nothing",
            (code, f"test-{code}"),
        )


def _reset_fixture(connection: psycopg.Connection, code: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("create schema if not exists fact")
        cursor.execute(
            """
            create table if not exists fact.stock_daily_1d (
                market text not null, code text not null, trade_date date not null,
                open double precision, high double precision, low double precision,
                close double precision, volume double precision, amount double precision,
                is_suspended boolean not null default false,
                adj_factor double precision,
                loaded_at timestamptz not null default now(),
                primary key (market, code, trade_date)
            )
            """
        )
        cursor.execute("delete from fact.stock_daily_1d where code = %s", (code,))
        if _table_exists(connection, "audit", "stock_adj_factor_daily_status"):
            cursor.execute("delete from audit.stock_adj_factor_daily_status")
    _ensure_stock(connection, code)
    connection.commit()


def _table_exists(connection: psycopg.Connection, schema: str, table: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("select to_regclass(%s) is not null", (f"{schema}.{table}",))
        return bool(cursor.fetchone()[0])


@requires_db
def test_find_incomplete_trade_dates_only_returns_null_adj_factor_rows() -> None:
    code = "600000"
    with _connect() as connection:
        _reset_fixture(connection, code)
        with connection.cursor() as cursor:
            cursor.execute(
                "insert into fact.stock_daily_1d (market, code, trade_date, open, high, low, close, volume, amount, adj_factor) values "
                "('SHSE', %s, '2026-08-10', 1,1,1,1,1,1, null), "
                "('SHSE', %s, '2026-08-11', 1,1,1,1,1,1, 2.5)",
                (code, code),
            )
        connection.commit()
        dates = mod.find_incomplete_trade_dates(connection, "2026-08-01", "2026-08-31")
    assert "2026-08-10" in dates
    assert "2026-08-11" not in dates


@requires_db
def test_record_day_status_upsert_is_retry_safe_and_isolated_per_day() -> None:
    with _connect() as connection:
        mod._ensure_daily_status_schema(connection)
        mod.record_day_status(connection, "2026-08-10", status="failed", error_message="boom")
        mod.record_day_status(connection, "2026-08-11", status="success", row_count=5000)
        rows = {row["trade_date"]: row for row in mod.list_day_statuses(connection, "2026-08-01", "2026-08-31")}
        assert rows["2026-08-10"]["status"] == "failed"
        assert rows["2026-08-11"]["status"] == "success"

        # A retry of the failed day must only overwrite that day's own row.
        mod.record_day_status(connection, "2026-08-10", status="success", row_count=4800)
        rows = {row["trade_date"]: row for row in mod.list_day_statuses(connection, "2026-08-01", "2026-08-31")}
        assert rows["2026-08-10"]["status"] == "success"
        assert rows["2026-08-10"]["row_count"] == 4800
        assert rows["2026-08-11"]["status"] == "success"
        assert rows["2026-08-11"]["row_count"] == 5000


@requires_db
def test_apply_artifact_never_overwrites_and_fails_closed_on_conflict(tmp_path: Path) -> None:
    code = "600000"
    with _connect() as connection:
        _reset_fixture(connection, code)
        with connection.cursor() as cursor:
            cursor.execute(
                "insert into fact.stock_daily_1d (market, code, trade_date, open, high, low, close, volume, amount, adj_factor) values "
                "('SHSE', %s, '2026-08-10', 1,1,1,1,1,1, 3.0)",
                (code,),
            )
        connection.commit()

    manifest = mod.fetch_snapshots(
        "2026-08-10",
        "2026-08-10",
        tmp_path,
        handler=lambda trade_date: [AdjFactorItem(code=code, trade_date=trade_date, adj_factor=99.0)],
    )
    manifest_path = Path(manifest["raw_csv"]).with_suffix(".manifest.json")

    with pytest.raises(RuntimeError, match="adj_factor_existing_conflicts"):
        mod.apply_artifact(manifest_path)

    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("select adj_factor from fact.stock_daily_1d where code = %s and trade_date = '2026-08-10'", (code,))
            stored = cursor.fetchone()[0]
    assert stored == 3.0  # unchanged: the provider "revision" was never applied


@requires_db
def test_run_daily_persists_status_per_date_and_is_retryable(tmp_path: Path) -> None:
    ok_code, bad_code = "600001", "600002"
    with _connect() as connection:
        _reset_fixture(connection, ok_code)
        _ensure_stock(connection, bad_code)
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute("delete from fact.stock_daily_1d where code = %s", (bad_code,))
            cursor.execute(
                "insert into fact.stock_daily_1d (market, code, trade_date, open, high, low, close, volume, amount, adj_factor) values "
                "('SHSE', %s, '2026-08-10', 1,1,1,1,1,1, null), "
                "('SHSE', %s, '2026-08-11', 1,1,1,1,1,1, null)",
                (ok_code, bad_code),
            )
        connection.commit()

    def handler(trade_date: str) -> list[AdjFactorItem]:
        if trade_date == "2026-08-11":
            return []  # simulate the provider returning nothing for this day
        return [AdjFactorItem(code=ok_code, trade_date=trade_date, adj_factor=1.23)]

    empty_env_file = tmp_path / "empty.env"
    empty_env_file.write_text("", encoding="utf-8")
    result = mod.run_daily(empty_env_file, tmp_path, "2026-08-01", "2026-08-31", handler=handler)

    by_date = {item["trade_date"]: item for item in result["results"]}
    assert by_date["2026-08-10"]["status"] == "success"
    assert by_date["2026-08-11"]["status"] == "failed"
    assert result["failed"] == 1

    with _connect() as connection:
        # Re-running must not re-touch the already-successful day; only the failed one remains incomplete.
        remaining = mod.find_incomplete_trade_dates(connection, "2026-08-01", "2026-08-31")
    assert "2026-08-10" not in remaining
    assert "2026-08-11" in remaining


def test_daily_update_script_scopes_since_date_and_propagates_exit_code() -> None:
    source = (SCRIPT_DIR.parent / "dailyupdate" / "adj-factor-daily-update.sh").read_text(encoding="utf-8")

    assert "set -Eeuo pipefail" in source
    assert 'MARKETHUB_ADJ_FACTOR_DAILY_SINCE_DATE="${MARKETHUB_ADJ_FACTOR_DAILY_SINCE_DATE:-${QUOTEMUX_ADJUSTMENT_BASE_DATE:-}}"' in source
    assert "backfill_tushare_adj_factor_snapshots.py" in source
    assert "daily \\" in source
    assert '--since-date "$MARKETHUB_ADJ_FACTOR_DAILY_SINCE_DATE"' in source
    assert 'raise SystemExit(f"复权因子每日更新存在失败交易日' in source
    assert 'main 2>&1 | tee -a "$LOG_PATH"' in source
