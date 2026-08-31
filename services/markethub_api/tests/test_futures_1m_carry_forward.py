from __future__ import annotations

from fastapi.testclient import TestClient
from pathlib import Path

from services import admin_runtime, futures_1m_completeness as completeness


LINEAGE = {
    "series_type": "back_adjusted_continuous",
    "generation": 7,
    "row_count": 100,
    "first_bar_time": "2026-08-01 09:01:00",
    "last_bar_time": "2026-08-01 15:00:00",
    "transaction_id": 70,
    "operation": "insert",
    "delta_fingerprint": "a" * 32,
}


class _Result:
    def __init__(self, row=None, *, rowcount: int = 0) -> None:
        self._row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, *, existing=None, previous=None, dataset_state=None) -> None:
        self.existing = existing
        self.previous = previous
        self.dataset_state = dataset_state
        self.writes: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, statement: str, _params=None) -> _Result:
        normalized = " ".join(statement.split())
        if "insert into readmodel.future_1m_completeness_rebuild" in normalized:
            self.writes.append(normalized)
            return _Result({
                "rebuild_id": 41,
                "status": "rebuild_pending",
                "created_at_utc": "2026-08-31 13:00:00+00:00",
                "updated_at_utc": "2026-08-31 13:00:00+00:00",
            })
        if "where publication.dataset_version=%s" in normalized:
            return _Result(self.existing)
        if "where publication.dataset_version<>%s" in normalized:
            return _Result(self.previous)
        if "from audit.dataset_version_state" in normalized:
            return _Result(self.dataset_state)
        if normalized.startswith("insert into"):
            self.writes.append(normalized)
            return _Result(rowcount=3)
        raise AssertionError(normalized)


class _SeriesReader:
    def __init__(self, rows) -> None:
        self.rows = rows

    def list_futures_series_state_batch(self, _series_type):
        class _Batch:
            def __init__(self, rows) -> None:
                self.rows = rows

            def as_dicts(self):
                return self.rows

        return _Batch(self.rows)


def _configure(monkeypatch, connection: _Connection, rows) -> None:
    monkeypatch.setattr(completeness, "current_dataset_version", lambda _dataset_id: "mhd-v1-current")
    monkeypatch.setattr(completeness, "_connect", lambda: connection)
    monkeypatch.setattr(completeness, "_SERIES_READER", _SeriesReader(rows))
    monkeypatch.setattr(completeness, "dataset_version_from_state", lambda *_args: "mhd-v1-current")


def test_identical_lineage_is_carried_once_and_then_idempotent(monkeypatch) -> None:
    connection = _Connection(
        previous={"dataset_version": "mhd-v1-prior", "back_adjusted_series_state": LINEAGE, "manifest_sha256": "b" * 64},
        dataset_state={"baseline_id": "baseline", "generation": 1},
    )
    _configure(monkeypatch, connection, [LINEAGE])

    carried = completeness.carry_forward_current_back_adjusted_completeness()
    connection.existing = {"dataset_version": "mhd-v1-current"}
    rerun = completeness.carry_forward_current_back_adjusted_completeness()

    assert carried["carried"] is True
    assert len(connection.writes) == 3
    assert rerun["reason"] == "already_online"
    assert len(connection.writes) == 3


def test_changed_lineage_defers_rebuild_without_turning_capture_into_http_failure(monkeypatch) -> None:
    connection = _Connection(
        previous={"dataset_version": "mhd-v1-prior", "back_adjusted_series_state": LINEAGE, "manifest_sha256": "b" * 64},
    )
    changed = {**LINEAGE, "generation": 8, "transaction_id": 80}
    _configure(monkeypatch, connection, [changed])

    finalization = completeness.carry_forward_current_back_adjusted_completeness()

    assert finalization["outcome"] == "rebuild_pending"
    assert finalization["reason"] == "back_adjusted_lineage_changed"
    assert finalization["previous_dataset_version"] == "mhd-v1-prior"
    assert finalization["current_back_adjusted_series_state"] == {
        key: value for key, value in changed.items() if key != "series_type"
    }
    assert finalization["rebuild_id"] == 41
    assert finalization["next_action"]["action"] == "process_futures_1m_completeness_rebuild"
    assert len(connection.writes) == 1

    repeated = completeness.carry_forward_current_back_adjusted_completeness()
    assert repeated["rebuild_id"] == 41
    assert len(connection.writes) == 2

    class _CaptureAdmin:
        def run_capture(self, capability_id):
            return {"capability_id": capability_id, "status": "success"}

    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", _CaptureAdmin())
    monkeypatch.setattr(admin_runtime, "run_with_memory_log", lambda _name, _detail, operation: operation())
    monkeypatch.setattr(admin_runtime, "carry_forward_current_back_adjusted_completeness", lambda: finalization)
    monkeypatch.setattr(admin_runtime, "process_next_futures_1m_completeness_rebuild", lambda: {
        "outcome": "published", "rebuild_id": 41, "dataset_version": "mhd-v1-current",
    })
    capture = admin_runtime.run_capture("futures.quotes.main_continuous.1m")

    assert capture["status"] == "success"
    assert capture["read_model_finalization"]["future_1m_completeness"]["outcome"] == "published"
    assert capture["read_model_finalization"]["future_1m_completeness"]["rebuild_id"] == 41


def test_unpublished_lineage_fails_closed_with_actionable_recovery(monkeypatch) -> None:
    connection = _Connection()
    _configure(monkeypatch, connection, [LINEAGE])

    result = completeness.carry_forward_current_back_adjusted_completeness()

    assert result["outcome"] == "failed_closed"
    assert result["reason"] == "back_adjusted_lineage_unpublished"
    assert result["next_action"]["action"] == "publish_verified_futures_1m_completeness"
    assert connection.writes == []


def test_missing_current_lineage_fails_closed_without_any_completeness_write(monkeypatch) -> None:
    connection = _Connection()
    _configure(monkeypatch, connection, [])

    result = completeness.carry_forward_current_back_adjusted_completeness()

    assert result["outcome"] == "failed_closed"
    assert result["reason"] == "back_adjusted_lineage_unavailable"
    assert connection.writes == []


def test_pending_insert_lineage_rebuild_is_verified_and_published_atomically(monkeypatch) -> None:
    current = {**LINEAGE, "generation": 8, "transaction_id": 80, "row_count": 110}
    statements: list[str] = []

    class _RebuildConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, statement: str, _params=None) -> _Result:
            normalized = " ".join(statement.split())
            statements.append(normalized)
            if "for update skip locked" in normalized:
                return _Result({
                    "rebuild_id": 41,
                    "dataset_version": "mhd-v1-current",
                    "previous_dataset_version": "mhd-v1-prior",
                    "back_adjusted_series_state": current,
                    "previous_back_adjusted_series_state": LINEAGE,
                })
            if normalized.startswith("update readmodel.future_1m_completeness_rebuild set status='rebuild_running'"):
                return _Result({"attempt_count": 1})
            if normalized.startswith("insert into readmodel.future_1m_completeness_interval"):
                return _Result(rowcount=12)
            if normalized.startswith("insert into readmodel.future_1m_completeness_publication"):
                return _Result()
            if "from audit.dataset_version_state" in normalized:
                return _Result({"baseline_id": "baseline", "generation": 1})
            if normalized.startswith("insert into readmodel.dataset_build_state"):
                return _Result()
            if normalized.startswith("update readmodel.future_1m_completeness_rebuild set status='published'"):
                return _Result({"updated_at_utc": "2026-08-31 13:02:00+00:00"})
            raise AssertionError(normalized)

    monkeypatch.setattr(completeness, "current_dataset_version", lambda _dataset_id: "mhd-v1-current")
    monkeypatch.setattr(completeness, "dataset_version_from_state", lambda *_args: "mhd-v1-current")
    monkeypatch.setattr(completeness, "_connect", lambda: _RebuildConnection())
    monkeypatch.setattr(completeness, "_SERIES_READER", _SeriesReader([current]))

    result = completeness.process_next_futures_1m_completeness_rebuild()

    assert result["outcome"] == "published"
    assert result["rebuild_id"] == 41
    assert result["intervals"] == 12
    assert any("future_1m_completeness_publication" in statement for statement in statements)
    assert any("dataset_build_state" in statement for statement in statements)


def test_rebuild_is_superseded_when_lineage_changes_before_publication(monkeypatch) -> None:
    queued = {**LINEAGE, "generation": 8, "transaction_id": 80, "row_count": 110}
    observed = {**queued, "generation": 9, "transaction_id": 90, "row_count": 111}
    statements: list[str] = []

    class _Connection:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def execute(self, statement: str, _params=None) -> _Result:
            normalized = " ".join(statement.split()); statements.append(normalized)
            if "for update skip locked" in normalized:
                return _Result({"rebuild_id": 41, "dataset_version": "mhd-v1-current", "previous_dataset_version": "mhd-v1-prior", "back_adjusted_series_state": queued, "previous_back_adjusted_series_state": LINEAGE})
            if "status='rebuild_running'" in normalized: return _Result({"attempt_count": 1})
            if "insert into readmodel.future_1m_completeness_rebuild" in normalized:
                return _Result({"rebuild_id": 42, "status": "rebuild_pending", "created_at_utc": "", "updated_at_utc": ""})
            if "status='superseded'" in normalized: return _Result()
            raise AssertionError(normalized)

    monkeypatch.setattr(completeness, "current_dataset_version", lambda _dataset_id: "mhd-v1-current")
    monkeypatch.setattr(completeness, "_connect", lambda: _Connection())
    monkeypatch.setattr(completeness, "_SERIES_READER", _SeriesReader([observed]))

    result = completeness.process_next_futures_1m_completeness_rebuild()

    assert result["outcome"] == "superseded"
    assert result["replacement_rebuild_id"] == 42
    assert result["next_action"]["action"] == "process_futures_1m_completeness_rebuild"
    assert not any("future_1m_completeness_publication" in statement for statement in statements)


def test_non_monotonic_lineage_rebuild_fails_closed_without_publication(monkeypatch) -> None:
    unsafe = {**LINEAGE, "generation": 8, "transaction_id": 80, "operation": "upsert"}
    statements: list[str] = []

    class _Connection:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def execute(self, statement: str, _params=None) -> _Result:
            normalized = " ".join(statement.split()); statements.append(normalized)
            if "for update skip locked" in normalized:
                return _Result({"rebuild_id": 41, "dataset_version": "mhd-v1-current", "previous_dataset_version": "mhd-v1-prior", "back_adjusted_series_state": unsafe, "previous_back_adjusted_series_state": LINEAGE})
            if "status='rebuild_running'" in normalized: return _Result({"attempt_count": 1})
            if "status='failed_closed'" in normalized: return _Result()
            raise AssertionError(normalized)

    monkeypatch.setattr(completeness, "current_dataset_version", lambda _dataset_id: "mhd-v1-current")
    monkeypatch.setattr(completeness, "_connect", lambda: _Connection())
    monkeypatch.setattr(completeness, "_SERIES_READER", _SeriesReader([unsafe]))

    result = completeness.process_next_futures_1m_completeness_rebuild()

    assert result["outcome"] == "failed_closed"
    assert result["reason"] == "full_completeness_audit_required"
    assert not any("future_1m_completeness_publication" in statement for statement in statements)


def test_admin_api_exposes_rebuild_status_health_and_safe_retry(monkeypatch) -> None:
    from main import app

    item = {
        "rebuild_id": 41, "status": "failed_closed", "reason": "full_completeness_audit_required",
        "dataset_version": "mhd-v1-current", "lineage_generation": 8, "lineage_transaction_id": 80,
        "next_action": {"action": "run_full_futures_1m_completeness_audit"},
    }
    monkeypatch.setattr(admin_runtime, "list_futures_1m_completeness_rebuilds", lambda _limit: [item])
    monkeypatch.setattr(admin_runtime, "retry_futures_1m_completeness_rebuild", lambda _rebuild_id: {**item, "status": "rebuild_pending"})
    monkeypatch.setattr(admin_runtime, "get_futures_1m_completeness_rebuild_health", lambda: {
        "status": "unhealthy", "failed_closed": 1, "pending": 0, "oldest_pending_seconds": 0,
    })
    client = TestClient(app)

    assert client.get("/api/admin/futures-1m-completeness-rebuilds?limit=10").json() == [item]
    assert client.post("/api/admin/futures-1m-completeness-rebuilds/41/retry").json()["status"] == "rebuild_pending"
    assert client.get("/api/admin/futures-1m-completeness-rebuilds/health").json()["status"] == "unhealthy"


def test_release_database_bootstrap_installs_rebuild_lifecycle_schema() -> None:
    project_root = Path(__file__).resolve().parents[3]
    deployment_sql = (project_root / "scripts" / "deploy" / "dataset-version-vector.sql").read_text(encoding="utf-8")

    assert "create table if not exists readmodel.future_1m_completeness_rebuild" in deployment_sql
    assert "unique (dataset_id,dataset_version,lineage_generation,lineage_transaction_id)" in deployment_sql
