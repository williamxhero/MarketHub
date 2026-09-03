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


def test_stock_quotes_openapi_documents_current_bar_contract() -> None:
    schema = TestClient(app).get("/api/openapi.json").json()
    operation = schema["paths"]["/api/stocks/quotes"]["get"]
    description = operation["description"]

    assert "datetime=now" in description
    assert "live.stock_bar_selected" in description
    assert "fact.stock_bar_1m" in description
    assert "fact.stock_bar_30m" in description
    assert "is_final=false" in description
    assert "完整 1m 前缀" in description

    parameters = {item["name"]: item for item in operation["parameters"]}
    assert "freq=1m 或 freq=30m" in parameters["datetime"]["description"]
    assert "count=1" in parameters["datetime"]["description"]
    assert "当前交易周期" in parameters["datetime"]["description"]

    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert "additionalProperties" not in response_schema
    response_refs = {item["$ref"] for item in response_schema["oneOf"]}
    assert response_refs == {
        "#/components/schemas/CurrentStockQuotesQueryResult",
        "#/components/schemas/StockQuotesVersionedQueryResult",
    }

    item_properties = schema["components"]["schemas"]["CurrentStockQuoteItem"]["properties"]
    for field in (
        "interval_start",
        "interval_end",
        "is_final",
        "observed_at",
        "last_trade_at",
        "provider",
        "source_semantics",
        "observation_version",
        "freshness_ms",
        "degraded",
        "market_status",
    ):
        assert item_properties[field]["description"]

    meta_properties = schema["components"]["schemas"]["CurrentStockQuotesMeta"]["properties"]
    assert meta_properties["effective_now"]["description"]
    assert meta_properties["historical_dataset_version"]["description"]


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


def test_current_period_with_missing_elapsed_inputs_has_a_stable_public_error(monkeypatch) -> None:
    class _IncompleteGateway:
        @staticmethod
        def get_current_quotes(request):
            del request
            raise live_bars.LiveBarDataIncomplete("current 30m Bar has unexplained elapsed minutes: 2026-09-02T13:30:00+08:00")

    monkeypatch.setattr(live_bars, "_GATEWAY", _IncompleteGateway())

    response = TestClient(app).get(
        "/api/stocks/quotes",
        params={"code": "600519", "freq": "30m", "datetime": "now"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "LIVE_BAR_DATA_INCOMPLETE"


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
    assert captured["kwargs"]["timeout"] == 15


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


def test_current_bar_request_enforces_the_deployed_liquid_stock_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("MHK_LIVE_ALLOWED_CODES", "600519,000001")

    with pytest.raises(ValueError, match="allowlist"):
        live_bars.build_current_bar_request(
            code="600000", codes="", freq="1m", count=1, adjust="none",
            trade_date="", start_date="", end_date="", start_time="", end_time="",
            effective_now=datetime.fromisoformat("2026-09-02T13:30:08+08:00"),
        )


def test_current_30m_bar_is_derived_only_after_every_elapsed_minute_is_accounted_for(monkeypatch) -> None:
    current = datetime.fromisoformat("2026-09-02T13:32:08+08:00")
    worker_payload = {
        "items": [{
            "code": "600519", "trade_time": "2026-09-02T13:32:00+08:00", "freq": "1m",
            "open": 1401.5, "high": 1403.0, "low": 1401.0, "close": 1402.5,
            "volume": 130, "amount": 182325.0, "adjust": "none", "is_suspended": False, "is_st": False,
            "interval_start": "2026-09-02T13:32:00+08:00", "interval_end": "2026-09-02T13:33:00+08:00",
            "is_final": False, "observed_at": current.isoformat(), "last_trade_at": "2026-09-02T13:32:00+08:00",
            "provider": "mootdx", "source_semantics": "native", "observation_version": "live-1332",
            "freshness_ms": 0, "degraded": False, "market_status": "trading",
        }], "errors": [], "diagnostics": [],
    }
    monkeypatch.setattr(
        live_bars.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0], returncode=0, stdout=json.dumps(worker_payload), stderr=""),
    )

    class _ElapsedMinuteReader:
        @staticmethod
        def read_finalized(code, expected_starts):
            assert code == "600519"
            assert expected_starts == (
                "2026-09-02T13:30:00+08:00",
                "2026-09-02T13:31:00+08:00",
            )
            return [
                {"interval_start": expected_starts[0], "open": 1400.0, "high": 1401.0, "low": 1399.0, "close": 1400.5, "volume": 100, "amount": 140050.0, "observation_version": "final-1330"},
                {"interval_start": expected_starts[1], "open": 1400.5, "high": 1402.0, "low": 1400.0, "close": 1401.5, "volume": 120, "amount": 168180.0, "observation_version": "final-1331"},
            ]

    def derive(code, expected_starts, minute_bars):
        assert code == "600519"
        assert expected_starts == [
            "2026-09-02T13:30:00+08:00",
            "2026-09-02T13:31:00+08:00",
            "2026-09-02T13:32:00+08:00",
        ]
        assert [item["interval_start"] for item in minute_bars] == expected_starts
        return {"code": code, "interval_start": expected_starts[0], "open": 1400.0, "high": 1403.0, "low": 1399.0, "close": 1402.5, "volume": 350, "amount": 490555.0, "source_semantics": "derived"}

    gateway = live_bars.QuoteMuxWorkerGateway(
        session_resolver=_ActiveSessionResolver(),
        elapsed_minute_reader=_ElapsedMinuteReader(),
        current_period_deriver=derive,
    )

    result = gateway.get_current_quotes(live_bars.CurrentBarRequest(("600519",), "30m", 1, "none", current))

    assert result.items[0].freq == "30m"
    assert result.items[0].interval_start == "2026-09-02T13:30:00+08:00"
    assert result.items[0].interval_end == "2026-09-02T14:00:00+08:00"
    assert result.items[0].is_final is False
    assert result.items[0].source_semantics == "derived"
    assert result.items[0].volume == 350.0


def test_current_30m_bar_prefers_a_committed_native_worker_bar(monkeypatch) -> None:
    current = datetime.fromisoformat("2026-09-02T13:32:08+08:00")
    native = _current_result().items[0].model_copy(update={
        "freq": "30m", "trade_time": "2026-09-02T13:30:00+08:00",
        "interval_start": "2026-09-02T13:30:00+08:00", "interval_end": "2026-09-02T14:00:00+08:00",
        "observed_at": current.isoformat(), "provider": "mootdx", "observation_version": "native-30m",
    })
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        live_bars.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(json.loads(kwargs["input"])) or subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=json.dumps({"items": [native.model_dump()], "errors": [], "diagnostics": []}), stderr=""
        ),
    )
    gateway = live_bars.QuoteMuxWorkerGateway(
        session_resolver=_ActiveSessionResolver(),
        elapsed_minute_reader=lambda *args: pytest.fail("native 30m must not read 1m inputs"),
    )

    result = gateway.get_current_quotes(live_bars.CurrentBarRequest(("600519",), "30m", 1, "none", current))

    assert result.items[0].provider == "mootdx"
    assert result.items[0].source_semantics == "native"
    assert calls == [{"codes": ["600519"], "freq": "30m", "effective_now": current.isoformat()}]


def test_current_30m_bar_refuses_partial_elapsed_minute_input(monkeypatch) -> None:
    current = datetime.fromisoformat("2026-09-02T13:32:08+08:00")

    class _ElapsedMinuteReader:
        @staticmethod
        def read_finalized(code, expected_starts):
            del code, expected_starts
            return []

    gateway = live_bars.QuoteMuxWorkerGateway(
        session_resolver=_ActiveSessionResolver(),
        elapsed_minute_reader=_ElapsedMinuteReader(),
        current_period_deriver=lambda *args: pytest.fail("partial period must not be derived"),
    )
    current_item = _current_result().items[0].model_copy(update={
        "trade_time": "2026-09-02T13:32:00+08:00",
        "interval_start": "2026-09-02T13:32:00+08:00",
        "interval_end": "2026-09-02T13:33:00+08:00",
        "observed_at": current.isoformat(),
    })
    worker_payload = {"items": [current_item.model_dump()], "errors": [], "diagnostics": []}
    monkeypatch.setattr(
        live_bars.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0], returncode=0, stdout=json.dumps(worker_payload), stderr=""),
    )

    with pytest.raises(live_bars.LiveBarDataIncomplete, match="13:30"):
        gateway.get_current_quotes(live_bars.CurrentBarRequest(("600519",), "30m", 1, "none", current))


def test_health_keeps_live_bar_readiness_separate_from_historical_versions(monkeypatch) -> None:
    monkeypatch.setattr(live_bars, "get_current_bar_health", lambda: {"status": "warning", "capabilities": ["1m"], "clock": {"status": "healthy"}})

    payload = TestClient(app).get("/api/health").json()

    assert payload["live_bars"] == {"status": "warning", "capabilities": ["1m"], "clock": {"status": "healthy"}}
    assert "dataset_versions" in payload


def test_current_bar_health_does_not_treat_active_staging_as_finalizer_backlog(monkeypatch) -> None:
    monkeypatch.setattr(
        live_bars,
        "query_dataframe",
        lambda *args, **kwargs: pd.DataFrame([{
            "staged_count": 2, "overdue_staged_count": 0, "oldest_overdue_interval": None,
            "last_selected_at": pd.Timestamp("2026-09-03 10:07:45+08:00"), "failed_count": 0,
            "staged_1m_count": 1, "staged_30m_count": 1, "failed_1m_count": 0, "failed_30m_count": 0,
            "last_selected_1m_at": pd.Timestamp("2026-09-03 10:07:45+08:00"),
            "last_selected_30m_at": pd.Timestamp("2026-09-03 10:07:45+08:00"),
        }]),
    )

    health = live_bars.get_current_bar_health()

    assert health["status"] == "healthy"
    assert health["worker"]["deadline_seconds"] == 15
    assert health["finalizer"] == {"status": "ready", "staged_count": 2, "overdue_staged_count": 0, "oldest_overdue_interval": "", "failed_count": 0}


def test_finalized_history_reader_preserves_naive_database_bar_time_as_shanghai(monkeypatch) -> None:
    monkeypatch.setattr(
        live_bars,
        "query_dataframe",
        lambda *args, **kwargs: pd.DataFrame([{
            "market": "SHSE", "code": "600519", "bar_time": pd.Timestamp("2026-09-02 15:00:00"),
            "open": 1400.0, "high": 1401.0, "low": 1399.0, "close": 1400.5, "volume": 1200, "amount": 1_680_600.0,
        }]),
    )
    request = live_bars.CurrentBarRequest(("600519",), "1m", 1, "none", datetime.fromisoformat("2026-09-02T21:00:00+08:00"))

    result = live_bars.PostgresFinalizedCurrentBarReader().get_latest_finalized(request, "closed")

    assert result.items[0].interval_start == "2026-09-02T15:00:00+08:00"
