from __future__ import annotations

from pathlib import Path
import inspect
import importlib.util
import os
import sys

import pytest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from quotemux.infra.db.read_client import QueryBatch
from quotemux.public_reader import FuturesPartialPublicationQueryError, FuturesPartialPublicationStaleError
from routers import futures as futures_router
from services import futures


QMP = "qmp-v1-" + "a" * 64
QMC = "qmc-v1-" + "b" * 64
QMG = "qmg-v1-" + "c" * 64
DATASET = "future_1m_partial_s000012_quotemux"


class _Reader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def read_futures_1m_partial_page(self, *args: object, **kwargs: object) -> tuple[QueryBatch, str]:
        self.calls.append(("page", args, kwargs))
        return QueryBatch(
            ("product_code", "exchange", "series_type", "bar_time", "open", "high", "low", "close", "volume", "open_interest", "adjustment_offset", "boundary_ids", "source_keys"),
            (("ag", "SHFE", "back_adjusted_continuous", "2026-07-14 09:01:00", 1.0, 2.0, 1.0, 1.5, 3.0, None, 0.0, ["boundary-1"], ["pyramid_back_adjusted_20260714"]),),
        ), "next"

    def read_futures_1m_partial_coverage_page(self, *args: object, **kwargs: object) -> tuple[QueryBatch, str]:
        self.calls.append(("coverage", args, kwargs))
        return QueryBatch(
            ("product_code", "exchange", "start_time", "end_time", "status", "observed_count", "interval_id", "residual_json"),
            (("ag", "SHFE", "2026-07-14 09:01:00", "2026-07-14 09:01:00", "accepted", 1, "interval-1", {}),),
        ), "coverage-next"


def test_partial_facade_delegates_all_partial_identity_and_cursor_to_quotemux(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader()
    monkeypatch.setattr(futures, "_PUBLIC_READER", reader)

    items, cursor = futures.get_quotes_1m_partial(DATASET, QMP, QMC, QMG, "ag", "2026-07-14 09:01:00", "2026-07-14 09:01:00", 10)
    coverage, coverage_cursor = futures.get_quotes_1m_partial_coverage(DATASET, QMP, QMC, QMG, "ag", "2026-07-14 09:01:00", "2026-07-14 09:01:00", 10)

    assert items[0]["source_keys"] == ["pyramid_back_adjusted_20260714"]
    assert coverage[0]["interval_id"] == "interval-1"
    assert (cursor, coverage_cursor) == ("next", "coverage-next")
    for _, args, kwargs in reader.calls:
        assert args == ("ag", "2026-07-14 09:01:00", "2026-07-14 09:01:00")
        assert kwargs["qmp_id"] == QMP and kwargs["qmc_id"] == QMC and kwargs["qmg_id"] == QMG


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


def test_router_accepts_prefixed_quote_mux_ids_and_never_derives_complete_from_a_page() -> None:
    source = inspect.getsource(futures_router)
    assert "^qmp-v1-[0-9a-f]{64}$" in source
    assert "^qmc-v1-[0-9a-f]{64}$" in source
    assert "^qmg-v1-[0-9a-f]{64}$" in source
    assert '"complete": None' in source
    assert '"partial_contract_satisfied": True' in source
    assert "aggregate_source_keys" in source and "boundary_ids" in source


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
    if os.name != "nt":
        assert os.stat(secret_env).st_mode & 0o777 == 0o600
    source = path.read_text(encoding="utf-8").lower()
    deploy_script = (SERVICE_ROOT.parents[1] / "scripts" / "local" / "deploy_yosef_server.ps1").read_text(encoding="utf-8").lower()
    assert "provision_futures_partial_roles" in source and "os.chmod(path, 0o600)" in source
    assert "quotemux_publish_db" in source and "quotemux_read_db" in source
    assert "environmentfile=-$reader_env_path" in deploy_script
    assert "quotemux-futures-partial-publisher.env" not in deploy_script
    assert "create table" not in source and "readmodel.future_1m_partial" not in source
