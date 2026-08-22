from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from fastapi import HTTPException


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from runtime_paths import configure_python_path


configure_python_path()

from app import app
from quotemux.models import StockQuotesMeta, StockQuotesQueryResult, TradingCalendarItem
from quotemux.reports import ContractReport
from quotemux.requests.stocks import StockQuotesRequest
from quotemux.settings import QuoteMuxSettings
from quotemux.stocks import QuoteMuxStocks
from services import market_data_version, stocks


def _version_frame(mutation: str = "42") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"source": "fact.stock_daily_1d", "row_count": "10", "watermark": "2026-08-07", "mutation": mutation},
            {"source": "fact.stock_financial_pit_factor", "row_count": "10", "watermark": "2026-08-07", "mutation": mutation},
            {"source": "fact.stock_listing_board_history", "row_count": "10", "watermark": "2026-08-07", "mutation": mutation},
            {"source": "fact.stock_market_indicators_daily", "row_count": "10", "watermark": "2026-08-07", "mutation": mutation},
            {"source": "fact.stock_money_flow_daily", "row_count": "10", "watermark": "2026-08-07", "mutation": mutation},
            {"source": "fact.stock_price_band_daily", "row_count": "10", "watermark": "2026-08-07", "mutation": mutation},
            {"source": "fact.concept_daily_1d", "row_count": "10", "watermark": "2026-08-07", "mutation": mutation},
            {"source": "audit.stock_bar_1m_write_event", "row_count": "3", "watermark": "2026-08-07 15:00:00", "mutation": mutation},
            {"source": "public.capability_cache_rows.market_inputs", "row_count": "3", "watermark": "2026-08-07 20:00:00", "mutation": mutation},
            {"source": "ref.concept", "row_count": "4", "watermark": "2026-08-07 20:00:00", "mutation": mutation},
            {"source": "ref.concept_stock_membership", "row_count": "20", "watermark": "2026-08-07", "mutation": mutation},
            {"source": "ref.stock", "row_count": "2", "watermark": "2020-01-01", "mutation": mutation},
            {"source": "ref.trade_calendar", "row_count": "5", "watermark": "2026-12-31", "mutation": mutation},
            {"source": "schema.ref.concept_stock_membership.pit", "row_count": "2", "watermark": "knowledge_time:timestamp with time zone,knowledge_time_status:text", "mutation": "0"},
        ]
    )


def test_market_data_version_changes_with_market_fact_mutation(monkeypatch) -> None:
    monkeypatch.setattr(market_data_version, "query_dataframe", lambda *_: _version_frame("42"))
    first = market_data_version.current_market_data_version()
    monkeypatch.setattr(market_data_version, "query_dataframe", lambda *_: _version_frame("43"))
    second = market_data_version.current_market_data_version()

    assert first.startswith("mhf-v1-")
    assert first != second


def test_market_data_version_uses_triggered_state_without_full_scan(monkeypatch) -> None:
    queries: list[str] = []

    def query(query_text: str, *_args) -> pd.DataFrame:
        queries.append(query_text)
        if query_text == market_data_version._VERSION_STATE_QUERY:
            return pd.DataFrame([{"baseline_id": "baseline-a", "generation": 7}])
        raise AssertionError("trigger-backed state must avoid the full fingerprint scan")

    monkeypatch.setattr(market_data_version, "query_dataframe", query)
    first = market_data_version.current_market_data_version()
    second = market_data_version.current_market_data_version()

    assert first == second
    assert queries == [market_data_version._VERSION_STATE_QUERY, market_data_version._VERSION_STATE_QUERY]


def test_market_data_version_changes_with_trigger_generation(monkeypatch) -> None:
    state = {"generation": 7}

    def query(query_text: str, *_args) -> pd.DataFrame:
        assert query_text == market_data_version._VERSION_STATE_QUERY
        return pd.DataFrame([{"baseline_id": "baseline-a", "generation": state["generation"]}])

    monkeypatch.setattr(market_data_version, "query_dataframe", query)
    first = market_data_version.current_market_data_version()
    state["generation"] = 8
    second = market_data_version.current_market_data_version()

    assert first != second


def test_market_data_version_rejects_missing_and_stale_request(monkeypatch) -> None:
    monkeypatch.setattr(market_data_version, "current_market_data_version", lambda: "mhf-v1-current")

    with pytest.raises(HTTPException, match="市场查询必须携带"):
        market_data_version.require_market_data_version("")
    with pytest.raises(HTTPException, match="请求版本已失效"):
        market_data_version.require_market_data_version("mhf-v1-stale")
    assert market_data_version.require_market_data_version("mhf-v1-current") == "mhf-v1-current"


def test_quote_post_contract_uses_body_version_and_openapi_discovery() -> None:
    from fastapi.testclient import TestClient

    client = TestClient(app)
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["openapi_url"] == "/api/openapi.json"
    schema = client.get(health.json()["openapi_url"]).json()
    operation = schema["paths"]["/api/stocks/quotes/query"]["post"]
    payload_schema = schema["components"]["schemas"]["StockQuotesQueryPayload"]

    assert operation.get("parameters", []) == []
    assert "data_version" in payload_schema["required"]
    assert payload_schema["examples"][0]["data_version"] == "mhf-v1-from-api-health"


def test_quotes_api_requires_matching_version_and_rejects_incomplete_window(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    calls: list[str] = []
    monkeypatch.setattr(stocks, "require_market_data_version", lambda value: value if value == "mhf-v1-current" else (_ for _ in ()).throw(HTTPException(status_code=409, detail="stale")))
    monkeypatch.setattr(
        stocks._QUOTEMUX.stocks,
        "get_quotes_query_result",
        lambda request: calls.append(request.data_version) or StockQuotesQueryResult(items=[], meta=StockQuotesMeta(data_version=request.data_version, total_rows=0, returned_rows=0, complete=False, truncated=False)),
    )

    response = TestClient(app).post(
        "/api/stocks/quotes/query",
        json={"codes": ["600000"], "freq": "1d", "trade_date": "2026-08-07", "data_version": "mhf-v1-current"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MARKET_DATA_INCOMPLETE"
    assert calls == ["mhf-v1-current"]


def test_versioned_daily_query_never_uses_provider_to_fill_gaps(monkeypatch) -> None:
    import quotemux.stocks as quotemux_stocks

    monkeypatch.setattr(quotemux_stocks, "get_local_stock_quotes", lambda *_: [])
    monkeypatch.setattr(quotemux_stocks, "_stock_listing_windows", lambda *_: {})
    monkeypatch.setattr(quotemux_stocks, "_base_source_report", lambda *args: ContractReport.empty("stocks.quotes.daily"))
    monkeypatch.setattr(
        quotemux_stocks,
        "get_local_trading_calendar",
        lambda *_: [TradingCalendarItem(exchange="SSE", trade_date="2026-08-07", is_open=True)],
    )
    monkeypatch.setattr(quotemux_stocks, "execute_capability_query", lambda *_: (_ for _ in ()).throw(AssertionError("版本化读取不能调用 provider")))

    result, _ = QuoteMuxStocks(QuoteMuxSettings(enabled_sources=())).get_quotes_query_result_with_report(
        StockQuotesRequest(codes=["600000"], freq="1d", trade_date="2026-08-07", data_version="mhf-v1-current")
    )

    assert result.meta.data_version == "mhf-v1-current"
    assert result.meta.complete is False
    assert result.meta.codes[0].missing_trade_dates == ["2026-08-07"]
