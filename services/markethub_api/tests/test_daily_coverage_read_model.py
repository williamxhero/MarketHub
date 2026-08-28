from __future__ import annotations

import pandas as pd
from fastapi import HTTPException
import pytest

from services import daily_coverage_read_model as read_model


def test_coverage_universe_requires_auditable_bse_identity() -> None:
    assert "ref.stock_code_migration migration" in read_model._CREATE_STATE_SQL
    assert "left(code,3)='920'" in read_model._CREATE_STATE_SQL


def test_daily_read_model_builder_materializes_sparse_gaps_and_day_summaries() -> None:
    assert "create temporary table query_read_daily_state" in read_model._CREATE_STATE_SQL
    assert "fact.stock_daily_1d" in read_model._CREATE_STATE_SQL
    assert "readmodel.stock_daily_coverage_day" in read_model._INSERT_DAY_SQL
    assert "readmodel.stock_daily_coverage_gap" in read_model._INSERT_GAP_SQL
    assert "for each row" not in read_model._CREATE_STATE_SQL.lower()
    assert "suspended_daily.is_suspended" in read_model._CREATE_STATE_SQL


def test_daily_read_model_uses_common_market_fact_horizon() -> None:
    source = __import__("inspect").getsource(read_model._build_current_stock_daily_coverage_once)
    assert "select max(first) first,max(last) last" in source
    assert "group by market" in source


def test_coverage_build_retries_when_capture_advances_the_dataset_version(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def build_once(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append(1)
        if len(calls) == 1:
            raise read_model.DatasetVersionChanged("dataset changed during coverage build: old -> new")
        return {"dataset_version": "new", "complete": True}

    monkeypatch.setattr(read_model, "_coverage_retry_config", lambda: (3, 0.25))
    monkeypatch.setattr(read_model, "_build_current_stock_daily_coverage_once", build_once)
    monkeypatch.setattr(read_model.time, "sleep", sleeps.append)

    assert read_model.build_current_stock_daily_coverage()["dataset_version"] == "new"
    assert calls == [1, 1]
    assert sleeps == [0.25]


def test_coverage_build_retries_recoverable_serialization_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def build_once(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append(1)
        if len(calls) == 1:
            raise read_model.psycopg.errors.SerializationFailure("concurrent update")
        return {"dataset_version": "stable", "complete": True}

    monkeypatch.setattr(read_model, "_coverage_retry_config", lambda: (3, 0.5))
    monkeypatch.setattr(read_model, "_build_current_stock_daily_coverage_once", build_once)
    monkeypatch.setattr(read_model.time, "sleep", sleeps.append)

    assert read_model.build_current_stock_daily_coverage()["complete"] is True
    assert calls == [1, 1]
    assert sleeps == [0.5]


def test_coverage_build_fails_after_bounded_serialization_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def build_once(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append(1)
        raise read_model.psycopg.errors.SerializationFailure("concurrent update")

    monkeypatch.setattr(read_model, "_coverage_retry_config", lambda: (3, 0.5))
    monkeypatch.setattr(read_model, "_build_current_stock_daily_coverage_once", build_once)
    monkeypatch.setattr(read_model.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError, match="exhausted retry budget attempts=3"):
        read_model.build_current_stock_daily_coverage()
    assert calls == [1, 1, 1]
    assert sleeps == [0.5, 1.0]


def test_request_coverage_reads_only_readmodel_and_returns_complete_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    queries: list[str] = []

    def query(sql: str, _params: tuple[object, ...]) -> pd.DataFrame:
        queries.append(sql)
        if "dataset_build_state" in sql:
            return pd.DataFrame([{"coverage_ready": True, "status": "parquet_pending", "complete": True}])
        if "stock_daily_coverage_day" in sql:
            return pd.DataFrame([{"expected_total": 10, "actual_total": 10, "missing_total": 0, "duplicate_total": 0}])
        return pd.DataFrame(columns=["market", "code", "trade_date", "reason", "expected_rows", "actual_rows"])

    monkeypatch.setattr(read_model, "query_dataframe", query)
    summary, gaps = read_model.load_stock_daily_coverage_summary("mhd-v1-current", "2026-08-21", "2026-08-21")
    assert summary["actual_total"] == 10
    assert gaps == []
    assert all("fact.stock_daily_1d" not in sql for sql in queries)


def test_incomplete_readmodel_returns_fast_structured_409(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        (
            pd.DataFrame([{"coverage_ready": True, "status": "failed", "complete": False}]),
            pd.DataFrame([{"expected_total": 10, "actual_total": 9, "missing_total": 1, "duplicate_total": 0}]),
            pd.DataFrame([{"market": "SHSE", "code": "600000", "trade_date": "2026-08-21", "reason": "missing", "expected_rows": 1, "actual_rows": 0}]),
        )
    )
    monkeypatch.setattr(read_model, "query_dataframe", lambda *_args, **_kwargs: next(responses))
    with pytest.raises(HTTPException) as raised:
        read_model.load_stock_daily_coverage_summary("mhd-v1-current", "2026-08-21", "2026-08-21")
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "DATA_INCOMPLETE"
    assert raised.value.detail["details"]["repair_endpoint"] == "/api/admin/data-repairs"


def test_missing_readmodel_returns_503_without_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_model, "query_dataframe", lambda *_args, **_kwargs: pd.DataFrame())
    with pytest.raises(HTTPException) as raised:
        read_model.load_stock_daily_coverage_summary("mhd-v1-current", "2026-08-21", "2026-08-21")
    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "READ_MODEL_NOT_READY"
