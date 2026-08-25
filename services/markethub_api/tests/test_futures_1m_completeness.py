from __future__ import annotations

from pathlib import Path
import inspect
import sys

from fastapi import HTTPException
import pytest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from quotemux.infra.db.read_client import QueryBatch
from services import futures, futures_1m_completeness as completeness


VERSION = "mhd-v1-futures-test"


class _Reader:
    def __init__(self, intervals: tuple[tuple[object, ...], ...]) -> None:
        self.intervals = intervals
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def query_batch(self, sql: str, params: tuple[object, ...], *, stage: str) -> QueryBatch:
        self.calls.append((stage, params))
        if stage == "futures_1m_completeness_state":
            return QueryBatch(("coverage_ready", "status", "complete", "error_message"), ((True, "online", True, ""),))
        return QueryBatch(
            ("product_code", "exchange", "series_type", "start_date", "end_date", "status", "availability_ref", "session_rule_ref", "detail_json"),
            self.intervals,
        )


def _interval(status: str, start: str = "2026-02-02", end: str = "2026-02-02", detail: object = None) -> tuple[object, ...]:
    return ("ag", "SHFE", "back_adjusted_continuous", start, end, status, "listing:ag:v1", "session:shfe-day:v3", detail or {})


def _configure(monkeypatch: pytest.MonkeyPatch, *intervals: tuple[object, ...]) -> _Reader:
    reader = _Reader(tuple(intervals))
    monkeypatch.setattr(completeness, "current_dataset_version", lambda _dataset: VERSION)
    monkeypatch.setattr(completeness, "_READ_CLIENT", reader)
    return reader


def _back_adjusted_series_state() -> dict[str, object]:
    return {
        "series_type": "back_adjusted_continuous",
        "generation": 19,
        "row_count": 1_234_567,
        "first_bar_time": "2012-01-04 09:01:00",
        "last_bar_time": "2026-08-11 15:00:00",
        "transaction_id": 42,
        "operation": "upsert",
        "delta_fingerprint": "a" * 32,
    }


def test_ag_225_192_manifest_gap_returns_409_even_when_endpoints_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _interval("missing", detail={"expected_rows": 225, "actual_rows": 192, "first_bar_time": "2026-02-02 09:01:00", "last_bar_time": "2026-02-02 15:00:00", "missing_rows": 33}))

    with pytest.raises(HTTPException) as raised:
        completeness.validate_published_futures_1m_completeness("ag", "back_adjusted_continuous", "2026-02-02 09:01:00", "2026-02-02 15:00:00")

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "DATA_INCOMPLETE"
    assert raised.value.detail["details"]["gap_sample"][0]["detail"]["missing_rows"] == 33


@pytest.mark.parametrize("status", ("not_applicable", "known_no_bar"))
def test_explicit_pre_listing_or_known_no_bar_state_passes(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    _configure(monkeypatch, _interval(status))

    assert completeness.validate_published_futures_1m_completeness("ag", "back_adjusted_continuous", "2026-02-02 09:01:00", "2026-02-02 15:00:00") == VERSION


def test_absent_interval_is_unknown_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)

    with pytest.raises(HTTPException) as raised:
        completeness.validate_published_futures_1m_completeness("ag", "back_adjusted_continuous", "2026-02-02 09:01:00", "2026-02-02 15:00:00")

    assert raised.value.detail["code"] == "DATA_INCOMPLETE"
    assert raised.value.detail["details"]["gap_sample"][0]["reason"] == "unknown"


def test_limit_cannot_hide_a_gap_before_public_reader_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _interval("unknown", detail={"missing_ranges": [["2026-02-02 10:46:00", "2026-02-02 10:46:00"]]}))
    monkeypatch.setattr(futures, "validate_published_futures_1m_completeness", completeness.validate_published_futures_1m_completeness)
    monkeypatch.setattr(
        futures._PUBLIC_READER,
        "get_futures_quotes_1m_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("page fetch must follow completeness validation")),
        raising=False,
    )

    with pytest.raises(HTTPException) as raised:
        futures.get_quotes_1m("ag", "back_adjusted_continuous", "2026-02-02 09:01:00", "2026-02-02 15:00:00", 1)

    assert raised.value.detail["code"] == "DATA_INCOMPLETE"


def test_version_mismatch_is_data_incomplete_not_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _interval("complete"))

    with pytest.raises(HTTPException) as raised:
        completeness.validate_published_futures_1m_completeness("ag", "back_adjusted_continuous", "2026-02-02 09:01:00", "2026-02-02 15:00:00", "mhd-v1-stale")

    assert raised.value.detail["code"] == "DATA_INCOMPLETE"
    assert raised.value.detail["details"]["reason"] == "dataset_version_mismatch"


def test_manifest_validation_requires_refs_and_rejects_overlapping_intervals() -> None:
    base = {
        "product_code": "ag", "exchange": "SHFE", "series_type": "back_adjusted_continuous",
        "status": "complete", "availability_ref": "listing:ag:v1", "session_rule_ref": "session:shfe:v1",
        "start_date": "2026-02-02", "end_date": "2026-02-03",
    }
    with pytest.raises(ValueError, match="overlap"):
        completeness._validated_entries([base, {**base, "start_date": "2026-02-03", "end_date": "2026-02-04"}])
    with pytest.raises(ValueError, match="availability_ref"):
        completeness._validated_entries([{**base, "availability_ref": ""}])


def test_schema_ddl_is_confined_to_explicit_bootstrap_seam() -> None:
    source = inspect.getsource(completeness)

    assert source.count("connection.execute(_DDL)") == 1
    assert "def bootstrap_futures_1m_completeness_schema()" in source
    assert "connection.executemany(" not in source
    assert "cursor.executemany(" in source


@pytest.mark.parametrize(
    "field,replacement",
    (
        ("generation", 20),
        ("row_count", 1_234_568),
        ("first_bar_time", "2012-01-05 09:01:00"),
        ("last_bar_time", "2026-08-11 14:59:00"),
        ("transaction_id", 43),
        ("operation", "delete"),
        ("delta_fingerprint", "b" * 32),
    ),
)
def test_main_generation_carry_forward_requires_every_back_adjusted_lineage_field_to_match(field: str, replacement: object) -> None:
    published = _back_adjusted_series_state()
    assert completeness.can_carry_forward_back_adjusted_completeness(published, _back_adjusted_series_state()) is True
    assert completeness.can_carry_forward_back_adjusted_completeness(published, {**published, field: replacement}) is False
