from __future__ import annotations

"""T3 (#562): per-row adjustment factor opt-in on the formal daily-window query.

The unit tests below pin the response contract (opt-out stays byte-identical,
opt-in widens both encodings, a coverage shortfall fails closed with the
existing incomplete-coverage shape). The DB-backed test at the end is what
proves the version rule on real stored data: a factor write advances the
published `stock_daily_1d` version, and a request pinned to the previous
version then fails closed exactly like a daily-data drift.
"""

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

import json
import os
from datetime import date

from fastapi import HTTPException
import pandas as pd
import pyarrow as pa
import pytest

from routers.stock_quote_models import StockDailyWindowQueryPayload
from services import daily_window
from services.dataset_versions import STOCK_DAILY_DATASET_ID, current_dataset_version


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


def _coverage(missing_codes: list[str] | None = None) -> dict[str, object]:
    return {
        "universe_size": 1,
        "expected_total": 1,
        "actual_total": 1,
        "missing_total": 0,
        "duplicate_total": 0,
        "missing_adj_factor_codes": missing_codes or [],
    }


def _rows(include_adj_factor: bool, factor: object = 1.5) -> list[dict[str, object]]:
    row: dict[str, object] = {
        "code": "600000",
        "trade_date": date(2021, 1, 4),
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": 10.5,
        "pre_close": 10.0,
        "change": 0.5,
        "pct_chg": 5.0,
        "volume": 1000.0,
        "amount": 10500.0,
        "is_st": False,
    }
    if include_adj_factor:
        row["adj_factor"] = factor
    return [row]


def _install(monkeypatch: pytest.MonkeyPatch, *, missing_codes: list[str] | None = None) -> None:
    """Stub the coverage read and the factor-coverage probe for JSON tests."""
    monkeypatch.setattr(
        daily_window,
        "_load_coverage_uncached",
        lambda _payload: (
            {
                "universe_size": 1,
                "expected_total": 1,
                "actual_total": 1,
                "missing_total": 0,
                "duplicate_total": 0,
            },
            [
                {
                    "code": "600000",
                    "expected_rows": 1,
                    "actual_rows": 1,
                    "missing_rows": 0,
                    "missing_trade_dates": [],
                    "complete": True,
                }
            ],
        ),
    )
    monkeypatch.setattr(
        daily_window,
        "_missing_adj_factor_codes",
        lambda _payload: list(missing_codes or []),
    )


def test_payload_accepts_include_adj_factor_parameter() -> None:
    assert _payload(include_adj_factor=True).include_adj_factor is True
    assert _payload().include_adj_factor is False


def test_json_response_includes_adj_factor_only_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)

    def page_query(query: str, _params: tuple) -> pd.DataFrame:
        requested = "'adj_factor'" in query
        return pd.DataFrame(
            [
                {
                    "items_json": json.dumps(
                        [
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
                                **({"adj_factor": 1.5} if requested else {}),
                            }
                        ]
                    ),
                    "returned_rows": 1,
                    "has_more": False,
                    "last_trade_time": "2021-01-04",
                    "last_code": "600000",
                }
            ]
        )

    monkeypatch.setattr(daily_window, "query_dataframe", page_query)

    opted_in = daily_window.build_response(_payload(include_adj_factor=True), False)
    assert json.loads(opted_in.content)["items"][0]["adj_factor"] == 1.5

    opted_out = daily_window.build_response(_payload(), False)
    assert "adj_factor" not in json.loads(opted_out.content)["items"][0]


def test_opt_out_cache_key_and_cursor_are_unchanged_by_the_new_contract() -> None:
    """A plain request must keep the fingerprint, and so the cursor, it had."""
    plain = _payload()
    expected = {
        "freq": "1d",
        "universe": "codes",
        "codes": ["000001", "600000"],
        "start_date": "2021-01-01",
        "end_date": "2021-01-31",
    }
    import hashlib

    digest = hashlib.sha256(
        json.dumps(expected, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    assert daily_window._request_fingerprint(plain) == digest
    # The opt-in is a different item shape, so it must not share a cursor.
    assert daily_window._request_fingerprint(_payload(include_adj_factor=True)) != digest


def test_missing_adj_factors_fail_closed_with_the_incomplete_coverage_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, missing_codes=["600000"])

    with pytest.raises(HTTPException) as exc_info:
        daily_window.build_response(_payload(include_adj_factor=True), False)

    detail = exc_info.value.detail
    assert exc_info.value.status_code == 409
    assert detail["code"] == "MARKET_DATA_INCOMPLETE"
    assert detail["details"]["incomplete_codes"] == ["600000"]
    assert detail["details"]["missing_adj_factor_codes"] == ["600000"]


def test_arrow_response_widens_schema_only_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)

    def stream(query: str, _params: tuple, *, batch_size: int):
        if "count(delivered.code)" in query:
            yield [{"returned_rows": 1, "has_more": False, "last_trade_time": date(2021, 1, 4), "last_code": "600000"}]
        elif "select code,trade_date" in query:
            yield _rows(True)
        else:
            return

    monkeypatch.setattr(daily_window, "stream_query_batches", stream)

    plain = daily_window.prepare_arrow_response(_payload())
    plain_reader = pa.ipc.open_stream(b"".join(plain.body))
    plain_table = plain_reader.read_all()
    assert tuple(plain_table.schema.names) == tuple(daily_window.ARROW_SCHEMA.names)
    assert "adj_factor" not in plain_table.schema.names
    assert plain.headers["X-MarketHub-Arrow-Schema-Version"] == daily_window.ARROW_SCHEMA_VERSION

    opted = daily_window.prepare_arrow_response(_payload(include_adj_factor=True))
    opted_reader = pa.ipc.open_stream(b"".join(opted.body))
    opted_table = opted_reader.read_all()
    assert "adj_factor" in opted_table.schema.names
    assert opted_table.to_pylist()[0]["adj_factor"] == 1.5
    assert opted_table.to_pylist()[0]["close"] == 10.5
    assert opted.headers["X-MarketHub-Arrow-Schema-Version"] == daily_window.ARROW_ADJ_FACTOR_SCHEMA_VERSION
    # The advertised schema version must match the schema actually streamed.
    assert (
        opted_reader.schema.metadata[b"markethub.schema_version"].decode()
        == daily_window.ARROW_ADJ_FACTOR_SCHEMA_VERSION
    )


def test_arrow_factor_request_fails_closed_on_missing_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, missing_codes=["600000"])

    def stream(_query: str, _params: tuple, *, batch_size: int):
        raise AssertionError("no page may be streamed once factor coverage is known to be short")

    monkeypatch.setattr(daily_window, "stream_query_batches", stream)

    with pytest.raises(HTTPException) as exc_info:
        daily_window.prepare_arrow_response(_payload(include_adj_factor=True))
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "MARKET_DATA_INCOMPLETE"


def _database_configured() -> bool:
    if not all(
        os.getenv(name, "")
        for name in (
            "MARKETHUB_DB_HOST",
            "MARKETHUB_DB_PORT",
            "MARKETHUB_DB_NAME",
            "MARKETHUB_DB_USER",
            "MARKETHUB_DB_PASSWORD",
        )
    ):
        return False
    # The DB-backed tests below insert and mutate rows; refuse to run anywhere
    # that is not obviously an isolated test database.
    return "test" in os.getenv("MARKETHUB_DB_NAME", "")


def _connect_for_test():
    import psycopg

    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        autocommit=True,
    )


def _ensure_query_sources(connection, codes: tuple[str, ...], trade_dates: tuple[str, ...]) -> None:
    """Stand up the upstream-ingested sources the window query joins.

    `ref.trade_calendar` and `fact.stock_suspension_history` are populated by
    provider ingestion outside this repository, so no bootstrap path creates
    them; a fresh isolated database has neither. Creating them here keeps these
    tests runnable on a byte-for-byte fresh database instead of quietly
    depending on state some earlier test happened to leave behind.
    """
    connection.execute(
        "create table if not exists ref.trade_calendar (exchange text, trade_date date, is_open boolean, "
        "primary key (exchange, trade_date))"
    )
    connection.execute(
        "create table if not exists fact.stock_suspension_history ("
        "market text, code text, suspend_start_date date, suspend_end_date date, resume_date date, "
        "status text, source text, source_marker text, captured_at_utc timestamptz, data_version text, "
        "loaded_at timestamptz default now())"
    )
    for market, code in (("SHSE", value) for value in codes):
        connection.execute(
            "insert into ref.stock (market, code, name, listed_date) values (%s, %s, %s, date '1990-01-01') "
            "on conflict do nothing",
            (market, code, f"T3 probe {code}"),
        )
    for trade_day in trade_dates:
        connection.execute(
            "insert into ref.trade_calendar (exchange, trade_date, is_open) values ('SHSE', %s::date, true) "
            "on conflict do nothing",
            (trade_day,),
        )


def _stub_daily_coverage(monkeypatch: pytest.MonkeyPatch, rows: list[tuple[str, int]]) -> None:
    """Serve the daily-coverage read from the rows the test actually declared.

    The shared isolated database accumulates rows across the whole suite, and
    the coverage read model is built for the whole fact table -- rebuilding it
    per test would race the suite's other cache users. These tests also probe a
    window that deliberately contains only one code's rows, so the read model
    legitimately has no entry for the current dataset version until the next
    full-table build. What is under test here is the factor gate, the version
    gate and the delivered rows, all of which stay real: the row count below is
    read from the database, not asserted from a constant.
    """
    def load(payload):
        summary = {
            "expected_total": sum(expected for _, expected in rows),
            "actual_total": sum(expected for _, expected in rows),
            "missing_total": 0,
            "duplicate_total": 0,
            "universe_size": len(rows),
        }
        coverage = [
            {
                "code": code,
                "expected_rows": expected,
                "actual_rows": expected,
                "missing_rows": 0,
                "missing_trade_dates": [],
                "complete": True,
            }
            for code, expected in rows
        ]
        return summary, coverage

    monkeypatch.setattr(daily_window, "_load_coverage_uncached", load)


def _row_count(code: str, start: str, end: str) -> int:
    with _connect_for_test() as connection:
        row = connection.execute(
            "select count(*) from fact.stock_daily_1d where code = %s and trade_date between %s::date and %s::date "
            "and not coalesce(is_suspended, false)",
            (code, start, end),
        ).fetchone()
    return int(row[0])


def _seed_daily_rows(connection, *, code: str, trade_dates: tuple[str, ...], statement: str) -> None:
    """Replace one code's rows with an exactly known set.

    The shared isolated database accumulates rows across the whole suite, so a
    probe deletes its own code first and then inserts only what it declares.
    Without this, a window that happens to span the calendar range some earlier
    test left behind raises the daily-coverage gate before the factor gate the
    test is about.
    """
    connection.execute("delete from fact.stock_daily_1d where code = %s", (code,))
    _ensure_query_sources(connection, (code,), trade_dates)
    for trade_day in trade_dates:
        connection.execute(statement, (trade_day,))


@pytest.mark.skipif(not _database_configured(), reason="requires an isolated PostgreSQL test database")
def test_factor_write_advances_the_pinned_dataset_version_and_a_stale_pin_fails_closed() -> None:
    from quotemux.fact_ref_writes import _upsert_stock_adj_factors
    from services.dataset_versions import require_dataset_version

    trade_day = "2024-01-02"
    with _connect_for_test() as connection:
        _seed_daily_rows(
            connection,
            code="601988",
            trade_dates=(trade_day,),
            statement=(
                "insert into fact.stock_daily_1d "
                "(market, code, trade_date, open, high, low, close, pre_close, volume, amount, is_suspended, is_st, adj_factor) "
                "values ('SHSE', '601988', %s::date, 1, 1, 1, 1, 1, 1, 1, false, false, null)"
            ),
        )
    baseline = current_dataset_version(STOCK_DAILY_DATASET_ID)
    assert baseline

    class _Item:
        code = "601988"
        trade_date = trade_day.replace("-", "")
        adj_factor = 7.5

    assert _upsert_stock_adj_factors([_Item()]) is True

    after = current_dataset_version(STOCK_DAILY_DATASET_ID)
    # The factor column lives on a version-registered table, so writing it must
    # move the version a caller pins -- otherwise a caller could hold a version
    # whose factor data changed underneath it.
    assert after != baseline

    # And a caller still pinning the previous version is rejected, not served.
    with pytest.raises(HTTPException) as exc_info:
        require_dataset_version(STOCK_DAILY_DATASET_ID, requested_dataset_version=baseline)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "DATASET_VERSION_STALE"
    assert exc_info.value.detail["details"]["current_version"] == after


# Real stored rows for SHSE 603093 around its 2026-08-10 ex-rights/dividend
# event, read back from the production fact table with no editing: the factor
# steps 1.0255 -> 1.488 while the raw close drops 18.05 -> 12.50, and
# close x factor is continuous across the event.
_EX_DATE_ROWS: tuple[tuple[object, ...], ...] = (
    ("2026-08-06", 18.11, 18.44, 17.82, 18.03, 18.19, 10_658_400.0, 192_775_852.0, 1.0255),
    ("2026-08-07", 18.02, 18.15, 17.69, 18.05, 18.03, 9_649_700.0, 173_109_239.0, 1.0255),
    ("2026-08-10", 12.41, 12.58, 12.32, 12.50, 12.44, 11_528_375.0, 143_757_556.0, 1.488),
    ("2026-08-11", 12.50, 12.51, 12.16, 12.18, 12.50, 10_654_980.0, 130_847_137.0, 1.488),
    ("2026-08-12", 12.70, 13.00, 12.27, 12.53, 12.18, 19_653_787.0, 245_916_376.0, 1.488),
)


@pytest.mark.skipif(not _database_configured(), reason="requires an isolated PostgreSQL test database")
def test_raw_close_gaps_across_the_ex_date_while_the_adjusted_series_stays_continuous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: a factor request on real stored data is continuous across an ex-date.

    Tushare re-bases the factor on the ex-date (1.0255 -> 1.488) to reproduce the
    corporate action, so the caller-visible continuity identity is that a raw
    close step is cancelled by the delivered factor step: closing 18.05 and
    reopening at 12.50 is a 30.7% raw gap and a 0.5% move once the factor that
    shipped with those two rows is applied. Without the delivered factor the
    caller can only ever see the 30.7% gap, which is exactly the failure this
    opt-in exists to remove.
    """
    trade_dates = tuple(row[0] for row in _EX_DATE_ROWS)
    with _connect_for_test() as connection:
        connection.execute("delete from fact.stock_daily_1d where code = '603093'")
        _ensure_query_sources(connection, ("603093",), trade_dates)
        for row in _EX_DATE_ROWS:
            connection.execute(
                "insert into fact.stock_daily_1d "
                "(market, code, trade_date, open, high, low, close, pre_close, volume, amount, is_suspended, is_st, adj_factor) "
                "values ('SHSE', '603093', %s::date, %s, %s, %s, %s, %s, %s, %s, false, false, %s)",
                row,
            )

    _stub_daily_coverage(monkeypatch, [("603093", _row_count("603093", "2026-08-06", "2026-08-12"))])
    payload = _payload(
        universe="codes",
        codes=["603093"],
        start_date="2026-08-06",
        end_date="2026-08-12",
        page_size=100,
        meta_detail="full",
        include_adj_factor=True,
    ).model_copy(update={"dataset_version": current_dataset_version(STOCK_DAILY_DATASET_ID)})
    items = json.loads(daily_window.build_response(payload, False).content)["items"]
    assert [item["trade_time"] for item in items] == [row[0] for row in _EX_DATE_ROWS]
    assert all(item["adj_factor"] is not None for item in items)

    ex_index = [row[0] for row in _EX_DATE_ROWS].index("2026-08-10")
    previous, event = items[ex_index - 1], items[ex_index]

    raw_step = abs(event["close"] / previous["close"] - 1)
    bridged_step = abs(event["close"] * event["adj_factor"] / (previous["close"] * previous["adj_factor"]) - 1)

    # Raw close gaps hard across the ex-date...
    assert raw_step > 0.30
    # ...while the delivered factors reduce the same step to under a percent,
    # and by more than an order of magnitude. The residual is the vendor's own
    # settlement rounding, not a free pass: the unscaled step is thirty times
    # larger.
    assert bridged_step < 0.01
    assert raw_step / bridged_step > 10

    # On ordinary days the factors are flat, so the opt-in must not perturb the
    # series a caller already reads.
    assert previous["adj_factor"] != event["adj_factor"]
    early = items[0]
    for later in items[1:ex_index]:
        assert abs(later["close"] * later["adj_factor"] / (early["close"] * early["adj_factor"]) - 1) < 0.01


@pytest.mark.skipif(not _database_configured(), reason="requires an isolated PostgreSQL test database")
def test_factor_coverage_shortfall_in_the_window_fails_the_real_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: a real row with no factor must fail closed, never zero-fill."""
    gap_days = ("2026-08-06", "2026-08-07")
    with _connect_for_test() as connection:
        connection.execute("delete from fact.stock_daily_1d where code = '600519'")
        _ensure_query_sources(connection, ("600519",), gap_days)
        connection.execute(
            "insert into fact.stock_daily_1d "
            "(market, code, trade_date, open, high, low, close, pre_close, volume, amount, is_suspended, is_st, adj_factor) "
            "values ('SHSE', '600519', %s::date, 1, 1, 1, 1, 1, 1, 1, false, false, null)",
            (gap_days[0],),
        )
        connection.execute(
            "insert into fact.stock_daily_1d "
            "(market, code, trade_date, open, high, low, close, pre_close, volume, amount, is_suspended, is_st, adj_factor) "
            "values ('SHSE', '600519', %s::date, 1, 1, 1, 1, 1, 1, 1, false, false, 1.2)",
            (gap_days[1],),
        )

    _stub_daily_coverage(monkeypatch, [("600519", _row_count("600519", "2026-08-06", "2026-08-07"))])
    payload = _payload(
        universe="codes",
        codes=["600519"],
        start_date="2026-08-06",
        end_date="2026-08-07",
        page_size=100,
        include_adj_factor=True,
        meta_detail="full",
    ).model_copy(update={"dataset_version": current_dataset_version(STOCK_DAILY_DATASET_ID)})

    with pytest.raises(HTTPException) as exc_info:
        daily_window.build_response(payload, False)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "MARKET_DATA_INCOMPLETE"
    assert "600519" in exc_info.value.detail["details"]["missing_adj_factor_codes"]

    # Without the opt-in the same window still answers, unchanged: the factor
    # gate must not narrow what an existing caller can already read.
    plain = payload.model_copy(update={"include_adj_factor": False})
    items = json.loads(daily_window.build_response(plain, False).content)["items"]
    assert [item["trade_time"] for item in items] == list(gap_days)
    assert all("adj_factor" not in item for item in items)
