from __future__ import annotations

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
from main import app
from services import daily_window


@pytest.fixture(autouse=True)
def _reset_coverage_cache() -> None:
    daily_window.clear_coverage_cache()


def _payload(**updates: object) -> StockDailyWindowQueryPayload:
    values: dict[str, object] = {
        "data_version": "mhf-v1-test",
        "freq": "1d",
        "universe": "codes",
        "codes": ["600000", "000001"],
        "start_date": "2021-01-01",
        "end_date": "2021-01-31",
        "page_size": 1,
    }
    values.update(updates)
    return StockDailyWindowQueryPayload.model_validate(values)


def _coverage_row(
    *,
    missing_rows: int = 0,
    duplicate_rows: int = 0,
    unknown_codes: list[str] | None = None,
) -> pd.DataFrame:
    coverage = [
        {
            "code": "000001",
            "expected_rows": 1,
            "actual_rows": 1,
            "missing_rows": 0,
            "missing_trade_dates": [],
            "complete": True,
        },
        {
            "code": "600000",
            "expected_rows": 1,
            "actual_rows": 1 - missing_rows,
            "missing_rows": missing_rows,
            "missing_trade_dates": ["2021-01-05"] if missing_rows else [],
            "complete": missing_rows == 0,
        },
    ]
    return pd.DataFrame(
        [
            {
                "universe_size": 2,
                "expected_total": 2,
                "actual_total": 2 - missing_rows,
                "missing_total": missing_rows,
                "duplicate_total": duplicate_rows,
                "coverage_json": json.dumps(coverage),
                "unknown_codes_json": json.dumps(unknown_codes or []),
            }
        ]
    )


def _page_row(code: str, trade_time: str, *, has_more: bool) -> pd.DataFrame:
    item = {
        "code": code,
        "trade_time": trade_time,
        "freq": "1d",
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "pre_close": 1.0,
        "change": 0.5,
        "pct_chg": 50.0,
        "volume": 100.0,
        "amount": 150.0,
        "adjust": "none",
        "is_suspended": False,
        "is_st": False,
    }
    return pd.DataFrame(
        [
            {
                "items_json": json.dumps([item], separators=(",", ":")),
                "returned_rows": 1,
                "has_more": has_more,
                "last_trade_time": trade_time,
                "last_code": code,
            }
        ]
    )


def test_payload_enforces_daily_universe_and_dates() -> None:
    assert _payload(codes=["600000", "600000"]).codes == ["600000"]
    with pytest.raises(ValidationError):
        _payload(data_version="")
    with pytest.raises(ValidationError):
        _payload(freq="1m")
    with pytest.raises(ValidationError):
        _payload(codes=[])
    with pytest.raises(ValidationError):
        _payload(universe="all_a", codes=["600000"])
    with pytest.raises(ValidationError):
        _payload(codes=["SH.600000"])
    with pytest.raises(ValidationError):
        _payload(start_date="2021-02-01", end_date="2021-01-31")
    with pytest.raises(ValidationError):
        _payload(page_size=100001)


def test_build_response_uses_keyset_cursor_and_gzip_without_changing_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daily_window, "require_market_data_version", lambda value: value)

    def query(query_text: str, params: tuple[object, ...]) -> pd.DataFrame:
        if "expected_state as materialized" in query_text:
            return _coverage_row()
        cursor_trade_time = params[7]
        if cursor_trade_time is None:
            return _page_row("000001", "2021-01-04", has_more=True)
        assert cursor_trade_time == "2021-01-04"
        assert params[9] == "000001"
        return _page_row("600000", "2021-01-05", has_more=False)

    monkeypatch.setattr(daily_window, "query_dataframe", query)
    payload = _payload()
    identity = daily_window.build_response(payload, False)
    compressed = daily_window.build_response(payload, True)
    assert identity.headers["Content-Encoding"] == "identity"
    assert compressed.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(compressed.content) == identity.content
    first = json.loads(identity.content)
    assert first["items"][0]["code"] == "000001"
    assert first["meta"]["complete"] is True
    assert first["meta"]["request_complete"] is True
    assert first["meta"]["page_complete"] is True
    assert first["meta"]["delivery_complete"] is False
    assert first["meta"]["truncated"] is False
    assert first["meta"]["total_rows"] == 2
    assert "coverage_db" in identity.headers["Server-Timing"]

    second_payload = payload.model_copy(update={"cursor": first["meta"]["next_cursor"]})
    second = json.loads(daily_window.build_response(second_payload, False).content)
    assert second["items"][0]["code"] == "600000"
    assert second["meta"]["next_cursor"] is None
    assert second["meta"]["delivery_complete"] is True


def test_cursor_is_bound_to_query_fingerprint() -> None:
    payload = _payload()
    cursor = daily_window._encode_cursor(payload, "2021-01-04", "000001")
    changed = payload.model_copy(update={"end_date": "2021-02-01", "cursor": cursor})
    with pytest.raises(HTTPException) as error:
        daily_window._decode_cursor(changed)
    assert error.value.status_code == 409


def test_missing_coverage_fails_before_page_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daily_window, "require_market_data_version", lambda value: value)
    calls: list[str] = []

    def query(query_text: str, _: tuple[object, ...]) -> pd.DataFrame:
        calls.append(query_text)
        if "expected_state as materialized" in query_text:
            return _coverage_row(missing_rows=1)
        raise AssertionError("page query must not run after incomplete coverage")

    monkeypatch.setattr(daily_window, "query_dataframe", query)
    with pytest.raises(HTTPException) as error:
        daily_window.build_response(_payload(), False)
    assert error.value.status_code == 409
    assert len(calls) == 1


def test_unknown_code_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daily_window, "require_market_data_version", lambda value: value)
    monkeypatch.setattr(
        daily_window,
        "query_dataframe",
        lambda *_: _coverage_row(unknown_codes=["999999"]),
    )
    with pytest.raises(HTTPException) as error:
        daily_window.build_response(_payload(), False)
    assert error.value.status_code == 409
    assert error.value.detail["details"]["unknown_codes"] == ["999999"]


def test_duplicate_daily_key_fails_before_page_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daily_window, "require_market_data_version", lambda value: value)
    monkeypatch.setattr(daily_window, "query_dataframe", lambda *_: _coverage_row(duplicate_rows=1))
    with pytest.raises(HTTPException) as error:
        daily_window.build_response(_payload(), False)
    assert error.value.status_code == 409
    assert error.value.detail["details"]["duplicate_rows"] == 1


def test_data_version_drift_after_page_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    version_checks = 0

    def require(value: str) -> str:
        nonlocal version_checks
        version_checks += 1
        if version_checks == 3:
            raise HTTPException(status_code=409, detail={"code": "MARKET_DATA_VERSION_MISMATCH"})
        return value

    monkeypatch.setattr(daily_window, "require_market_data_version", require)
    monkeypatch.setattr(
        daily_window,
        "query_dataframe",
        lambda query_text, _: _coverage_row() if "expected_state as materialized" in query_text else _page_row("000001", "2021-01-04", has_more=False),
    )
    with pytest.raises(HTTPException) as error:
        daily_window.build_response(_payload(), False)
    assert error.value.status_code == 409
    assert version_checks == 3


def test_v2_sql_is_read_only_and_deterministically_sorted() -> None:
    combined = f"{daily_window._COVERAGE_QUERY}\n{daily_window._PAGE_QUERY}".lower()
    for forbidden in ("insert into", "update ", "delete from", "capture", "provider"):
        assert forbidden not in combined
    assert "order by daily_rows.trade_date, daily_rows.code" in daily_window._PAGE_QUERY
    assert "order by delivered.trade_date, delivered.code" in daily_window._PAGE_QUERY
    assert "daily_rows.trade_date < universe.delisted_date" in daily_window._PAGE_QUERY
    assert "date '2021-11-15'" in daily_window._BASE_CTE


def test_v2_route_is_discoverable_and_returns_items_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(
        {
            "items": [],
            "meta": {
                "data_version": "mhf-v1-test",
                "total_rows": 0,
                "returned_rows": 0,
                "complete": True,
                "truncated": False,
                "page_complete": True,
                "request_complete": True,
                "delivery_complete": True,
                "next_cursor": None,
                "universe_kind": "codes",
                "universe_size": 1,
                "page_size": 50000,
                "coverage": [],
            },
        }
    ).encode()
    monkeypatch.setattr(
        daily_window,
        "build_response",
        lambda *_: daily_window.EncodedDailyWindowResponse(
            content=body,
            headers={"Content-Encoding": "identity", "Content-Length": str(len(body))},
        ),
    )
    client = TestClient(app)
    response = client.post(
        "/api/stocks/quotes/daily-window/query",
        json={
            "data_version": "mhf-v1-test",
            "freq": "1d",
            "universe": "codes",
            "codes": ["600000"],
            "start_date": "2021-01-01",
            "end_date": "2021-01-31",
        },
        headers={"Accept-Encoding": "identity"},
    )
    assert response.status_code == 200
    assert response.json()["meta"]["complete"] is True
    schema = client.get("/api/openapi.json").json()
    operation = schema["paths"]["/api/stocks/quotes/daily-window/query"]["post"]
    assert operation["requestBody"]["required"] is True
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("StockDailyWindowQueryResponse")
    assert "application/vnd.apache.arrow.stream" in operation["responses"]["200"]["content"]


def test_arrow_response_streams_fixed_schema_and_equivalent_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daily_window, "require_market_data_version", lambda value: value)
    monkeypatch.setattr(daily_window, "query_dataframe", lambda *_: _coverage_row())

    def stream(query: str, params: tuple[object, ...], *, batch_size: int):
        if "duplicate_rows" in query:
            rows = [
                {"code": "000001", "expected_rows": 1, "actual_rows": 1, "missing_rows": 0, "missing_trade_dates": [], "complete": True, "duplicate_rows": 0},
                {"code": "600000", "expected_rows": 1, "actual_rows": 1, "missing_rows": 0, "missing_trade_dates": [], "complete": True, "duplicate_rows": 0},
            ]
        elif "left join catalog" in query:
            rows = []
        elif "count(delivered.code)" in query:
            rows = [{"returned_rows": 1, "has_more": True, "last_trade_time": date(2021, 1, 4), "last_code": "000001"}]
        else:
            assert batch_size == daily_window.ARROW_RECORD_BATCH_ROWS
            rows = [{"code": "000001", "trade_date": date(2021, 1, 4), "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "pre_close": 1.0, "change": 0.5, "pct_chg": 50.0, "volume": 100.0, "amount": 150.0, "is_st": False}]
        if rows:
            yield rows

    monkeypatch.setattr(daily_window, "stream_query_batches", stream)
    prepared = daily_window.prepare_arrow_response(_payload())
    encoded = b"".join(prepared.body)
    reader = pa.ipc.open_stream(encoded)
    table = reader.read_all()
    meta = json.loads(reader.schema.metadata[b"markethub.meta"])
    assert table.to_pylist()[0]["code"] == "000001"
    assert table.to_pylist()[0]["trade_time"] == "2021-01-04"
    assert tuple(table.schema.names) == tuple(daily_window.ARROW_SCHEMA.names)
    assert meta["returned_rows"] == 1
    assert meta["coverage"][0]["complete"] is True
    assert meta["delivery_complete"] is False
    assert prepared.headers["X-MarketHub-Returned-Rows"] == "1"
    assert prepared.headers["X-MarketHub-Next-Cursor"] == meta["next_cursor"]


def test_arrow_body_close_closes_database_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daily_window, "require_market_data_version", lambda value: value)
    monkeypatch.setattr(daily_window, "query_dataframe", lambda *_: _coverage_row())
    page_stream_closed = False

    def stream(query: str, params: tuple[object, ...], *, batch_size: int):
        nonlocal page_stream_closed
        try:
            if "duplicate_rows" in query:
                yield [{"code": "600000", "expected_rows": 2, "actual_rows": 2, "missing_rows": 0, "missing_trade_dates": [], "complete": True, "duplicate_rows": 0}]
            elif "left join catalog" in query:
                return
            elif "count(delivered.code)" in query:
                yield [{"returned_rows": 2, "has_more": False, "last_trade_time": date(2021, 1, 5), "last_code": "600000"}]
            else:
                yield [{"code": "600000", "trade_date": date(2021, 1, 4), "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "pre_close": 1.0, "change": 0.5, "pct_chg": 50.0, "volume": 100.0, "amount": 150.0, "is_st": False}]
                yield [{"code": "600000", "trade_date": date(2021, 1, 5), "open": 1.1, "high": 2.1, "low": 0.6, "close": 1.6, "pre_close": 1.5, "change": 0.1, "pct_chg": 6.67, "volume": 110.0, "amount": 160.0, "is_st": False}]
        finally:
            if "select code,trade_date" in query:
                page_stream_closed = True

    monkeypatch.setattr(daily_window, "stream_query_batches", stream)
    body = daily_window.prepare_arrow_response(_payload(page_size=2)).body
    next(body)
    body.close()
    assert page_stream_closed is True


def test_arrow_sql_has_no_json_aggregation_or_pandas_materialization() -> None:
    combined = "\n".join(
        (daily_window._COVERAGE_ROWS_QUERY, daily_window._UNKNOWN_CODES_QUERY, daily_window._PAGE_META_QUERY, daily_window._PAGE_ROWS_QUERY)
    ).lower()
    assert "json_agg" not in combined
    assert "order by trade_date,code" in daily_window._PAGE_ROWS_QUERY
    assert daily_window.ARROW_RECORD_BATCH_ROWS == 8192


def test_daily_window_rejects_unknown_accept_before_query() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/stocks/quotes/daily-window/query",
        json={
            "data_version": "mhf-v1-test",
            "freq": "1d",
            "universe": "codes",
            "codes": ["600000"],
            "start_date": "2021-01-01",
            "end_date": "2021-01-31",
        },
        headers={"Accept": "text/plain"},
    )
    assert response.status_code == 406
    assert response.json()["code"] == "DAILY_WINDOW_MEDIA_TYPE_NOT_ACCEPTABLE"


def test_coverage_cache_is_keyed_by_immutable_data_version_and_window(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def load(payload: StockDailyWindowQueryPayload):
        nonlocal calls
        calls += 1
        return ({"universe_size": 1, "expected_total": 1, "actual_total": 1, "missing_total": 0, "duplicate_total": 0}, [{"code": "600000", "expected_rows": 1, "actual_rows": 1, "missing_rows": 0, "missing_trade_dates": [], "complete": True}])

    monkeypatch.setattr(daily_window, "_load_coverage_uncached", load)
    daily_window.clear_coverage_cache()
    first = _payload(data_version="mhf-v1-a", codes=["600000"], start_date="2021-01-01", end_date="2021-01-31")
    equivalent = first.model_copy(update={"page_size": 99, "cursor": None})
    next_version = first.model_copy(update={"data_version": "mhf-v1-b"})

    assert daily_window._cached_coverage(first) == daily_window._cached_coverage(equivalent)
    assert calls == 1
    daily_window._cached_coverage(next_version)
    assert calls == 2


def test_coverage_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        daily_window,
        "_load_coverage_uncached",
        lambda _payload: ({"universe_size": 1, "expected_total": 1, "actual_total": 1, "missing_total": 0, "duplicate_total": 0}, []),
    )
    daily_window.clear_coverage_cache()
    for index in range(daily_window.COVERAGE_CACHE_MAX_ENTRIES + 3):
        daily_window._cached_coverage(_payload(data_version=f"mhf-v1-{index}"))
    assert len(daily_window._COVERAGE_CACHE) == daily_window.COVERAGE_CACHE_MAX_ENTRIES
