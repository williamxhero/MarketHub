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
        if stage == "futures_1m_completeness_active_revision":
            return QueryBatch(("revision_sha256",), ())
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

    assert completeness.validate_published_futures_1m_completeness(
        "ag", "back_adjusted_continuous", "2026-02-02 09:01:00", "2026-02-02 15:00:00",
    ) == completeness.Futures1mCompletenessEvidence(VERSION)


def test_absent_interval_is_unknown_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)

    with pytest.raises(HTTPException) as raised:
        completeness.validate_published_futures_1m_completeness("ag", "back_adjusted_continuous", "2026-02-02 09:01:00", "2026-02-02 15:00:00")

    assert raised.value.detail["code"] == "DATA_INCOMPLETE"
    assert raised.value.detail["details"]["gap_sample"][0]["reason"] == "unknown"


def test_partial_revision_cannot_make_the_original_23_product_history_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    codes = "ag,al,AP,CF,cu,hc,i,j,m,MA,ni,p,ru,sc,T,TA,TF,v,y,lh,SA,ao,si"

    with pytest.raises(HTTPException) as raised:
        completeness.validate_published_futures_1m_completeness(
            codes, "back_adjusted_continuous", "2012-01-01 00:00:00", "2026-08-11 23:59:59",
        )

    assert raised.value.detail["details"]["reason"] == "missing_or_unknown_interval"
    assert len(raised.value.detail["details"]["gap_sample"]) == 23


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


def test_public_utc_offset_timestamps_keep_the_strict_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)

    with pytest.raises(HTTPException) as raised:
        completeness.validate_published_futures_1m_completeness(
            "ag", "back_adjusted_continuous", "2012-01-01 00:00:00+00:00", "2026-08-11 23:59:59+00:00",
        )

    assert raised.value.detail["details"]["reason"] == "missing_or_unknown_interval"


def test_timestamp_revision_allows_only_exact_verified_window_and_returns_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    revision = "b" * 64
    reader = _configure(monkeypatch)

    def query_batch(sql: str, params: tuple[object, ...], *, stage: str) -> QueryBatch:
        if stage == "futures_1m_completeness_state":
            return QueryBatch(("coverage_ready", "status", "complete", "error_message"), ((True, "online", True, ""),))
        if stage == "futures_1m_completeness_active_revision":
            return QueryBatch(("revision_sha256",), ((revision,),))
        assert stage == "futures_1m_completeness_revision_intervals"
        return QueryBatch(
            ("product_code", "exchange", "series_type", "start_time", "end_time", "status", "availability_ref", "session_rule_ref", "evidence_sha256", "detail_json"),
            (("ag", "SHFE", "back_adjusted_continuous", "2026-07-20 21:00:00", "2026-07-21 02:30:00", "complete", "listing:ag:v1", "session:ag:2026w30", "c" * 64, {}),),
        )

    reader.query_batch = query_batch  # type: ignore[method-assign]
    evidence = completeness.validate_published_futures_1m_completeness(
        "ag", "back_adjusted_continuous", "2026-07-20 21:00:00", "2026-07-21 02:30:00", VERSION, revision,
    )

    assert evidence == completeness.Futures1mCompletenessEvidence(VERSION, revision)
    with pytest.raises(HTTPException) as raised:
        completeness.validate_published_futures_1m_completeness(
            "ag", "back_adjusted_continuous", "2026-07-20 20:59:00", "2026-07-21 02:30:00", VERSION, revision,
        )
    assert raised.value.detail["details"]["reason"] == "missing_or_unknown_interval"


def test_stale_expected_revision_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    revision = "b" * 64
    reader = _configure(monkeypatch)
    original = reader.query_batch

    def query_batch(sql: str, params: tuple[object, ...], *, stage: str) -> QueryBatch:
        if stage == "futures_1m_completeness_active_revision":
            return QueryBatch(("revision_sha256",), ((revision,),))
        return original(sql, params, stage=stage)

    reader.query_batch = query_batch  # type: ignore[method-assign]
    with pytest.raises(HTTPException) as raised:
        completeness.validate_published_futures_1m_completeness(
            "ag", "back_adjusted_continuous", "2026-07-20 21:00:00", "2026-07-21 02:30:00", VERSION, "a" * 64,
        )
    assert raised.value.detail["details"]["reason"] == "completeness_revision_mismatch"


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


def test_revision_manifest_requires_evidence_hash_and_rejects_timestamp_overlap() -> None:
    base = {
        "product_code": "ag", "exchange": "SHFE", "series_type": "back_adjusted_continuous", "status": "complete",
        "availability_ref": "listing:ag:v1", "session_rule_ref": "session:ag:2026w30", "evidence_sha256": "d" * 64,
        "start_time": "2026-07-20 21:00:00", "end_time": "2026-07-21 02:30:00",
    }
    with pytest.raises(ValueError, match="overlap"):
        completeness._validated_revision_entries([base, {**base, "start_time": "2026-07-21 02:30:00", "end_time": "2026-07-21 02:31:00"}])
    with pytest.raises(ValueError, match="SHA-256"):
        completeness._validated_revision_entries([{**base, "evidence_sha256": "not-a-hash"}])


def test_activation_appends_an_event_without_updating_immutable_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    revision = "e" * 64
    calls: list[str] = []

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: tuple[object, ...]):
            calls.append(sql)
            if "from readmodel.future_1m_completeness_revision where" in sql:
                return _Result({"revision_sha256": revision})
            if "future_1m_completeness_active_revision" in sql:
                return _Result(None)
            assert "insert into readmodel.future_1m_completeness_revision_activation" in sql
            return _Result({"activation_id": 7})

    class _Result:
        def __init__(self, row: object) -> None:
            self.row = row

        def fetchone(self) -> object:
            return self.row

    monkeypatch.setattr(completeness, "current_dataset_version", lambda _dataset: VERSION)
    monkeypatch.setattr(completeness, "_connect", lambda: _Connection())
    result = completeness.activate_futures_1m_completeness_revision(VERSION, revision)

    assert result == {"dataset_id": "future_bar_1m", "dataset_version": VERSION, "revision_sha256": revision, "activation_id": 7, "idempotent": False}
    assert any("insert into readmodel.future_1m_completeness_revision_activation" in call for call in calls)
    assert not any(call.lstrip().lower().startswith("update") for call in calls)


def test_schema_ddl_is_confined_to_explicit_bootstrap_seam() -> None:
    source = inspect.getsource(completeness)

    assert source.count("connection.execute(_DDL)") == 1
    assert "def bootstrap_futures_1m_completeness_schema()" in source
    assert "connection.executemany(" not in source
    assert "cursor.executemany(" in source
    assert "future_1m_completeness_revision_activation" in source
    assert "create or replace view readmodel.future_1m_completeness_active_revision" in source
    assert "immutable futures completeness state already exists" in source


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
