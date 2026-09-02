from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import subprocess
import sys

from fastapi.testclient import TestClient
import pandas as pd
import pytest


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


class _ActiveSessionResolver:
    @staticmethod
    def resolve(effective_now):
        del effective_now
        return live_bars.CurrentBarSession(market_status="trading", active=True)


def _active_gateway() -> live_bars.QuoteMuxWorkerGateway:
    return live_bars.QuoteMuxWorkerGateway(session_resolver=_ActiveSessionResolver())


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


def test_current_bar_reports_unavailable_gateway(monkeypatch) -> None:
    monkeypatch.setattr(live_bars, "_GATEWAY", live_bars._UnavailableCurrentBarGateway())
    response = TestClient(app).get(
        "/api/stocks/quotes",
        params={"code": "600519", "freq": "1m", "datetime": "now"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "LIVE_INGEST_UNAVAILABLE"


def test_current_bar_gateway_maps_a_committed_worker_result(monkeypatch) -> None:
    captured: dict[str, object] = {}
    worker_payload = {
        "items": [
            {
                "code": "600519", "trade_time": "2026-09-02T13:30:00+08:00", "freq": "1m",
                "open": 1400.0, "high": 1401.0, "low": 1399.0, "close": 1400.5,
                "volume": 1200, "amount": 1680600.0, "adjust": "none", "is_suspended": False, "is_st": False,
                "interval_start": "2026-09-02T13:30:00+08:00", "interval_end": "2026-09-02T13:31:00+08:00",
                "is_final": False, "observed_at": "2026-09-02T13:30:09+08:00", "last_trade_at": "2026-09-02T13:30:00+08:00",
                "provider": "mootdx", "source_semantics": "native", "observation_version": "42",
                "freshness_ms": 1000, "degraded": False, "market_status": "trading",
            }
        ],
        "errors": [],
        "diagnostics": [
            {"code": "600519", "validator": "efinance", "status": "warning", "difference_ratio": 0.02},
        ],
    }

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout=json.dumps(worker_payload), stderr="")

    monkeypatch.setattr(live_bars.subprocess, "run", fake_run)
    monkeypatch.setattr(live_bars, "_GATEWAY", _active_gateway())

    result = live_bars.get_current_quotes(
        live_bars.CurrentBarRequest(codes=("600519",), freq="1m", count=1, adjust="none", effective_now=datetime.fromisoformat(_current_result().meta.effective_now))
    )

    assert result.items[0].observation_version == "42"
    assert result.diagnostics == [{"code": "600519", "validator": "efinance", "status": "warning", "difference_ratio": 0.02}]
    assert json.loads(captured["kwargs"]["input"])["codes"] == ["600519"]


def test_current_bar_gateway_refreshes_stale_observations_but_never_uses_a_previous_interval(monkeypatch) -> None:
    calls: list[object] = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        observed_at = "2026-09-02T13:29:07+08:00" if len(calls) == 1 else "2026-09-02T13:30:08+08:00"
        payload = {
            "items": [
                {
                    "code": "600519", "trade_time": "2026-09-02T13:30:00+08:00", "freq": "1m",
                    "open": 1400.0, "high": 1401.0, "low": 1399.0, "close": 1400.5,
                    "volume": 1200, "amount": 1680600.0, "adjust": "none", "is_suspended": False, "is_st": False,
                    "interval_start": "2026-09-02T13:30:00+08:00", "interval_end": "2026-09-02T13:31:00+08:00",
                    "is_final": False, "observed_at": observed_at, "last_trade_at": "2026-09-02T13:30:00+08:00",
                    "provider": "mootdx", "source_semantics": "native", "observation_version": str(len(calls)),
                    "freshness_ms": 0, "degraded": False, "market_status": "trading",
                }
            ], "errors": [],
        }
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(live_bars.subprocess, "run", fake_run)
    gateway = _active_gateway()
    request = live_bars.CurrentBarRequest(
        codes=("600519",), freq="1m", count=1, adjust="none", effective_now=datetime.fromisoformat("2026-09-02T13:30:08+08:00"),
    )

    gateway.get_current_quotes(request)
    refreshed = gateway.get_current_quotes(request)
    cached = gateway.get_current_quotes(request)

    assert len(calls) == 2  # cached age is >60s, so it was refreshed; the new observation is reused
    assert refreshed.items[0].observation_version == "2"
    assert cached.items[0].observation_version == "2"


def test_current_bar_gateway_allows_exactly_300_seconds_as_degraded_fallback_but_rejects_more(monkeypatch) -> None:
    calls = 0
    payload = {
        "items": [
            {
                "code": "600519", "trade_time": "2026-09-02T13:30:00+08:00", "freq": "1m",
                "open": 1400.0, "high": 1401.0, "low": 1399.0, "close": 1400.5,
                "volume": 1200, "amount": 1680600.0, "adjust": "none", "is_suspended": False, "is_st": False,
                "interval_start": "2026-09-02T13:30:00+08:00", "interval_end": "2026-09-02T13:31:00+08:00",
                "is_final": False, "observed_at": "2026-09-02T13:25:08+08:00", "last_trade_at": "2026-09-02T13:30:00+08:00",
                "provider": "mootdx", "source_semantics": "native", "observation_version": "1",
                "freshness_ms": 0, "degraded": False, "market_status": "trading",
            }
        ], "errors": [],
    }

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise subprocess.TimeoutExpired(args[0], 8)
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(live_bars.subprocess, "run", fake_run)
    gateway = _active_gateway()
    initial = live_bars.CurrentBarRequest(("600519",), "1m", 1, "none", datetime.fromisoformat("2026-09-02T13:30:08+08:00"))
    exactly_300 = live_bars.CurrentBarRequest(("600519",), "1m", 1, "none", datetime.fromisoformat("2026-09-02T13:30:08+08:00"))
    over_300 = live_bars.CurrentBarRequest(("600519",), "1m", 1, "none", datetime.fromisoformat("2026-09-02T13:30:09+08:00"))

    gateway.get_current_quotes(initial)
    fallback = gateway.get_current_quotes(exactly_300)

    assert fallback.items[0].degraded is True
    with pytest.raises(live_bars.LiveBarUnavailable):
        gateway.get_current_quotes(over_300)


def test_current_bar_gateway_returns_finalized_history_during_lunch_without_calling_providers(monkeypatch) -> None:
    lunch = datetime.fromisoformat("2026-09-02T12:00:00+08:00")

    class _SessionResolver:
        @staticmethod
        def resolve(effective_now):
            assert effective_now == lunch
            return live_bars.CurrentBarSession(market_status="recess", active=False)

    class _FinalizedReader:
        @staticmethod
        def get_latest_finalized(request, market_status):
            assert request.effective_now == lunch
            assert market_status == "recess"
            return CurrentStockQuotesQueryResult(
                items=[_current_result().items[0].model_copy(update={"is_final": True, "market_status": "recess", "observed_at": "2026-09-02T11:30:07+08:00"})],
                meta=CurrentStockQuotesMeta(total_rows=1, returned_rows=1, complete=True, truncated=False, effective_now=lunch.isoformat()),
            )

    monkeypatch.setattr(live_bars.subprocess, "run", lambda *args, **kwargs: pytest.fail("recess must not invoke the live provider worker"))
    gateway = live_bars.QuoteMuxWorkerGateway(session_resolver=_SessionResolver(), finalized_reader=_FinalizedReader())

    result = gateway.get_current_quotes(live_bars.CurrentBarRequest(("600519",), "1m", 1, "none", lunch))

    assert result.items[0].is_final is True
    assert result.items[0].market_status == "recess"


def test_china_stock_session_uses_shanghai_boundaries_and_calendar(monkeypatch) -> None:
    monkeypatch.setattr(live_bars, "query_dataframe", lambda *args, **kwargs: pd.DataFrame([{"is_open": True}]))
    resolver = live_bars.ChinaStockSessionResolver()

    assert resolver.resolve(datetime.fromisoformat("2026-09-02T09:29:59+08:00")) == live_bars.CurrentBarSession("preopen", False)
    assert resolver.resolve(datetime.fromisoformat("2026-09-02T09:30:00+08:00")) == live_bars.CurrentBarSession("trading", True)
    assert resolver.resolve(datetime.fromisoformat("2026-09-02T11:30:00+08:00")) == live_bars.CurrentBarSession("recess", False)
    assert resolver.resolve(datetime.fromisoformat("2026-09-02T13:00:00+08:00")) == live_bars.CurrentBarSession("trading", True)
    assert resolver.resolve(datetime.fromisoformat("2026-09-02T15:00:00+08:00")) == live_bars.CurrentBarSession("closed", False)

    monkeypatch.setattr(live_bars, "query_dataframe", lambda *args, **kwargs: pd.DataFrame([{"is_open": False}]))
    assert resolver.resolve(datetime.fromisoformat("2026-09-05T13:30:00+08:00")) == live_bars.CurrentBarSession("closed", False)


def test_current_bar_gateway_fails_closed_when_clock_health_is_unhealthy() -> None:
    current = datetime.fromisoformat("2026-09-02T13:30:08+08:00")

    class _ClockHealth:
        @staticmethod
        def assert_healthy():
            raise live_bars.LiveClockUnhealthy("clock skew exceeds tolerance")

    with pytest.raises(live_bars.LiveClockUnhealthy, match="skew"):
        live_bars.QuoteMuxWorkerGateway(session_resolver=_ActiveSessionResolver(), clock_health=_ClockHealth()).get_current_quotes(
            live_bars.CurrentBarRequest(("600519",), "1m", 1, "none", current)
        )


def test_current_bar_http_response_exposes_a_stable_clock_health_error(monkeypatch) -> None:
    class _ClockBrokenGateway:
        @staticmethod
        def get_current_quotes(request):
            del request
            raise live_bars.LiveClockUnhealthy("clock skew exceeds tolerance")

    monkeypatch.setattr(live_bars, "_GATEWAY", _ClockBrokenGateway())

    response = TestClient(app).get("/api/stocks/quotes", params={"code": "600519", "freq": "1m", "datetime": "now"})

    assert response.status_code == 503
    assert response.json()["code"] == "LIVE_CLOCK_UNHEALTHY"


def test_current_bar_gateway_batches_codes_with_independent_mixed_outcomes(monkeypatch) -> None:
    current = datetime.fromisoformat("2026-09-02T13:30:08+08:00")

    def fake_run(*args, **kwargs):
        code = json.loads(kwargs["input"])["codes"][0]
        assert len(json.loads(kwargs["input"])["codes"]) == 1
        if code == "000001":
            return subprocess.CompletedProcess(args=args[0], returncode=0, stdout=json.dumps({"items": [], "errors": [{"code": code, "message": "provider unavailable"}]}), stderr="")
        payload = {
            "items": [{
                "code": code, "trade_time": "2026-09-02T13:30:00+08:00", "freq": "1m",
                "open": 1400.0, "high": 1401.0, "low": 1399.0, "close": 1400.5,
                "volume": 1200, "amount": 1680600.0, "adjust": "none", "is_suspended": False, "is_st": False,
                "interval_start": "2026-09-02T13:30:00+08:00", "interval_end": "2026-09-02T13:31:00+08:00",
                "is_final": False, "observed_at": "2026-09-02T13:30:08+08:00", "last_trade_at": "2026-09-02T13:30:00+08:00",
                "provider": "mootdx", "source_semantics": "native", "observation_version": code,
                "freshness_ms": 0, "degraded": False, "market_status": "trading",
            }], "errors": [],
        }
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(live_bars.subprocess, "run", fake_run)

    result = _active_gateway().get_current_quotes(live_bars.CurrentBarRequest(("600519", "000001"), "1m", 1, "none", current))

    assert [item.code for item in result.items] == ["600519"]
    assert result.errors == [{"code": "000001", "message": "provider unavailable"}]


def test_current_bar_request_rejects_more_than_twenty_unique_codes() -> None:
    codes = ",".join(f"{number:06d}" for number in range(1, 22))

    with pytest.raises(ValueError, match="at most 20"):
        live_bars.build_current_bar_request(
            code="", codes=codes, freq="1m", count=1, adjust="none",
            trade_date="", start_date="", end_date="", start_time="", end_time="",
            effective_now=datetime.fromisoformat("2026-09-02T13:30:08+08:00"),
        )
