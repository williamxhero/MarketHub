from __future__ import annotations

from pathlib import Path
import sys
import importlib.util
import inspect

import hashlib
import json
import pyarrow as pa
import pyarrow.parquet as pq
import pytest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from quotemux.infra.db.read_client import QueryBatch
from services import futures_partial_publication as partial


STATE = {
    "generation": 7, "row_count": 2, "first_bar_time": "2026-07-14 09:01:00",
    "last_bar_time": "2026-07-14 09:02:00", "transaction_id": 9,
    "operation": "insert", "delta_fingerprint": "abc",
}
LINEAGE = {
    "provider": "shinny_edb", "provider_package_version": "2026.8.25",
    "timestamp_contract": "bar-start +1m -> Asia/Shanghai bar-end", "adjustment": "none",
    "roll_mapping": "unavailable", "license": "retention_unverified",
    "raw_artifact_sha256": "a" * 64, "staged_artifact_sha256": "b" * 64,
    "normalized_artifact_sha256": "c" * 64, "normalized_rowset_sha256": "d" * 64,
    "fields": "OHLCV,close_oi", "oi_semantics": "close_oi",
    "catalog_version": "unverified", "calendar_version": "unverified",
    "session_contract": "unverified", "session_evidence_sha256": "e" * 64,
    "timezone": "Asia/Shanghai", "bar_label": "unverified", "units": "unknown",
    "source_boundary": "fixture", "missing_bar_semantics": "skip",
}
ACCEPTED = {
    "product_code": "ag", "exchange": "SHFE", "start_time": "2026-07-14 09:01:00",
    "end_time": "2026-07-14 09:02:00", "status": "accepted", "evidence_sha256": "b" * 64, "detail": {"bar_count": 2},
}


def test_manifest_rejects_conflicting_duplicate_instead_of_silently_deduping() -> None:
    rows = [
        {"product_code": "ag", "exchange": "SHFE", "bar_time": "2026-07-14 09:01:00", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 1, "source_key": "pyramid:a"},
        {"product_code": "ag", "exchange": "SHFE", "bar_time": "2026-07-14 09:01:00", "open": 2, "high": 3, "low": 2, "close": 3, "volume": 1, "source_key": "pyramid:b"},
    ]
    with pytest.raises(ValueError, match="duplicate"):
        partial._validated_bars(rows, [ACCEPTED])


def test_partial_copy_stage_has_exact_copy_columns_without_publication_identity() -> None:
    ddl = partial._PARTIAL_BAR_STAGE_DDL.lower()
    assert "dataset_id" not in ddl and "dataset_version" not in ddl
    assert tuple(partial._NORMALIZED_COLUMNS) == ("product_code", "exchange", "bar_time", "open", "high", "low", "close", "volume", "open_interest", "adjustment_offset", "source_key")


def test_partial_bootstrap_privilege_query_rejects_all_write_like_truthy_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKETHUB_DB_USER", "datalake_reader")
    calls: list[str] = []
    class Result:
        def fetchall(self) -> list[dict[str, object]]:
            return [{"can_select": True, "can_insert": False, "can_update": False, "can_delete": False, "can_truncate": False, "can_references": False, "can_trigger": True}] * 5
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_args: object): return None
        def execute(self, statement: object, _params: object = None) -> Result:
            calls.append(str(statement)); return Result()
    monkeypatch.setattr(partial, "_connect", lambda: Connection())
    with pytest.raises(RuntimeError, match="privilege verification"):
        partial.bootstrap_futures_1m_partial_publication_schema()
    query = next(call.lower() for call in calls if "has_table_privilege" in call.lower())
    for privilege in ("insert", "update", "delete", "truncate", "references", "trigger"):
        assert f"'{privilege}'" in query


def test_manifest_rejects_bar_in_exact_exclusion() -> None:
    rows = [{"product_code": "ag", "exchange": "SHFE", "bar_time": "2026-07-14 09:01:00", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 1, "source_key": "pyramid:a"}]
    with pytest.raises(ValueError, match="outside accepted"):
        partial._validated_bars(rows, [{**ACCEPTED, "status": "excluded"}])


def test_partial_read_requires_frozen_identity_generation_and_returns_skip_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    pin = partial.generation_pin(STATE)

    class Reader:
        def query_batch(self, _sql: str, _params: tuple[object, ...], *, stage: str) -> QueryBatch:
            if stage == "futures_1m_partial_header":
                return QueryBatch(("source_id", "read_series_type", "source_series_state", "source_lineage"), (("shinny_edb_main", "main_continuous", STATE, LINEAGE),))
            assert stage == "futures_1m_partial_intervals"
            return QueryBatch(("product_code", "exchange", "start_time", "end_time", "status", "evidence_sha256", "detail_json"), (
                ("ag", "SHFE", "2026-07-14 09:01:00", "2026-07-14 09:01:00", "accepted", "b" * 64, {}),
                ("ag", "SHFE", "2026-07-14 09:02:00", "2026-07-14 09:02:00", "excluded", "c" * 64, {"reason": "conflicting_raw_duplicate"}),
                ("ag", "SHFE", "2026-07-14 09:03:00", "2026-07-14 09:03:00", "residual", "d" * 64, {"reason": "source_unavailable"}),
            ))

    monkeypatch.setattr(partial, "_READ_CLIENT", Reader())
    evidence = partial.validate_futures_1m_partial_publication(
        "future_1m_partial_shinny_edb", "fmp-v1-" + "e" * 64, "e" * 64, pin,
        "ag", "2026-07-14 09:01:00", "2026-07-14 09:03:00",
    )
    assert [item["status"] for item in evidence.accepted] == ["accepted"]
    assert evidence.skipped[0]["detail"]["reason"] == "conflicting_raw_duplicate"
    assert evidence.residual[0]["status"] == "residual"
    with pytest.raises(ValueError, match="generation_pin"):
        partial.validate_futures_1m_partial_publication("future_1m_partial_shinny_edb", "fmp-v1-" + "e" * 64, "e" * 64, "stale", "ag", "2026-07-14 09:01:00", "2026-07-14 09:03:00")


def test_partial_page_reads_materialization_not_mutable_global_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = partial.PartialPublicationEvidence("future_1m_partial_shinny_edb", "fmp-v1-" + "e" * 64, "e" * 64, partial.generation_pin(STATE), "shinny_edb_main", "main_continuous", LINEAGE, (), (), ())
    captured: dict[str, str] = {}
    class Reader:
        def query_batch(self, sql: str, _params: tuple[object, ...], *, stage: str) -> QueryBatch:
            captured["sql"] = sql
            assert stage == "futures_1m_partial_page"
            return QueryBatch(("product_code", "exchange", "bar_time", "open", "high", "low", "close", "volume", "open_interest", "adjustment_offset", "source_key"), (("ag", "SHFE", "2026-07-14 09:01:00", 1.0, 2.0, 1.0, 1.5, 2.0, 3.0, None, "shinny_edb:capture"),))
    monkeypatch.setattr(partial, "_READ_CLIENT", Reader())
    rows, cursor = partial.read_futures_1m_partial_page(evidence, "ag", "2026-07-14 09:01:00", "2026-07-14 09:01:00", 2)
    assert "readmodel.future_1m_partial_bar" in captured["sql"]
    assert "fact.future_bar_1m" not in captured["sql"]
    assert rows[0]["series_type"] == "main_continuous"
    assert cursor == ""


def test_missing_product_is_explicit_residual_and_cursor_is_identity_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    pin = partial.generation_pin(STATE)
    class Reader:
        def query_batch(self, _sql: str, _params: tuple[object, ...], *, stage: str) -> QueryBatch:
            if stage == "futures_1m_partial_header":
                return QueryBatch(("source_id", "read_series_type", "source_series_state", "source_lineage"), (("pyramid", "back_adjusted_continuous", STATE, LINEAGE),))
            return QueryBatch(("product_code", "exchange", "start_time", "end_time", "status", "evidence_sha256", "detail_json"), ())
    monkeypatch.setattr(partial, "_READ_CLIENT", Reader())
    evidence = partial.validate_futures_1m_partial_publication("future_1m_partial_pyramid", "fmp-v1-" + "f" * 64, "f" * 64, pin, "ag", "2026-07-14 09:01:00", "2026-07-14 09:02:00")
    assert evidence.residual[0]["detail"]["reason"] == "undeclared_coverage"
    cursor = partial._next_cursor({"bar_time": "2026-07-14 09:01:00", "product_code": "ag"}, evidence)
    assert partial._cursor(cursor, evidence) is not None
    other = partial.PartialPublicationEvidence(evidence.dataset_id, evidence.dataset_version, evidence.partial_completeness_revision, "fmpg-v1-other", evidence.source_id, evidence.read_series_type, evidence.source_lineage, (), (), ())
    with pytest.raises(ValueError, match="cursor"):
        partial._cursor(cursor, other)


def test_single_missing_minute_is_residual_not_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    pin = partial.generation_pin(STATE)
    class Reader:
        def query_batch(self, _sql: str, _params: tuple[object, ...], *, stage: str) -> QueryBatch:
            if stage == "futures_1m_partial_header":
                return QueryBatch(("source_id", "read_series_type", "source_series_state", "source_lineage"), (("pyramid", "back_adjusted_continuous", STATE, LINEAGE),))
            return QueryBatch(("product_code", "exchange", "start_time", "end_time", "status", "evidence_sha256", "detail_json"), ())
    monkeypatch.setattr(partial, "_READ_CLIENT", Reader())
    evidence = partial.validate_futures_1m_partial_publication("future_1m_partial_pyramid", "fmp-v1-" + "0" * 64, "0" * 64, pin, "ag", "2026-07-14 09:01:00", "2026-07-14 09:01:00")
    assert evidence.residual[0]["start_time"] == "2026-07-14 09:01:00"


def test_pre_db_artifact_gate_rejects_conflict_invalid_and_declared_residual(tmp_path: Path) -> None:
    rows = [
        {"product_code": "ag", "exchange": "SHFE", "bar_time": "2026-07-14 09:01:00", "open": 1.0, "high": 2.0, "low": 1.0, "close": 1.5, "volume": 2.0, "open_interest": None, "adjustment_offset": 0.0, "source_key": "raw:a"},
        {"product_code": "cu", "exchange": "SHFE", "bar_time": "2026-07-14 09:02:00", "open": 3.0, "high": 4.0, "low": 3.0, "close": 3.5, "volume": 4.0, "open_interest": None, "adjustment_offset": 0.0, "source_key": "raw:b"},
    ]
    path = tmp_path / "normalized.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=pa.schema([(name, pa.string() if name in ("product_code", "exchange", "bar_time", "source_key") else pa.float64()) for name in partial._NORMALIZED_COLUMNS])), path, compression="snappy")
    artifact = partial._file_sha256(path)
    rowset = hashlib.sha256(b"".join(partial.canonical_normalized_row_bytes(row) for row in rows)).hexdigest()
    entries = [
        {**ACCEPTED, "end_time": "2026-07-14 09:01:00", "evidence_sha256": artifact, "detail": {"bar_count": 1}},
        {"product_code": "ag", "exchange": "SHFE", "start_time": "2026-07-14 09:02:00", "end_time": "2026-07-14 09:02:00", "status": "excluded", "evidence_sha256": "a" * 64, "detail": {"reason": "conflicting_timestamp_group"}},
        {"product_code": "ag", "exchange": "SHFE", "start_time": "2026-07-14 09:03:00", "end_time": "2026-07-14 09:03:00", "status": "excluded", "evidence_sha256": "b" * 64, "detail": {"reason": "invalid_ohlc"}},
        {"product_code": "ag", "exchange": "SHFE", "start_time": "2026-07-14 09:04:00", "end_time": "2026-07-14 09:05:00", "status": "residual", "evidence_sha256": "c" * 64, "detail": {"reason": "declared_request_end_after_source"}},
        {"product_code": "cu", "exchange": "SHFE", "start_time": "2026-07-14 09:02:00", "end_time": "2026-07-14 09:02:00", "status": "accepted", "evidence_sha256": artifact, "detail": {"bar_count": 1}},
    ]
    manifest = {"dataset_id": "future_1m_partial_pyramid", "source_id": "pyramid_local", "read_series_type": "back_adjusted_continuous", "source_series_state": STATE, "source_lineage": {**LINEAGE, "normalized_artifact_sha256": artifact, "normalized_rowset_sha256": rowset}, "authorization": {"status": "private_research_authorized", "evidence": "fixture only"}, "normalized_row_count": 2, "entries": entries, "product_coverage": {"ag": {}, "cu": {}}}
    _, _, count, actual = partial.validate_normalized_partial_artifact(manifest, path)
    assert (count, actual) == (2, rowset)


def test_local_preparer_multifile_stages_conflicts_and_declares_gaps(tmp_path: Path) -> None:
    preparer_path = SERVICE_ROOT.parents[1] / "migrations" / "futures_1m_partial_publication_v1_20260825" / "prepare_pyramid_parquet.py"
    module_spec = importlib.util.spec_from_file_location("pyramid_preparer", preparer_path)
    assert module_spec and module_spec.loader
    preparer = importlib.util.module_from_spec(module_spec); sys.modules[module_spec.name] = preparer; module_spec.loader.exec_module(preparer)
    ag = tmp_path / "ag.txt"; cu = tmp_path / "cu.txt"; evidence = tmp_path / "gaps.txt"
    ag.write_text("2026-07-14-09:01\t1\t2\t1\t1.5\t2\t0\n2026-07-14-09:03\t1\t2\t1\t1.5\t2\t0\n2026-07-14-09:03\t1\t2\t1\t1.5\t2\t0\n", encoding="gbk")
    cu.write_text("2026-07-14-09:01\t3\t2\t4\t3\t2\t0\n", encoding="gbk")
    evidence.write_text("gap evidence", encoding="utf-8")
    source = {"dataset_id": "future_1m_partial_pyramid", "source_id": "pyramid_local", "source_series_state": STATE, "declared_request_end": "2026-07-14 09:05:00", "files": [{"raw_path": str(ag), "product_code": "ag", "exchange": "SHFE", "evidence_paths": [str(evidence)]}, {"raw_path": str(cu), "product_code": "cu", "exchange": "SHFE", "evidence_paths": [str(evidence)]}]}
    spec_path = tmp_path / "spec.json"; spec_path.write_text(json.dumps(source), encoding="utf-8")
    result = preparer.prepare_spec(spec_path, tmp_path / "bundle", batch_size=1)
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert Path(result["staged"]).is_file() and Path(result["normalized"]).is_file()
    assert manifest["schema_version"] == "futures_pyramid_partial_bundle_v5"
    assert "entries" not in manifest and (Path(result["manifest"]).parent / "intervals.jsonl").is_file()
    assert manifest["interval_artifact"]["status_counts"] == {"accepted": 1, "excluded": 2, "residual": 3}
    assert manifest["source_lineage"]["oi_semantics"] == "unavailable"
    assert manifest["product_coverage"]["ag"]["raw_rows"] == 3
    assert manifest["product_coverage"]["ag"]["conflicting_timestamp_keys"] == 1
    assert manifest["product_coverage"]["ag"]["conflicting_rows_removed"] == 2
    assert manifest["product_coverage"]["ag"]["excluded_interval_count"] == 1
    assert pq.ParquetFile(result["staged"]).metadata.num_row_groups > 1
    staged = pq.read_table(result["staged"]).to_pylist()
    assert all(row["status"] == "excluded" for row in staged if row["bar_time"] == "2026-07-14 09:03:00")
    assert partial.generation_pin(manifest["source_series_state"]).startswith("fmpg-v1-")
    canonical, entries, count, rowset, normalized = partial.validate_bundle_artifacts(manifest, Path(result["manifest"]).parent)
    assert canonical["dataset_id"] == "future_1m_partial_pyramid"
    assert (count, rowset, normalized.name) == (manifest["normalized_row_count"], manifest["source_lineage"]["normalized_rowset_sha256"], "normalized.parquet")
    tampered = json.loads(json.dumps(manifest)); tampered["product_coverage"]["ag"]["valid_rows"] = 999
    with pytest.raises(ValueError, match="product_coverage"):
        partial.validate_bundle_artifacts(tampered, Path(result["manifest"]).parent)


@pytest.mark.parametrize(("field", "replacement"), [("close", 1.4), ("source_key", "pyramid:" + "f" * 64)])
def test_bundle_rejects_normalized_provenance_tamper(tmp_path: Path, field: str, replacement: object) -> None:
    preparer_path = SERVICE_ROOT.parents[1] / "migrations" / "futures_1m_partial_publication_v1_20260825" / "prepare_pyramid_parquet.py"
    module_spec = importlib.util.spec_from_file_location("pyramid_provenance_preparer", preparer_path); assert module_spec and module_spec.loader
    preparer = importlib.util.module_from_spec(module_spec); sys.modules[module_spec.name] = preparer; module_spec.loader.exec_module(preparer)
    raw=tmp_path / "ag.txt"; evidence=tmp_path / "evidence.txt"; raw.write_text("2026-07-14-09:01\t1\t2\t1\t1.5\t2\t0\n",encoding="gbk"); evidence.write_text("e",encoding="utf-8")
    spec=tmp_path / "spec.json"; spec.write_text(json.dumps({"dataset_id":"future_1m_partial_pyramid","source_id":"pyramid","files":[{"raw_path":str(raw),"product_code":"ag","exchange":"SHFE","evidence_paths":[str(evidence)]}]}),encoding="utf-8")
    result=preparer.prepare_spec(spec,tmp_path / "bundle"); root=Path(result["manifest"]).parent; manifest=json.loads(Path(result["manifest"]).read_text(encoding="utf-8")); normalized=root / "normalized.parquet"; rows=pq.read_table(normalized).to_pylist(); rows[0][field]=replacement
    pq.write_table(pa.Table.from_pylist(rows,schema=pq.read_table(normalized).schema),normalized,compression="snappy")
    digest=partial._file_sha256(normalized); manifest["source_lineage"]["normalized_artifact_sha256"]=digest
    for item in manifest["artifact_bundle"]["files"]:
        if item["role"]=="normalized": item["sha256"]=digest;item["size_bytes"]=normalized.stat().st_size
    manifest["source_series_state"]["generation_id"] = hashlib.sha256(json.dumps({"raw":manifest["artifact_bundle"]["raw_files"],"evidence":manifest["artifact_bundle"]["evidence_files"],"normalized":digest},sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
    with pytest.raises(ValueError, match="rowset|staged-derived"):
        partial.validate_bundle_artifacts(manifest,root)


def test_bundle_rejects_staged_and_normalized_rewrite_when_raw_is_unchanged(tmp_path: Path) -> None:
    preparer_path = SERVICE_ROOT.parents[1] / "migrations" / "futures_1m_partial_publication_v1_20260825" / "prepare_pyramid_parquet.py"
    module_spec = importlib.util.spec_from_file_location("pyramid_staged_provenance_preparer", preparer_path); assert module_spec and module_spec.loader
    preparer = importlib.util.module_from_spec(module_spec); sys.modules[module_spec.name] = preparer; module_spec.loader.exec_module(preparer)
    raw=tmp_path / "ag.txt"; evidence=tmp_path / "evidence.txt"; raw.write_text("2026-07-14-09:01\t1\t2\t1\t1.5\t2\t0\n",encoding="gbk"); evidence.write_text("e",encoding="utf-8")
    spec=tmp_path / "spec.json"; spec.write_text(json.dumps({"dataset_id":"future_1m_partial_pyramid","source_id":"pyramid","files":[{"raw_path":str(raw),"product_code":"ag","exchange":"SHFE","evidence_paths":[str(evidence)]}]}),encoding="utf-8")
    result=preparer.prepare_spec(spec,tmp_path / "bundle"); root=Path(result["manifest"]).parent; manifest=json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    staged=root / "staged.parquet"; normalized=root / "normalized.parquet"
    staged_rows=pq.read_table(staged).to_pylist(); staged_rows[0]["close"]=1.4
    normalized_rows=pq.read_table(normalized).to_pylist(); normalized_rows[0]["close"]=1.4
    pq.write_table(pa.Table.from_pylist(staged_rows,schema=pq.read_table(staged).schema),staged,compression="snappy")
    pq.write_table(pa.Table.from_pylist(normalized_rows,schema=pq.read_table(normalized).schema),normalized,compression="snappy")
    staged_sha=partial._file_sha256(staged); normalized_sha=partial._file_sha256(normalized)
    manifest["source_lineage"]["staged_artifact_sha256"]=staged_sha; manifest["source_lineage"]["staged_rowset_sha256"]=partial._staged_rowset(staged)[1]
    manifest["source_lineage"]["normalized_artifact_sha256"]=normalized_sha
    manifest["source_lineage"]["normalized_rowset_sha256"]=hashlib.sha256(b"".join(partial.canonical_normalized_row_bytes(row) for row in normalized_rows)).hexdigest()
    for item in manifest["artifact_bundle"]["files"]:
        if item["role"] == "staged": item["sha256"]=staged_sha; item["size_bytes"]=staged.stat().st_size
        if item["role"] == "normalized": item["sha256"]=normalized_sha; item["size_bytes"]=normalized.stat().st_size
    manifest["artifact_bundle"]["staged_artifact_sha256"]=staged_sha; manifest["artifact_bundle"]["normalized_artifact_sha256"]=normalized_sha
    manifest["source_series_state"]["generation_id"] = hashlib.sha256(json.dumps({"raw":manifest["artifact_bundle"]["raw_files"],"evidence":manifest["artifact_bundle"]["evidence_files"],"normalized":normalized_sha},sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    with pytest.raises(ValueError, match="staged artifact does not derive"):
        partial.validate_bundle_artifacts(manifest,root)


@pytest.mark.parametrize(("field", "value"), [("product_code", "cu"), ("encoding", "utf-8")])
def test_v5_raw_descriptor_tamper_is_rejected(tmp_path: Path, field: str, value: str) -> None:
    preparer_path = SERVICE_ROOT.parents[1] / "migrations" / "futures_1m_partial_publication_v1_20260825" / "prepare_pyramid_parquet.py"
    module_spec = importlib.util.spec_from_file_location(f"pyramid_raw_descriptor_{field}", preparer_path); assert module_spec and module_spec.loader
    preparer = importlib.util.module_from_spec(module_spec); sys.modules[module_spec.name] = preparer; module_spec.loader.exec_module(preparer)
    raw=tmp_path / "ag.txt"; evidence=tmp_path / "evidence.txt"; raw.write_text("2026-07-14-09:01\t1\t2\t1\t1.5\t2\t0\n",encoding="gbk"); evidence.write_text("e",encoding="utf-8")
    spec=tmp_path / "spec.json"; spec.write_text(json.dumps({"dataset_id":"future_1m_partial_pyramid","source_id":"pyramid","files":[{"raw_path":str(raw),"product_code":"ag","exchange":"SHFE","evidence_paths":[str(evidence)]}]}),encoding="utf-8")
    result=preparer.prepare_spec(spec,tmp_path / "bundle"); root=Path(result["manifest"]).parent; manifest=json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    manifest["artifact_bundle"]["raw_files"][0][field]=value
    for item in manifest["artifact_bundle"]["files"]:
        if item["role"] == "raw": item[field]=value
    with pytest.raises(ValueError, match="hash|generation|derive|descriptor"):
        partial.validate_bundle_artifacts(manifest,root)


def test_bundle_validation_rejects_tamper_and_path_escape(tmp_path: Path) -> None:
    preparer_path = SERVICE_ROOT.parents[1] / "migrations" / "futures_1m_partial_publication_v1_20260825" / "prepare_pyramid_parquet.py"
    module_spec = importlib.util.spec_from_file_location("pyramid_bundle_preparer", preparer_path)
    assert module_spec and module_spec.loader
    preparer = importlib.util.module_from_spec(module_spec); sys.modules[module_spec.name] = preparer; module_spec.loader.exec_module(preparer)
    raw = tmp_path / "ag.txt"; evidence = tmp_path / "gaps.txt"
    raw.write_text("2026-07-14-09:01\t1\t2\t1\t1.5\t2\t0\n", encoding="gbk"); evidence.write_text("evidence", encoding="utf-8")
    spec = {"dataset_id": "future_1m_partial_pyramid", "source_id": "pyramid_local", "authorization": {"status": "private_research_authorized", "evidence": "test authorization"}, "files": [{"raw_path": str(raw), "product_code": "ag", "exchange": "SHFE", "evidence_paths": [str(evidence)]}]}
    spec_path = tmp_path / "spec.json"; spec_path.write_text(json.dumps(spec), encoding="utf-8")
    result = preparer.prepare_spec(spec_path, tmp_path / "bundle")
    root = Path(result["manifest"]).parent; manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    raw_copy = root / str(manifest["artifact_bundle"]["raw_files"][0]["path"]); raw_copy.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash or size"):
        partial.validate_bundle_artifacts(manifest, root)
    manifest["artifact_bundle"]["files"][0]["path"] = "../outside"
    with pytest.raises(ValueError, match="relative"):
        partial.validate_bundle_artifacts(manifest, root)


def test_coverage_paginates_and_cursor_binds_full_query(monkeypatch: pytest.MonkeyPatch) -> None:
    pin = partial.generation_pin(STATE)
    class Reader:
        def query_batch(self, _sql: str, _params: tuple[object, ...], *, stage: str) -> QueryBatch:
            if stage == "futures_1m_partial_header":
                return QueryBatch(("source_id", "read_series_type", "source_series_state", "source_lineage"), (("pyramid", "back_adjusted_continuous", STATE, LINEAGE),))
            if stage == "futures_1m_partial_coverage_page":
                columns=("product_code","exchange","start_time","end_time","status","evidence_sha256","detail_json","interval_count","excluded_count","residual_count","products")
                products={"ag":{"accepted_count":1,"excluded_count":1,"residual_count":1}}
                rows=(("ag","SHFE","2026-07-14 09:01:00","2026-07-14 09:01:00","accepted","a" * 64,{},3,1,1,products),("ag","SHFE","2026-07-14 09:02:00","2026-07-14 09:02:00","excluded","b" * 64,{},3,1,1,products)) if not _params[-4] else (("ag","SHFE","2026-07-14 09:02:00","2026-07-14 09:02:00","excluded","b" * 64,{},3,1,1,products),("ag","SHFE","2026-07-14 09:03:00","2026-07-14 09:03:00","residual","c" * 64,{},3,1,1,products))
                return QueryBatch(columns, rows)
            assert stage == "futures_1m_partial_intervals"
            return QueryBatch(("product_code", "exchange", "start_time", "end_time", "status", "evidence_sha256", "detail_json"), (
                ("ag", "SHFE", "2026-07-14 09:01:00", "2026-07-14 09:01:00", "accepted", "a" * 64, {"bar_count": 1}),
                ("ag", "SHFE", "2026-07-14 09:02:00", "2026-07-14 09:02:00", "excluded", "b" * 64, {}),
                ("ag", "SHFE", "2026-07-14 09:03:00", "2026-07-14 09:03:00", "residual", "c" * 64, {}),
            ))
    monkeypatch.setattr(partial, "_READ_CLIENT", Reader())
    evidence = partial.validate_futures_1m_partial_publication("future_1m_partial_pyramid", "fmp-v1-" + "a" * 64, "a" * 64, pin, "ag", "2026-07-14 09:01:00", "2026-07-14 09:03:00", include_intervals=False)
    first, cursor, summary = partial.read_futures_1m_partial_coverage_page(evidence, "ag", "2026-07-14 09:01:00", "2026-07-14 09:03:00", 1)
    second, _, _ = partial.read_futures_1m_partial_coverage_page(evidence, "ag", "2026-07-14 09:01:00", "2026-07-14 09:03:00", 10, cursor)
    assert first[0]["status"] == "accepted" and [x["status"] for x in second] == ["excluded", "residual"]
    assert summary["products"]["ag"]["accepted_count"] == 1
    with pytest.raises(partial.PartialPublicationQueryError):
        partial.read_futures_1m_partial_coverage_page(evidence, "cu", "2026-07-14 09:01:00", "2026-07-14 09:03:00", 10, cursor)


def test_publish_gate_requires_structured_retention_authorization() -> None:
    canonical = {"source_lineage": {"license": "retention_unverified"}, "authorization": {}}
    with pytest.raises(ValueError, match="structured"):
        partial._require_publish_authorization(canonical)
    partial._require_publish_authorization({"source_lineage": {"license": "retention_unverified"}, "authorization": {"status": "private_research_authorized", "evidence": "user authorization", "no_redistribution": True, "private_server_scope": True, "semantic_limitations_acknowledged": True}})


def test_v5_identity_separates_immutable_bars_from_completeness() -> None:
    coverage = {"ag": {"actual_start": "2026-07-14 09:01:00", "actual_end": "2026-07-14 09:02:00", "exchange": "SHFE", "raw_rows": 2, "valid_rows": 2, "conflicting_timestamp_keys": 0, "conflicting_rows_removed": 0, "invalid_ohlcv_rows": 0, "accepted_interval_count": 1, "excluded_interval_count": 0, "residual_interval_count": 0}}
    canonical = {"dataset_id": "future_1m_partial_pyramid", "source_id": "pyramid", "read_series_type": "back_adjusted_continuous", "source_series_state": {"kind":"artifact_bundle","generation_id":"a"*64,"row_count":2,"first_bar_time":"2026-07-14 09:01:00","last_bar_time":"2026-07-14 09:02:00"}, "normalized_row_count": 2, "source_lineage": {**LINEAGE, "staged_rowset_sha256":"f"*64, "product_coverage": coverage}, "interval_artifact": {"sha256":"b"*64,"rowset_sha256":"c"*64,"row_count":1,"status_counts":{"accepted":1,"excluded":0,"residual":0},"product_counts":{"ag":{"accepted":1,"excluded":0,"residual":0}}}}
    version = partial.partial_dataset_version(canonical); revision = partial.partial_completeness_revision(canonical, version)
    changed_interval = {**canonical, "interval_artifact": {**canonical["interval_artifact"], "sha256":"d"*64}}
    assert partial.partial_dataset_version(changed_interval) == version
    assert partial.partial_completeness_revision(changed_interval, version) != revision
    changed_bars = {**canonical, "source_lineage": {**canonical["source_lineage"], "normalized_rowset_sha256":"e"*64}}
    assert partial.partial_dataset_version(changed_bars) != version
    changed_semantic = {**canonical, "source_lineage": {**canonical["source_lineage"], "adjustment":"different"}}
    assert partial.partial_dataset_version(changed_semantic) != version


def test_revision_sql_persists_authorization_json() -> None:
    source = inspect.getsource(partial.publish_futures_1m_partial_manifest)
    assert source.count("authorization_json") >= 2
    assert "json.dumps(canonical[\"authorization\"],sort_keys=True)" in source
    assert "source_lineage,authorization_json,manifest_sha256" in source
    assert "existing partial revision conflicts" in source
    assert "generation_pin(stored_state)" in source
