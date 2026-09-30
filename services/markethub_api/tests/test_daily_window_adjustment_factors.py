from __future__ import annotations

import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

QUOTEMUX_ROOT = Path(__file__).resolve().parents[4] / 'QuoteMux' / 'src'
if str(QUOTEMUX_ROOT) not in sys.path:
    sys.path.insert(0, str(QUOTEMUX_ROOT))

from runtime_paths import configure_python_path

configure_python_path()

import gzip
import json
from datetime import date

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pandas as pd
import pyarrow as pa
import pytest
from pydantic import ValidationError

from routers.stock_quote_models import StockDailyWindowQueryPayload
from services import daily_window


@pytest.fixture(autouse=True)
def _reset_coverage_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    daily_window.clear_coverage_cache()
    daily_window.clear_response_cache()
    monkeypatch.setattr(
        daily_window,
        "require_dataset_version",
        lambda _dataset_id, requested_dataset_version="", requested_market_version="": requested_dataset_version or "mhd-v1-current",
    )


def _payload(**updates: object) -> StockDailyWindowQueryPayload:
    values: dict[str, object] = {
        "data_version": "mhf-v1-test",
        "dataset_version": "mhd-v1-daily-test",
        "freq": "1d",
        "universe": "codes",
        "codes": ["600000", "000001"],
        "start_date": "2021-01-01",
        "end_date": "2021-01-31",
        "page_size": 1,
    }
    values.update(updates)
    return StockDailyWindowQueryPayload.model_validate(values)


def test_payload_accepts_include_adj_factor_parameter() -> None:
    # RED: This test will fail because include_adj_factor doesn't exist yet
    payload = _payload(include_adj_factor=True)
    assert payload.include_adj_factor is True

    payload_default = _payload()
    assert payload_default.include_adj_factor is False


def test_json_response_includes_adj_factor_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    # RED: This test will fail because the response doesn't include adj_factor yet
    def mock_coverage(_payload):
        coverage_row = {
            "universe_size": 1,
            "expected_total": 2,
            "actual_total": 2,
            "missing_total": 0,
            "duplicate_total": 0,
        }
        coverage = [
            {
                "code": "600000",
                "expected_rows": 2,
                "actual_rows": 2,
                "missing_rows": 0,
                "missing_trade_dates": [],
                "complete": True,
            }
        ]
        return coverage_row, coverage

    def mock_page_query(_query_text: str, _params: tuple) -> pd.DataFrame:
        # Simulate a page query result with adjustment factor
        items = [
            {
                "code": "600000",
                "trade_time": "2021-01-04",
                "freq": "1d",
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "pre_close": 10.0,
                "change": 0.5,
                "pct_chg": 5.0,
                "volume": 1000.0,
                "amount": 10500.0,
                "adjust": "none",
                "is_suspended": False,
                "is_st": False,
                "adj_factor": 1.0,
            }
        ]
        return pd.DataFrame([{
            "items_json": json.dumps(items),
            "returned_rows": 1,
            "has_more": False,
            "last_trade_time": "2021-01-04",
            "last_code": "600000",
        }])

    monkeypatch.setattr(daily_window, "_load_coverage_uncached", mock_coverage)
    monkeypatch.setattr(daily_window, "query_dataframe", mock_page_query)

    payload = _payload(include_adj_factor=True, codes=["600000"], start_date="2021-01-04", end_date="2021-01-04")
    response = daily_window.build_response(payload, False)
    content = json.loads(response.content)

    assert "items" in content
    assert len(content["items"]) == 1
    assert "adj_factor" in content["items"][0]
    assert content["items"][0]["adj_factor"] == 1.0
    assert content["items"][0]["close"] == 10.5


def test_json_response_excludes_adj_factor_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Test that adj_factor is NOT included when include_adj_factor=False (default)
    def mock_coverage(_payload):
        coverage_row = {
            "universe_size": 1,
            "expected_total": 1,
            "actual_total": 1,
            "missing_total": 0,
            "duplicate_total": 0,
        }
        coverage = [
            {
                "code": "600000",
                "expected_rows": 1,
                "actual_rows": 1,
                "missing_rows": 0,
                "missing_trade_dates": [],
                "complete": True,
            }
        ]
        return coverage_row, coverage

    def mock_page_query(_query_text: str, _params: tuple) -> pd.DataFrame:
        # Current implementation returns items without adj_factor
        items = [
            {
                "code": "600000",
                "trade_time": "2021-01-04",
                "freq": "1d",
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "pre_close": 10.0,
                "change": 0.5,
                "pct_chg": 5.0,
                "volume": 1000.0,
                "amount": 10500.0,
                "adjust": "none",
                "is_suspended": False,
                "is_st": False,
            }
        ]
        return pd.DataFrame([{
            "items_json": json.dumps(items),
            "returned_rows": 1,
            "has_more": False,
            "last_trade_time": "2021-01-04",
            "last_code": "600000",
        }])

    monkeypatch.setattr(daily_window, "_load_coverage_uncached", mock_coverage)
    monkeypatch.setattr(daily_window, "query_dataframe", mock_page_query)

    payload = _payload(include_adj_factor=False, codes=["600000"], start_date="2021-01-04", end_date="2021-01-04")
    response = daily_window.build_response(payload, False)
    content = json.loads(response.content)

    assert "items" in content
    assert len(content["items"]) == 1
    # Should NOT have adj_factor when include_adj_factor=False
    assert "adj_factor" not in content["items"][0]
    assert content["items"][0]["close"] == 10.5


def test_missing_adj_factors_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Test that missing adjustment factors cause fail-closed error
    def mock_coverage(_payload):
        coverage_row = {
            "universe_size": 1,
            "expected_total": 2,
            "actual_total": 2,
            "missing_total": 0,
            "duplicate_total": 0,
        }
        coverage = [
            {
                "code": "600000",
                "expected_rows": 2,
                "actual_rows": 2,
                "missing_rows": 0,
                "missing_trade_dates": [],
                "complete": True,
            }
        ]
        return coverage_row, coverage

    def mock_page_query(_query_text: str, _params: tuple) -> pd.DataFrame:
        # Simulate a page query result with missing adjustment factor (null)
        items = [
            {
                "code": "600000",
                "trade_time": "2021-01-04",
                "freq": "1d",
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "pre_close": 10.0,
                "change": 0.5,
                "pct_chg": 5.0,
                "volume": 1000.0,
                "amount": 10500.0,
                "adjust": "none",
                "is_suspended": False,
                "is_st": False,
                "adj_factor": None,  # Missing factor
            }
        ]
        return pd.DataFrame([{
            "items_json": json.dumps(items),
            "returned_rows": 1,
            "has_more": False,
            "last_trade_time": "2021-01-04",
            "last_code": "600000",
        }])

    monkeypatch.setattr(daily_window, "_load_coverage_uncached", mock_coverage)
    monkeypatch.setattr(daily_window, "query_dataframe", mock_page_query)

    payload = _payload(include_adj_factor=True, codes=["600000"], start_date="2021-01-04", end_date="2021-01-04")

    # Should raise HTTPException with incomplete coverage error
    with pytest.raises(HTTPException) as exc_info:
        daily_window.build_response(payload, False)

    assert exc_info.value.status_code == 503
    assert "ADJ_FACTOR_INCOMPLETE" in exc_info.value.detail["code"]
    assert "600000" in exc_info.value.detail["message"]

