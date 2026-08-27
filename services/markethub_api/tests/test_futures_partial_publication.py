from __future__ import annotations

from pathlib import Path
import inspect
import importlib.util
import os
import sys

import pytest
from fastapi.testclient import TestClient


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from quotemux.infra.db.read_client import QueryBatch
from quotemux.public_reader import FuturesPartialPublicationQueryError, FuturesPartialPublicationStaleError
from quotemux.store.futures_partial_migration import provision_futures_partial_roles
from routers import futures as futures_router
from services import futures


QMP = "qmp-v1-" + "a" * 64
QMC = "qmc-v1-" + "b" * 64
QMG = "qmg-v1-" + "c" * 64
DATASET = "future_1m_partial_s000012_quotemux"


class _Reader:
    def __init__(self, *, terminal: bool = False) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.terminal = terminal

    def read_futures_1m_partial_page(self, *args: object, **kwargs: object) -> tuple[QueryBatch, str | None]:
        self.calls.append(("page", args, kwargs))
        return QueryBatch(
            ("product_code", "exchange", "series_type", "bar_time", "open", "high", "low", "close", "volume", "open_interest", "adjustment_offset", "boundary_ids", "source_keys"),
            (("ag", "SHFE", "back_adjusted_continuous", "2026-07-14 09:01:00", 1.0, 2.0, 1.0, 1.5, 3.0, None, 0.0, ["boundary-1"], ["pyramid_back_adjusted_20260714"]),),
        ), None if self.terminal else "next"

    def get_futures_1m_partial_metadata(self, *, qmp_id: str, qmc_id: str, qmg_id: str) -> dict[str, object]:
        self.calls.append(("metadata", (qmp_id, qmc_id, qmg_id), {}))
        return {
            "dataset_id": DATASET, "qmp_id": qmp_id, "qmc_id": qmc_id, "qmg_id": qmg_id,
            "qmi_id": "qmi-v1-test", "catalog_identity": "catalog-v1-test", "publication_verified": True,
            "timezone": "Asia/Shanghai", "interval_bounds": "inclusive_local_naive",
            "missing_bar_semantics": "skip", "session_grid": "not_asserted_complete",
            "open_interest": "null_or_unavailable", "sources": [],
            "source_boundary_manifest": {"count": 1, "sha256": "a" * 64},
            "warmup": {"ag": {"start_time": "2026-07-14 09:01:00"}},
            "coverage_semantics": "observed_admitted_runs_only",
            "residual_semantics": "excluded_or_missing_rows_are_skipped",
            "lineage_limitations": ["fixture"],
        }

    def read_futures_1m_partial_coverage_page(self, *args: object, **kwargs: object) -> tuple[QueryBatch, str | None]:
        self.calls.append(("coverage", args, kwargs))
        return QueryBatch(
            ("product_code", "exchange", "start_time", "end_time", "status", "observed_count", "interval_id", "residual_json"),
            (("ag", "SHFE", "2026-07-14 09:01:00", "2026-07-14 09:01:00", "accepted", 1, "interval-1", {}),),
        ), None if self.terminal else "coverage-next"


def test_partial_facade_delegates_all_partial_identity_and_cursor_to_quotemux(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader()
    monkeypatch.setattr(futures, "_PUBLIC_READER", reader)

    items, metadata, cursor = futures.get_quotes_1m_partial(DATASET, QMP, QMC, QMG, "ag", "2026-07-14 09:01:00", "2026-07-14 09:01:00", 10)
    coverage, coverage_metadata, coverage_cursor = futures.get_quotes_1m_partial_coverage(DATASET, QMP, QMC, QMG, "ag", "2026-07-14 09:01:00", "2026-07-14 09:01:00", 10)

    assert items[0]["source_keys"] == ["pyramid_back_adjusted_20260714"]
    assert coverage[0]["interval_id"] == "interval-1"
    assert metadata["publication_verified"] and coverage_metadata["catalog_identity"] == "catalog-v1-test"
    assert (cursor, coverage_cursor) == ("next", "coverage-next")
    for kind, args, kwargs in reader.calls:
        if kind == "metadata":
            continue
        assert args == ("ag", "2026-07-14 09:01:00", "2026-07-14 09:01:00")
        assert kwargs["qmp_id"] == QMP and kwargs["qmc_id"] == QMC and kwargs["qmg_id"] == QMG


def test_partial_facade_preserves_quotemux_terminal_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader(terminal=True)
    monkeypatch.setattr(futures, "_PUBLIC_READER", reader)

    _, _, cursor = futures.get_quotes_1m_partial(
        DATASET, QMP, QMC, QMG, "ag", "2026-07-14 09:01:00", "2026-07-14 09:01:00", 10,
    )
    _, _, coverage_cursor = futures.get_quotes_1m_partial_coverage(
        DATASET, QMP, QMC, QMG, "ag", "2026-07-14 09:01:00", "2026-07-14 09:01:00", 10,
    )

    assert cursor is None
    assert coverage_cursor is None


def test_partial_router_serializes_terminal_cursor_as_json_null_and_passes_opaque_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    import main

    reader = _Reader(terminal=True)
    monkeypatch.setattr(futures, "_PUBLIC_READER", reader)

    async def run_inline(task, *args):
        return task(*args)

    monkeypatch.setattr(futures_router, "run_futures_partial_task", run_inline)
    params = {
        "dataset_id": DATASET,
        "dataset_version": QMP,
        "partial_completeness_revision": QMC,
        "generation_pin": QMG,
        "codes": "ag",
        "start_time": "2026-07-14 09:01:00",
        "end_time": "2026-07-14 09:01:00",
    }
    client = TestClient(main.app)
    bars = client.get("/api/futures/quotes/1m/partial", params={**params, "cursor": "opaque-bars"})
    coverage = client.get("/api/futures/quotes/1m/partial/coverage", params={**params, "cursor": "opaque-coverage"})

    assert bars.status_code == 200 and coverage.status_code == 200
    assert bars.json()["meta"]["next_cursor"] is None
    assert coverage.json()["meta"]["next_cursor"] is None
    assert b'"next_cursor":null' in bars.content
    assert b'"next_cursor":null' in coverage.content
    calls = {kind: kwargs for kind, _args, kwargs in reader.calls if kind != "metadata"}
    assert calls["page"]["cursor"] == "opaque-bars"
    assert calls["coverage"]["cursor"] == "opaque-coverage"


def test_partial_facade_rejects_unknown_dataset_without_calling_quotemux(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader()
    monkeypatch.setattr(futures, "_PUBLIC_READER", reader)
    with pytest.raises(Exception) as raised:
        futures.get_quotes_1m_partial("other", QMP, QMC, QMG, "ag", "2026-07-14 09:01:00", "2026-07-14 09:01:00", 10)
    assert getattr(raised.value, "status_code") == 409
    assert reader.calls == []


def test_partial_error_mapping_distinguishes_stale_identity_from_bad_cursor() -> None:
    stale = futures._partial_query_error(FuturesPartialPublicationStaleError("stale"), coverage=False)
    malformed = futures._partial_query_error(FuturesPartialPublicationQueryError("cursor"), coverage=True)
    assert stale.status_code == 409 and stale.detail["code"] == "PARTIAL_PUBLICATION_STALE_OR_INVALID"
    assert malformed.status_code == 400 and malformed.detail["code"] == "PARTIAL_COVERAGE_BAD_QUERY"


def test_partial_facade_refuses_page_when_metadata_is_not_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader()
    monkeypatch.setattr(futures, "_PUBLIC_READER", reader)
    monkeypatch.setattr(reader, "get_futures_1m_partial_metadata", lambda **_ids: {"publication_verified": False})
    with pytest.raises(Exception) as raised:
        futures.get_quotes_1m_partial(DATASET, QMP, QMC, QMG, "ag", "2026-07-14 09:01:00", "2026-07-14 09:01:00", 10)
    assert getattr(raised.value, "status_code") == 409
    assert reader.calls == []


def test_router_accepts_prefixed_quote_mux_ids_and_never_derives_complete_from_a_page() -> None:
    source = inspect.getsource(futures_router)
    assert "^qmp-v1-[0-9a-f]{64}$" in source
    assert "^qmc-v1-[0-9a-f]{64}$" in source
    assert "^qmg-v1-[0-9a-f]{64}$" in source
    assert '"complete": None' in source
    assert '"partial_contract_satisfied": metadata["publication_verified"]' in source
    assert "page_source_keys" in source and "source_manifests" in source and "boundary_ids" in source


def test_markethub_contains_no_partial_fact_ddl_or_reader_sql() -> None:
    root = SERVICE_ROOT / "src"
    text = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py"))
    assert "readmodel.future_1m_partial_bar" not in text
    assert "create table if not exists readmodel.future_1m_partial" not in text.lower()


def test_legacy_strict_gate_still_uses_completeness_evidence() -> None:
    source = inspect.getsource(futures.get_quotes_1m_with_evidence)
    assert "validate_published_futures_1m_completeness" in source


def test_privileged_wrapper_only_delegates_quotemux_migration_and_writes_0600_secret_env(tmp_path: Path) -> None:
    path = SERVICE_ROOT.parents[1] / "migrations" / "quotemux_futures_partial_v1_20260826" / "release_migration.py"
    spec = importlib.util.spec_from_file_location("partial_release_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    secret_env = tmp_path / "reader.env"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_HOST", "db.example")
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_PORT", "5432")
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_NAME", "datalake")
    try:
        module._write_secret_env(secret_env, module._database_env_lines("QUOTEMUX_READ_DB", "quotemux_public_reader", "secret"))
    finally:
        monkeypatch.undo()
    text = secret_env.read_text(encoding="utf-8")
    assert "QUOTEMUX_READ_DB_HOST=db.example" in text
    assert "QUOTEMUX_READ_DB_PORT=5432" in text
    assert "QUOTEMUX_READ_DB_NAME=datalake" in text
    assert "QUOTEMUX_READ_DB_USER=quotemux_public_reader" in text
    assert text.endswith("QUOTEMUX_READ_DB_PASSWORD=secret\n")
    # An interrupted first run leaves the durable publisher secret in place;
    # the retry must read the exact key that the wrapper originally wrote.
    publisher_env = tmp_path / "publisher.env"
    module._write_secret_env(publisher_env, (
        "QUOTEMUX_PUBLISH_DB_HOST=stale-host",
        "QUOTEMUX_PUBLISH_DB_PASSWORD=retry-secret",
    ))
    assert module._secret_from_env_file(publisher_env, "QUOTEMUX_PUBLISH_DB_PASSWORD") == "retry-secret"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_HOST", "db.example")
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_PORT", "5432")
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_NAME", "datalake")
    try:
        module._write_secret_env(publisher_env, module._database_env_lines(
            "QUOTEMUX_PUBLISH_DB", "quotemux_futures_partial_publisher", "retry-secret",
        ))
    finally:
        monkeypatch.undo()
    publisher_text = publisher_env.read_text(encoding="utf-8")
    assert "QUOTEMUX_PUBLISH_DB_HOST=db.example" in publisher_text
    assert "QUOTEMUX_PUBLISH_DB_PORT=5432" in publisher_text
    assert "QUOTEMUX_PUBLISH_DB_NAME=datalake" in publisher_text
    assert "QUOTEMUX_PUBLISH_DB_USER=quotemux_futures_partial_publisher" in publisher_text
    assert publisher_text.endswith("QUOTEMUX_PUBLISH_DB_PASSWORD=retry-secret\n")
    if os.name != "nt":
        assert os.stat(secret_env).st_mode & 0o777 == 0o600
    source = path.read_text(encoding="utf-8").lower()
    deploy_script = (SERVICE_ROOT.parents[1] / "scripts" / "local" / "deploy_yosef_server.ps1").read_text(encoding="utf-8").lower()
    assert "provision_futures_partial_roles" in source and "os.chmod(path, 0o600)" in source
    assert "quotemux_publish_db" in source and "quotemux_read_db" in source
    assert "environmentfile=$reader_env_path" in deploy_script
    assert "environmentfile=-$reader_env_path" not in deploy_script
    assert "environmentfile=$publisher_env_path" not in deploy_script


def test_privileged_wrapper_uses_tuple_rows_for_quotemux_role_provision(monkeypatch: pytest.MonkeyPatch) -> None:
    path = SERVICE_ROOT.parents[1] / "migrations" / "quotemux_futures_partial_v1_20260826" / "release_migration.py"
    spec = importlib.util.spec_from_file_location("partial_release_migration_tuple_rows", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured: dict[str, object] = {}
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_HOST", "db.example")
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_PORT", "5432")
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_NAME", "datalake")
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_USER", "postgres")
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_PASSWORD", "private")
    monkeypatch.setattr(module.psycopg, "connect", lambda **kwargs: captured.update(kwargs))
    module._connect()
    assert "row_factory" not in captured

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_args: object): return None
        def execute(self, *_args: object): return None
        def fetchone(self): return ("datalake",)

    class Connection:
        def cursor(self, **_kwargs: object): return Cursor()
        def commit(self): return None
        def rollback(self): return None

    # This mirrors psycopg's default tuple row. The QuoteMux provision path
    # indexes current_database()[0], so a dict-row migration connection would
    # have failed before any live role can be safely created.
    provision_futures_partial_roles("publisher", "reader", connection_factory=Connection)


def test_privileged_wrapper_peer_mode_uses_socket_without_admin_password(monkeypatch: pytest.MonkeyPatch) -> None:
    path = SERVICE_ROOT.parents[1] / "migrations" / "quotemux_futures_partial_v1_20260826" / "release_migration.py"
    spec = importlib.util.spec_from_file_location("partial_release_migration_peer", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured: dict[str, object] = {}
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_PEER", "1")
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_SOCKET_DIR", "/run/postgresql")
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_PORT", "55432")
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_NAME", "datalake")
    monkeypatch.setattr(module.psycopg, "connect", lambda **kwargs: captured.update(kwargs))

    module._connect()

    assert captured["host"] == "/run/postgresql"
    assert captured["port"] == 55432
    assert captured["dbname"] == "datalake"
    assert captured["user"] == "postgres"
    assert "password" not in captured


def test_privileged_wrapper_can_skip_health_only_when_service_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    path = SERVICE_ROOT.parents[1] / "migrations" / "quotemux_futures_partial_v1_20260826" / "release_migration.py"
    spec = importlib.util.spec_from_file_location("partial_release_migration_health_skip", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_SKIP_HEALTH_SNAPSHOT", "1")

    assert module._health_snapshot() == {"skipped": "service deliberately stopped for atomic release handoff"}


def test_privileged_wrapper_probes_publisher_and_reader_without_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    path = SERVICE_ROOT.parents[1] / "migrations" / "quotemux_futures_partial_v1_20260826" / "release_migration.py"
    spec = importlib.util.spec_from_file_location("partial_release_migration_role_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in (("HOST", "db.example"), ("PORT", "5432"), ("NAME", "datalake")):
        monkeypatch.setenv("MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_" + key, value)
    executed: list[str] = []

    class Cursor:
        def __init__(self, user: str) -> None: self.user = user
        def __enter__(self): return self
        def __exit__(self, *_args: object): return None
        def execute(self, query: str): executed.append(query)
        def fetchone(self): return (self.user, True, self.user.endswith("publisher"), False, False, False)

    class Connection:
        def __init__(self, user: str) -> None: self.user = user
        def cursor(self): return Cursor(self.user)
        def rollback(self): return None
        def close(self): return None

    monkeypatch.setattr(module.psycopg, "connect", lambda **kwargs: Connection(str(kwargs["user"])))
    module._probe_role("quotemux_futures_partial_publisher", "publisher-secret", reader=False)
    module._probe_role("quotemux_public_reader", "reader-secret", reader=True)
    assert executed.count("begin read only") == 2
