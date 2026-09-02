from __future__ import annotations

from pathlib import Path
import sys

from fastapi.testclient import TestClient


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from runtime_paths import configure_python_path


configure_python_path()

from app import app
from routers.stock_quote_models import CurrentStockQuoteItem, CurrentStockQuotesMeta, CurrentStockQuotesQueryResult
from services import live_bars


def _current_result() -> CurrentStockQuotesQueryResult:
    return CurrentStockQuotesQueryResult(
        items=[
            CurrentStockQuoteItem(
                code="600519",
                trade_time="2026-09-02T13:30:00+08:00",
                freq="1m",
                open=1400.0,
                high=1401.0,
                low=1399.0,
                close=1400.5,
                volume=1200.0,
                amount=1_680_600.0,
                interval_start="2026-09-02T13:30:00+08:00",
                interval_end="2026-09-02T13:31:00+08:00",
                is_final=False,
                observed_at="2026-09-02T13:30:08+08:00",
                provider="mootdx",
                source_semantics="native",
                observation_version="observation-v1",
                freshness_ms=3_000,
                market_status="trading",
            )
        ],
        meta=CurrentStockQuotesMeta(
            total_rows=1,
            returned_rows=1,
            complete=True,
            truncated=False,
            effective_now="2026-09-02T13:30:08+08:00",
            historical_dataset_version="",
        ),
    )


def test_get_current_bar_uses_existing_route_without_data_version(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(live_bars, "get_current_quotes", lambda request: captured.append(request) or _current_result())

    response = TestClient(app).get(
        "/api/stocks/quotes",
        params={"code": "600519", "freq": "1m", "datetime": "now"},
    )

    assert response.status_code == 200
    assert captured[0].count == 1
    assert captured[0].effective_now.utcoffset().total_seconds() == 8 * 60 * 60
    payload = response.json()
    assert payload["items"][0]["interval_start"] == "2026-09-02T13:30:00+08:00"
    assert payload["items"][0]["is_final"] is False
    assert payload["items"][0]["observation_version"] == "observation-v1"
    assert payload["meta"]["effective_now"] == "2026-09-02T13:30:08+08:00"


def test_current_bar_rejects_range_parameters(monkeypatch) -> None:
    monkeypatch.setattr(live_bars, "get_current_quotes", lambda _request: _current_result())

    response = TestClient(app).get(
        "/api/stocks/quotes",
        params={
            "code": "600519",
            "freq": "1m",
            "datetime": "now",
            "start_time": "2026-09-02 13:00:00",
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "HTTP_422"
    assert "datetime=now" in response.json()["message"]


def test_post_current_bar_uses_the_same_gateway_without_data_version(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(live_bars, "get_current_quotes", lambda request: captured.append(request) or _current_result())

    response = TestClient(app).post(
        "/api/stocks/quotes/query",
        json={"codes": ["600519"], "freq": "1m", "datetime": "now"},
    )

    assert response.status_code == 200
    assert captured[0].count == 1
    assert response.json()["items"][0]["provider"] == "mootdx"


def test_current_bar_reports_unavailable_gateway() -> None:
    response = TestClient(app).get(
        "/api/stocks/quotes",
        params={"code": "600519", "freq": "1m", "datetime": "now"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "LIVE_INGEST_UNAVAILABLE"
