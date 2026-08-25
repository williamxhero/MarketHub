"""Immutable, source-specific partial publications for futures 1m research reads.

This intentionally sits beside the all-or-nothing futures completeness gate.
It never turns a missing or conflicting minute into ``known_no_bar`` and it
never changes the legacy ``future_bar_1m`` publication identity.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import base64
import bisect
import binascii
import csv
import hashlib
import json
import math
import os
import re
from typing import Any

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg import sql
from psycopg.rows import dict_row

from quotemux.futures import normalize_product_codes
from quotemux.infra.db.read_client import QueryBatch, ReadOnlyClient


_DATASET_PREFIX = "future_1m_partial_"
_SERIES_TYPES = frozenset(("back_adjusted_continuous", "main_continuous"))
_INTERVAL_STATUSES = frozenset(("accepted", "excluded", "residual"))
_READ_CLIENT = ReadOnlyClient()
_NORMALIZED_COLUMNS = ("product_code", "exchange", "bar_time", "open", "high", "low", "close", "volume", "open_interest", "adjustment_offset", "source_key")
_STAGED_COLUMNS = ("product_code", "exchange", "raw_path", "source_line", "bar_time", "open", "high", "low", "close", "volume", "adjustment_offset", "timestamp_group", "status", "reason")
_PARTIAL_BAR_STAGE_DDL = """create temporary table future_1m_partial_bar_stage (
    product_code text not null, exchange text not null, bar_time timestamp without time zone not null,
    open double precision not null, high double precision not null, low double precision not null,
    close double precision not null, volume double precision not null, open_interest double precision,
    adjustment_offset double precision, source_key text not null,
    check (high >= greatest(open,close) and low <= least(open,close) and high >= low),
    check (volume >= 0), check (open_interest is null or open_interest >= 0)
) on commit drop"""


class PartialPublicationQueryError(ValueError):
    """Malformed partial-read request or cursor (HTTP 400)."""


class PartialPublicationStaleError(ValueError):
    """A requested immutable publication identity is absent or changed (HTTP 409)."""


@dataclass(frozen=True)
class IntervalArtifactValidation:
    path: Path
    count: int
    rowset_sha256: str
    status_counts: dict[str, int]
    product_counts: dict[str, dict[str, int]]


# This parser/classifier is deliberately shared with the diagnostic preparer.
# Raw-to-staged provenance is only meaningful when both sides use exactly one
# interpretation of the vendor rows and duplicate timestamp groups.
def pyramid_timestamp(value: str) -> datetime:
    value = value.strip().replace("/", "-")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.strptime(value, "%Y-%m-%d-%H:%M")


def parse_pyramid_raw_fields(fields: list[str]) -> tuple[datetime, dict[str, float], str]:
    if len(fields) != 7:
        raise ValueError("schema must contain exactly 7 columns")
    timestamp = pyramid_timestamp(fields[0])
    try:
        values = [float(value.strip()) for value in fields[1:]]
    except (ValueError, OverflowError) as exc:
        raise ValueError("malformed numeric field") from exc
    if not all(math.isfinite(value) for value in values):
        raise ValueError("non-finite numeric field")
    open_, high, low, close, volume, offset = values
    if volume < 0:
        raise ValueError("negative volume")
    return timestamp, {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "adjustment_offset": offset}, "valid" if high >= max(open_, close) and low <= min(open_, close) and high >= low else "invalid_ohlc"


def iter_pyramid_timestamp_groups(path: Path, encoding: str):
    """Yield monotonic raw timestamp groups without retaining the full file."""
    with path.open("r", encoding=encoding, newline="") as stream:
        reader = csv.reader(stream, delimiter="\t")
        current: datetime | None = None; previous: datetime | None = None
        rows: list[dict[str, object]] = []; raw_lines: list[str] = []
        for fields in reader:
            if not fields:
                continue
            try:
                timestamp, values, status = parse_pyramid_raw_fields(fields)
            except ValueError as exc:
                raise ValueError(f"{path}:{reader.line_num}: {exc}") from exc
            if previous is not None and timestamp < previous:
                raise ValueError(f"{path}:{reader.line_num}: raw timestamps are not monotonic")
            previous = timestamp
            if current is not None and timestamp != current:
                yield current, rows, hashlib.sha256("\n".join(raw_lines).encode()).hexdigest()
                rows = []; raw_lines = []
            current = timestamp
            rows.append({**values, "_status": status, "_source_line": reader.line_num})
            raw_lines.append(",".join(fields))
        if current is not None:
            yield current, rows, hashlib.sha256("\n".join(raw_lines).encode()).hexdigest()


def classify_pyramid_timestamp_group(items: Sequence[Mapping[str, object]]) -> tuple[bool, str]:
    invalid = len(items) != 1 or items[0].get("_status") != "valid"
    return invalid, "conflicting_timestamp_group" if len(items) != 1 else ("invalid_ohlc" if invalid else "")


def canonical_staged_row_bytes(row: Mapping[str, object]) -> bytes:
    return json.dumps({name: row.get(name) for name in _STAGED_COLUMNS}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode() + b"\n"


def _iter_interval_jsonl(path: Path):
    """Bounded v4 interval reader: one canonical line at a time, never a list."""
    previous: tuple[str, datetime, datetime] | None = None
    with path.open("rb") as stream:
        for line_number, raw_line in enumerate(stream, 1):
            if not raw_line.endswith(b"\n"):
                raise ValueError("interval JSONL line must end with newline")
            try:
                raw = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid interval JSONL line {line_number}") from exc
            item = _validated_entries([raw])[0]
            start = _timestamp(item["start_time"], "start_time"); end = _timestamp(item["end_time"], "end_time")
            key = (str(item["product_code"]), start, end)
            if previous is not None and (key[:2] <= previous[:2] or (key[0] == previous[0] and start <= previous[2])):
                raise ValueError("interval JSONL must be strictly ordered and non-overlapping")
            previous = key
            if canonical_interval_json(item) != raw_line:
                raise ValueError("interval JSONL is not canonical compact JSON")
            yield item


def canonical_interval_json(item: Mapping[str, object]) -> bytes:
    return json.dumps({"product_code":item["product_code"],"exchange":item["exchange"],"start_time":item["start_time"],"end_time":item["end_time"],"status":item["status"],"evidence_sha256":item["evidence_sha256"],"detail":item["detail"]}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode() + b"\n"


def validate_interval_artifact(path: Path, descriptor: Mapping[str, object]) -> IntervalArtifactValidation:
    if not path.is_file() or _file_sha256(path) != _sha(descriptor.get("sha256"), "interval artifact sha256"):
        raise ValueError("interval artifact is missing or tampered")
    count=0; digest=hashlib.sha256(); statuses={status:0 for status in _INTERVAL_STATUSES}; products: dict[str,dict[str,int]]={}
    for item in _iter_interval_jsonl(path):
        count+=1; digest.update(canonical_interval_json(item)); statuses[str(item["status"])] += 1
        bucket=products.setdefault(str(item["product_code"]), {status:0 for status in _INTERVAL_STATUSES}); bucket[str(item["status"])] += 1
    if count != int(descriptor.get("row_count", -1)) or digest.hexdigest() != _sha(descriptor.get("rowset_sha256"), "interval artifact rowset_sha256"):
        raise ValueError("interval artifact count or rowset differs from manifest")
    if descriptor.get("status_counts") != statuses or descriptor.get("product_counts") != products:
        raise ValueError("interval artifact summaries differ from manifest")
    return IntervalArtifactValidation(path,count,digest.hexdigest(),statuses,products)


def _staged_product_coverage(path: Path) -> dict[str, dict[str, object]]:
    parquet=pq.ParquetFile(path)
    if tuple(parquet.schema_arrow.names)!=_STAGED_COLUMNS: raise ValueError("staged parquet schema mismatch")
    result:dict[str,dict[str,object]]={}; seen_conflicts:set[tuple[str,str]]=set()
    for batch in parquet.iter_batches(batch_size=100_000):
        for row in pa.Table.from_batches([batch]).to_pylist():
            p=str(row["product_code"]); t=_timestamp(row["bar_time"],"bar_time"); item=result.setdefault(p,{"actual_start":t,"actual_end":t,"exchange":str(row["exchange"]),"raw_rows":0,"valid_rows":0,"conflicting_timestamp_keys":0,"conflicting_rows_removed":0,"invalid_ohlcv_rows":0})
            item["actual_start"]=min(item["actual_start"],t);item["actual_end"]=max(item["actual_end"],t);item["raw_rows"]+=1
            if row["status"]=="valid":
                item["valid_rows"]+=1;item["valid_start"]=min(item.get("valid_start",t),t);item["valid_end"]=max(item.get("valid_end",t),t)
            elif row["reason"]=="conflicting_timestamp_group":
                item["conflicting_rows_removed"]+=1; key=(p,str(row["timestamp_group"]));
                if key not in seen_conflicts:seen_conflicts.add(key);item["conflicting_timestamp_keys"]+=1
            elif row["reason"]=="invalid_ohlc": item["invalid_ohlcv_rows"]+=1
    return result


def _staged_rowset(path: Path) -> tuple[int, str]:
    parquet = pq.ParquetFile(path)
    if tuple(parquet.schema_arrow.names) != _STAGED_COLUMNS:
        raise ValueError("staged parquet schema mismatch")
    if any(str(parquet.metadata.row_group(group).column(column).compression).upper() != "SNAPPY" for group in range(parquet.metadata.num_row_groups) for column in range(parquet.metadata.row_group(group).num_columns)):
        raise ValueError("staged parquet must use SNAPPY")
    count = 0; digest = hashlib.sha256()
    for batch in parquet.iter_batches(batch_size=100_000):
        for row in pa.Table.from_batches([batch]).to_pylist():
            digest.update(canonical_staged_row_bytes(row)); count += 1
    return count, digest.hexdigest()


def _derived_staged_rowset_from_raw(bundle_root: Path, raw_descriptors: Sequence[Mapping[str, object]]) -> tuple[int, str]:
    """Reconstruct staged canonical rows from copied raw artifacts, streaming."""
    digest = hashlib.sha256(); count = 0
    products: set[str] = set()
    for descriptor in raw_descriptors:
        product_values = normalize_product_codes([str(descriptor.get("product_code", ""))])
        product = product_values[0] if len(product_values) == 1 else ""
        exchange = str(descriptor.get("exchange", "")).strip()
        encoding = str(descriptor.get("encoding", "")).strip()
        raw_path = str(descriptor.get("path", ""))
        if not product or not exchange or not encoding or product in products or descriptor.get("logical_name") != f"{product}_source":
            raise ValueError("v5 raw descriptor requires unique product_code, exchange, encoding, and logical_name")
        products.add(product)
        for timestamp, items, _group_hash in iter_pyramid_timestamp_groups(_bundle_file(bundle_root, raw_path), encoding):
            invalid, reason = classify_pyramid_timestamp_group(items)
            for item in items:
                row = {"product_code": product, "exchange": exchange, "raw_path": raw_path, "source_line": item["_source_line"], "bar_time": timestamp.isoformat(sep=" "), **{name: item.get(name) for name in ("open", "high", "low", "close", "volume", "adjustment_offset")}, "timestamp_group": f"{product}|{timestamp.isoformat(sep=' ')}", "status": "excluded" if invalid else "valid", "reason": reason}
                digest.update(canonical_staged_row_bytes(row)); count += 1
    return count, digest.hexdigest()


def _normalized_product_coverage(path: Path) -> dict[str, dict[str, object]]:
    parquet=pq.ParquetFile(path)
    if tuple(parquet.schema_arrow.names)!=_NORMALIZED_COLUMNS: raise ValueError("normalized parquet schema mismatch")
    result:dict[str,dict[str,object]]={}
    for batch in parquet.iter_batches(batch_size=100_000):
        for row in pa.Table.from_batches([batch]).to_pylist():
            p=str(row["product_code"]);t=_timestamp(row["bar_time"],"bar_time");item=result.setdefault(p,{"start":t,"end":t,"rows":0})
            item["start"]=min(item["start"],t);item["end"]=max(item["end"],t);item["rows"]+=1
    return result


def _derived_normalized_rowset_from_staged(path: Path, raw_sha_by_path: Mapping[str, str]) -> tuple[int, str]:
    parquet=pq.ParquetFile(path); digest=hashlib.sha256();count=0
    for batch in parquet.iter_batches(batch_size=100_000):
        for row in pa.Table.from_batches([batch]).to_pylist():
            if row["status"] != "valid": continue
            raw_path=str(row["raw_path"])
            if raw_path not in raw_sha_by_path: raise ValueError("staged raw_path is not a declared bundle raw artifact")
            normalized={"product_code":str(row["product_code"]),"exchange":str(row["exchange"]),"bar_time":str(row["bar_time"]),"open":float(row["open"]),"high":float(row["high"]),"low":float(row["low"]),"close":float(row["close"]),"volume":float(row["volume"]),"open_interest":None,"adjustment_offset":None if row["adjustment_offset"] is None else float(row["adjustment_offset"]),"source_key":"pyramid:"+raw_sha_by_path[raw_path]}
            digest.update(canonical_normalized_row_bytes(normalized));count+=1
    return count,digest.hexdigest()


def _interval_rows(path: Path, dataset_id: str, dataset_version: str, revision: str | None = None):
    for item in _iter_interval_jsonl(path):
        base=(dataset_id,dataset_version)
        if revision is not None: base+=(revision,)
        yield (*base,item["product_code"],item["exchange"],item["start_time"],item["end_time"],item["status"],item["evidence_sha256"],json.dumps(item["detail"],sort_keys=True))


def _executemany_interval_stream(cursor: Any, statement: str, rows: Any, batch_size: int = 10_000) -> None:
    batch=[]
    for row in rows:
        batch.append(row)
        if len(batch)>=batch_size:
            cursor.executemany(statement,batch);batch.clear()
    if batch: cursor.executemany(statement,batch)

_DDL = """
create schema if not exists readmodel;
create table if not exists readmodel.future_1m_partial_publication (
    dataset_id text not null,
    dataset_version text not null,
    partial_completeness_revision text not null,
    source_id text not null,
    read_series_type text not null check (read_series_type in ('back_adjusted_continuous','main_continuous')),
    source_series_state jsonb not null,
    source_lineage jsonb not null,
    row_count bigint not null check (row_count > 0),
    rowset_sha256 text not null,
    manifest_sha256 text not null,
    published_at_utc timestamp with time zone not null default clock_timestamp(),
    primary key (dataset_id, dataset_version),
    unique (dataset_id, partial_completeness_revision),
    check (dataset_id like 'future_1m_partial_%'),
    check (dataset_version ~ '^fmp-v1-[0-9a-f]{64}$'),
    check (partial_completeness_revision ~ '^[0-9a-f]{64}$'),
    check (manifest_sha256 ~ '^[0-9a-f]{64}$')
);
create table if not exists readmodel.future_1m_partial_bar (
    dataset_id text not null,
    dataset_version text not null,
    product_code text not null,
    exchange text not null,
    bar_time timestamp without time zone not null,
    open double precision not null,
    high double precision not null,
    low double precision not null,
    close double precision not null,
    volume double precision not null,
    open_interest double precision,
    adjustment_offset double precision,
    source_key text not null,
    primary key (dataset_id,dataset_version,product_code,bar_time),
    foreign key (dataset_id,dataset_version)
        references readmodel.future_1m_partial_publication(dataset_id,dataset_version),
    check (high >= greatest(open,close) and low <= least(open,close) and high >= low),
    check (volume is null or volume >= 0),
    check (open_interest is null or open_interest >= 0)
);
create index if not exists future_1m_partial_bar_page_idx
    on readmodel.future_1m_partial_bar(dataset_id,dataset_version,bar_time,product_code);
create table if not exists readmodel.future_1m_partial_interval (
    dataset_id text not null,
    dataset_version text not null,
    product_code text not null,
    exchange text not null,
    start_time timestamp without time zone not null,
    end_time timestamp without time zone not null,
    status text not null check (status in ('accepted','excluded','residual')),
    evidence_sha256 text not null,
    detail_json jsonb not null default '{}'::jsonb,
    primary key (dataset_id, dataset_version, product_code, start_time, end_time),
    foreign key (dataset_id, dataset_version)
        references readmodel.future_1m_partial_publication(dataset_id, dataset_version),
    check (start_time <= end_time),
    check (evidence_sha256 ~ '^[0-9a-f]{64}$')
);
create index if not exists future_1m_partial_interval_lookup_idx
    on readmodel.future_1m_partial_interval(dataset_id,dataset_version,product_code,start_time,end_time);
create table if not exists readmodel.future_1m_partial_revision (
    dataset_id text not null,
    dataset_version text not null,
    partial_completeness_revision text not null,
    manifest_sha256 text not null,
    source_lineage jsonb not null,
    authorization_json jsonb not null default '{}'::jsonb,
    created_at_utc timestamp with time zone not null default clock_timestamp(),
    primary key (dataset_id,dataset_version,partial_completeness_revision),
    foreign key (dataset_id,dataset_version) references readmodel.future_1m_partial_publication(dataset_id,dataset_version)
);
alter table readmodel.future_1m_partial_revision add column if not exists authorization_json jsonb not null default '{}'::jsonb;
create table if not exists readmodel.future_1m_partial_revision_interval (
    dataset_id text not null, dataset_version text not null, partial_completeness_revision text not null,
    product_code text not null, exchange text not null, start_time timestamp without time zone not null, end_time timestamp without time zone not null,
    status text not null check (status in ('accepted','excluded','residual')), evidence_sha256 text not null, detail_json jsonb not null default '{}'::jsonb,
    primary key(dataset_id,dataset_version,partial_completeness_revision,product_code,start_time,end_time),
    foreign key(dataset_id,dataset_version,partial_completeness_revision) references readmodel.future_1m_partial_revision(dataset_id,dataset_version,partial_completeness_revision)
);
create index if not exists future_1m_partial_revision_interval_lookup_idx on readmodel.future_1m_partial_revision_interval(dataset_id,dataset_version,partial_completeness_revision,product_code,start_time,end_time);
"""


@dataclass(frozen=True)
class PartialPublicationEvidence:
    dataset_id: str
    dataset_version: str
    partial_completeness_revision: str
    generation_pin: str
    source_id: str
    read_series_type: str
    source_lineage: dict[str, object]
    accepted: tuple[dict[str, object], ...]
    skipped: tuple[dict[str, object], ...]
    residual: tuple[dict[str, object], ...]


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def partial_dataset_version(canonical: Mapping[str, object]) -> str:
    """Immutable bar/source identity; deliberately excludes coverage and authorization."""
    lineage=canonical["source_lineage"];state=canonical["source_series_state"]
    products={p:{k:v.get(k) for k in ("actual_start","actual_end","exchange","raw_rows","valid_rows","conflicting_timestamp_keys","conflicting_rows_removed","invalid_ohlcv_rows")} for p,v in lineage["product_coverage"].items()}
    identity={"dataset_id":canonical["dataset_id"],"source_id":canonical["source_id"],"read_series_type":canonical["read_series_type"],"source_series_state":state,"normalized_row_count":canonical["normalized_row_count"],"products":products,**{k:lineage[k] for k in ("normalized_artifact_sha256","normalized_rowset_sha256","raw_artifact_sha256","staged_artifact_sha256","staged_rowset_sha256","provider","provider_package_version","timestamp_contract","adjustment","roll_mapping","fields","oi_semantics","timezone","bar_label","units","source_boundary","license")}}
    return f"fmp-v1-{_canonical_hash(identity)}"


def partial_completeness_revision(canonical: Mapping[str, object], dataset_version: str) -> str:
    lineage=canonical["source_lineage"];coverage={p:{k:v.get(k) for k in ("accepted_interval_count","excluded_interval_count","residual_interval_count")} for p,v in lineage["product_coverage"].items()}
    return _canonical_hash({"dataset_version":dataset_version,"interval_artifact":canonical["interval_artifact"],"interval_counts":coverage,"missing_bar_semantics":lineage["missing_bar_semantics"],**{k:lineage[k] for k in ("catalog_version","calendar_version","session_contract","session_evidence_sha256")}})


def partial_manifest_sha256(canonical: Mapping[str, object]) -> str:
    return _canonical_hash({key:value for key,value in canonical.items() if key not in {"interval_path","entries"}})


def canonical_normalized_row_bytes(row: Mapping[str, object]) -> bytes:
    """Stable artifact row serializer; preparation code must use this exact contract."""
    return json.dumps({name: row.get(name) for name in _NORMALIZED_COLUMNS}, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str).encode() + b"\n"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp(value: object, field: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if result.tzinfo is not None:
        raise ValueError(f"{field} must be timezone-naive Asia/Shanghai")
    return result


def _sha(value: object, field: str) -> str:
    result = str(value or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", result) is None:
        raise ValueError(f"{field} must be a SHA-256")
    return result


def _source_state(value: Mapping[str, object]) -> dict[str, object]:
    if value.get("kind") == "artifact_bundle":
        required = ("generation_id", "row_count", "first_bar_time", "last_bar_time")
        if any(name not in value for name in required):
            raise ValueError("artifact_bundle source_series_state is incomplete")
        state = {"kind": "artifact_bundle", "generation_id": _sha(value["generation_id"], "generation_id"), "row_count": int(value["row_count"]), "first_bar_time": str(value["first_bar_time"] or ""), "last_bar_time": str(value["last_bar_time"] or "")}
        if state["row_count"] < 1 or not state["first_bar_time"] or not state["last_bar_time"]:
            raise ValueError("artifact_bundle source_series_state is invalid")
        _timestamp(state["first_bar_time"], "first_bar_time"); _timestamp(state["last_bar_time"], "last_bar_time")
        return state
    required = ("generation", "row_count", "first_bar_time", "last_bar_time", "transaction_id", "operation", "delta_fingerprint")
    if any(name not in value for name in required):
        raise ValueError("source_series_state is incomplete")
    state = {
        "generation": int(value["generation"]), "row_count": int(value["row_count"]),
        "first_bar_time": str(value["first_bar_time"] or ""), "last_bar_time": str(value["last_bar_time"] or ""),
        "transaction_id": int(value["transaction_id"]), "operation": str(value["operation"]),
        "delta_fingerprint": str(value["delta_fingerprint"]),
    }
    if state["generation"] < 1 or state["row_count"] < 0 or not state["operation"] or not state["delta_fingerprint"]:
        raise ValueError("source_series_state is invalid")
    return state


def generation_pin(state: Mapping[str, object]) -> str:
    return f"fmpg-v1-{_canonical_hash(_source_state(state))}"


def _validated_entries(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError("entries must be a non-empty list")
    result: list[dict[str, object]] = []
    previous: dict[str, datetime] = {}
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("interval entry must be an object")
        product = normalize_product_codes([str(raw.get("product_code", ""))])
        if len(product) != 1:
            raise ValueError("interval entry requires one product_code")
        exchange = str(raw.get("exchange", "")).strip()
        status = str(raw.get("status", "")).strip()
        start, end = _timestamp(raw.get("start_time", ""), "start_time"), _timestamp(raw.get("end_time", ""), "end_time")
        if not exchange or status not in _INTERVAL_STATUSES or start > end:
            raise ValueError("interval entry has invalid exchange, status, or bounds")
        detail = raw.get("detail", {})
        json.dumps(detail, sort_keys=True, separators=(",", ":"), default=str)
        item = {"product_code": product[0], "exchange": exchange, "start_time": start.isoformat(sep=" "), "end_time": end.isoformat(sep=" "), "status": status, "evidence_sha256": _sha(raw.get("evidence_sha256"), "evidence_sha256"), "detail": detail}
        if product[0] in previous and start <= previous[product[0]]:
            raise ValueError(f"partial publication intervals overlap: {product[0]}")
        previous[product[0]] = end
        result.append(item)
    return sorted(result, key=lambda item: (str(item["product_code"]), str(item["start_time"]), str(item["end_time"])))


def _validated_manifest(manifest: Mapping[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
    dataset_id = str(manifest.get("dataset_id", "")).strip()
    source_id = str(manifest.get("source_id", "")).strip()
    read_series_type = str(manifest.get("read_series_type", "")).strip()
    lineage = manifest.get("source_lineage")
    if not dataset_id.startswith(_DATASET_PREFIX) or not source_id or read_series_type not in _SERIES_TYPES or not isinstance(lineage, Mapping):
        raise ValueError("manifest requires a source-specific dataset_id, source_id, read_series_type, and source_lineage")
    required_lineage = ("provider", "provider_package_version", "timestamp_contract", "adjustment", "roll_mapping", "license", "raw_artifact_sha256", "staged_artifact_sha256", "normalized_artifact_sha256", "normalized_rowset_sha256", "fields", "oi_semantics", "catalog_version", "calendar_version", "session_contract", "session_evidence_sha256", "timezone", "bar_label", "units", "source_boundary", "missing_bar_semantics")
    if any(str(lineage.get(name, "")).strip() == "" for name in required_lineage):
        raise ValueError("source_lineage must state provider, version, timestamp, adjustment, roll mapping, and license")
    for name in ("raw_artifact_sha256", "staged_artifact_sha256", "normalized_artifact_sha256", "normalized_rowset_sha256", "session_evidence_sha256"):
        _sha(lineage[name], f"source_lineage.{name}")
    authorization = manifest.get("authorization")
    state_value = manifest.get("source_series_state")
    if not isinstance(state_value, Mapping):
        raise ValueError("manifest requires source_series_state")
    state = _source_state(state_value)
    if manifest.get("schema_version") != "futures_pyramid_partial_bundle_v5":
        # Legacy fixtures may exercise local Parquet diagnostics, but this path
        # never reaches bundle publication.
        entries = _validated_entries(manifest.get("entries"))
        coverage = manifest.get("product_coverage", {})
        canonical = {"dataset_id": dataset_id, "source_id": source_id, "read_series_type": read_series_type, "source_series_state": state, "source_lineage": {**dict(lineage), "product_coverage": dict(coverage or {})}, "authorization": dict(authorization) if isinstance(authorization, Mapping) else {}, "entries": entries, "normalized_row_count": int(manifest.get("normalized_row_count", 0)), "diagnostic_only": True}
        return canonical, entries
    if not str(lineage.get("staged_rowset_sha256", "")).strip():
        raise ValueError("v5 source_lineage requires staged_rowset_sha256")
    _sha(lineage["staged_rowset_sha256"], "source_lineage.staged_rowset_sha256")
    if "entries" in manifest:
        raise ValueError("v5 manifest must not inline entries")
    interval_artifact = manifest.get("interval_artifact")
    if not isinstance(interval_artifact, Mapping) or int(interval_artifact.get("row_count", 0)) < 1:
        raise ValueError("manifest requires v5 interval_artifact descriptor")
    for name in ("sha256", "rowset_sha256"):
        _sha(interval_artifact.get(name), f"interval_artifact.{name}")
    entries: list[dict[str, object]] = []
    coverage = manifest.get("product_coverage")
    if not isinstance(coverage, Mapping) or not coverage:
        raise ValueError("manifest requires product_coverage")
    normalized_row_count = int(manifest.get("normalized_row_count", 0))
    if normalized_row_count < 1:
        raise ValueError("manifest requires positive normalized_row_count")
    canonical = {"dataset_id": dataset_id, "source_id": source_id, "read_series_type": read_series_type, "source_series_state": state, "source_lineage": {**dict(lineage), "product_coverage": dict(coverage)}, "authorization": dict(authorization) if isinstance(authorization, Mapping) else {}, "interval_artifact":dict(interval_artifact), "normalized_row_count": normalized_row_count}
    return canonical, entries


def _require_publish_authorization(canonical: Mapping[str, object]) -> None:
    """Diagnostic preparation is allowed; retention is authorized only at publication."""
    lineage = canonical["source_lineage"]
    license_state = str(lineage.get("license", ""))
    if license_state in {"redistribution_permitted", "private_research_permitted"}:
        return
    authorization = canonical.get("authorization")
    required = ("evidence", "no_redistribution", "private_server_scope", "semantic_limitations_acknowledged")
    if license_state != "retention_unverified" or not isinstance(authorization, Mapping) or authorization.get("status") != "private_research_authorized" or not str(authorization.get("evidence", "")).strip() or authorization.get("no_redistribution") is not True or authorization.get("private_server_scope") is not True or authorization.get("semantic_limitations_acknowledged") is not True:
        raise ValueError("retention_unverified publication requires structured private-research authorization, evidence, no_redistribution, private_server_scope, and semantic_limitations_acknowledged")


def _validated_bars(value: object, entries: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError("bars must be a non-empty source-native row list")
    accepted = [item for item in entries if item["status"] == "accepted"]
    result: list[dict[str, object]] = []
    seen: set[tuple[str, datetime]] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("bar must be an object")
        products = normalize_product_codes([str(raw.get("product_code", ""))])
        if len(products) != 1:
            raise ValueError("bar requires one product_code")
        product = products[0]
        exchange = str(raw.get("exchange", "")).strip()
        bar_time = _timestamp(raw.get("bar_time", ""), "bar_time")
        key = (product, bar_time)
        if key in seen:
            raise ValueError(f"source-native bar duplicate: {product} {bar_time.isoformat(sep=' ')}")
        seen.add(key)
        try:
            open_, high, low, close = (float(raw[name]) for name in ("open", "high", "low", "close"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("bar requires numeric OHLC") from exc
        if high < max(open_, close) or low > min(open_, close) or high < low:
            raise ValueError("bar has invalid OHLC")
        def optional_nonnegative(name: str) -> float | None:
            value = raw.get(name)
            if value is None or str(value).strip() == "": return None
            number = float(value)
            if number < 0: raise ValueError(f"bar {name} must be non-negative")
            return number
        volume = optional_nonnegative("volume")
        if volume is None:
            raise ValueError("bar volume is required")
        if not exchange or not any(str(item["product_code"]) == product and str(item["exchange"]) == exchange and _timestamp(item["start_time"], "start_time") <= bar_time <= _timestamp(item["end_time"], "end_time") for item in accepted):
            raise ValueError("bar is outside accepted intervals")
        result.append({"product_code": product, "exchange": exchange, "bar_time": bar_time.isoformat(sep=" "), "open": open_, "high": high, "low": low, "close": close, "volume": volume, "open_interest": optional_nonnegative("open_interest"), "adjustment_offset": None if raw.get("adjustment_offset") in (None, "") else float(raw["adjustment_offset"]), "source_key": str(raw.get("source_key", "")).strip()})
    if any(not item["source_key"] for item in result):
        raise ValueError("bar source_key is required")
    return sorted(result, key=lambda item: (str(item["bar_time"]), str(item["product_code"])))


def _connect() -> psycopg.Connection[Any]:
    """Owner-only connection; the API always uses the read role/client."""
    owner_user = os.environ["MARKETHUB_FUTURES_PARTIAL_PUBLISH_DB_USER"]
    if owner_user == os.environ["MARKETHUB_DB_USER"]:
        raise RuntimeError("partial publisher must use a distinct owner role, never the API read role")
    return psycopg.connect(host=os.getenv("MARKETHUB_FUTURES_PARTIAL_PUBLISH_DB_HOST", os.environ["MARKETHUB_DB_HOST"]), port=int(os.getenv("MARKETHUB_FUTURES_PARTIAL_PUBLISH_DB_PORT", os.environ["MARKETHUB_DB_PORT"])), dbname=os.getenv("MARKETHUB_FUTURES_PARTIAL_PUBLISH_DB_NAME", os.environ["MARKETHUB_DB_NAME"]), user=owner_user, password=os.environ["MARKETHUB_FUTURES_PARTIAL_PUBLISH_DB_PASSWORD"], connect_timeout=10, row_factory=dict_row, application_name="markethub-futures-partial-publisher")


def bootstrap_futures_1m_partial_publication_schema() -> None:
    """Migration-only schema seam; the API role receives read access only."""
    with _connect() as connection:
        connection.execute(_DDL)
        role = os.environ["MARKETHUB_DB_USER"].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role) is None:
            raise RuntimeError("MARKETHUB_DB_USER must be a PostgreSQL identifier")
        ident = sql.Identifier(role)
        tables = "readmodel.future_1m_partial_publication, readmodel.future_1m_partial_interval, readmodel.future_1m_partial_bar, readmodel.future_1m_partial_revision, readmodel.future_1m_partial_revision_interval"
        connection.execute(sql.SQL("grant select on table " + tables + " to {}").format(ident))
        connection.execute(sql.SQL("revoke insert, update, delete, truncate, references, trigger on table " + tables + " from {}").format(ident))
        privileges = connection.execute("select table_name,has_table_privilege(%s,'readmodel.'||table_name,'select') can_select,has_table_privilege(%s,'readmodel.'||table_name,'insert') can_insert,has_table_privilege(%s,'readmodel.'||table_name,'update') can_update,has_table_privilege(%s,'readmodel.'||table_name,'delete') can_delete,has_table_privilege(%s,'readmodel.'||table_name,'truncate') can_truncate,has_table_privilege(%s,'readmodel.'||table_name,'references') can_references,has_table_privilege(%s,'readmodel.'||table_name,'trigger') can_trigger from information_schema.tables where table_schema='readmodel' and table_name=any(%s::text[])", (role, role, role, role, role, role, role, ["future_1m_partial_publication","future_1m_partial_interval","future_1m_partial_bar","future_1m_partial_revision","future_1m_partial_revision_interval"])).fetchall()
        if len(privileges) != 5 or any(not row["can_select"] or row["can_insert"] or row["can_update"] or row["can_delete"] or row["can_truncate"] or row["can_references"] or row["can_trigger"] for row in privileges):
            raise RuntimeError("partial publication API role privilege verification failed")


def _accepted_interval_index(interval_path: Path) -> dict[str, tuple[list[datetime], list[tuple[datetime, datetime, str, int]]]]:
    """Retain only the compact accepted index needed to validate Parquet rows."""
    result: dict[str, tuple[list[datetime], list[tuple[datetime, datetime, str, int]]]] = {}
    for item in _iter_interval_jsonl(interval_path):
        if item["status"] != "accepted":
            continue
        detail=item["detail"] if isinstance(item["detail"],Mapping) else {}
        start=_timestamp(item["start_time"],"start_time"); end=_timestamp(item["end_time"],"end_time")
        declared=detail.get("bar_count")
        if not isinstance(declared,int): raise ValueError("accepted interval requires integer bar_count")
        starts, intervals=result.setdefault(str(item["product_code"]),([],[]));starts.append(start);intervals.append((start,end,str(item["exchange"]),declared))
    return result


def _iter_normalized_parquet(path: Path, entries: Sequence[Mapping[str, object]] | Path):
    required = _NORMALIZED_COLUMNS
    parquet = pq.ParquetFile(path)
    if tuple(parquet.schema_arrow.names) != required:
        raise ValueError("normalized parquet schema mismatch")
    if any(str(parquet.metadata.row_group(group).column(column).compression).upper() != "SNAPPY" for group in range(parquet.metadata.num_row_groups) for column in range(parquet.metadata.row_group(group).num_columns)):
        raise ValueError("normalized parquet must use SNAPPY")
    accepted_index = _accepted_interval_index(entries) if isinstance(entries,Path) else {}
    if not isinstance(entries,Path):
        for entry in entries:
            if entry["status"] != "accepted": continue
            product=str(entry["product_code"]);starts,intervals=accepted_index.setdefault(product,([],[]));start=_timestamp(entry["start_time"],"start_time");starts.append(start);intervals.append((start,_timestamp(entry["end_time"],"end_time"),str(entry["exchange"]),int(entry["detail"]["bar_count"])))
    for starts, intervals in accepted_index.values():
        paired = sorted(zip(starts, intervals, strict=True))
        starts[:] = [item[0] for item in paired]; intervals[:] = [item[1] for item in paired]
    previous: tuple[str, datetime] | None = None
    for batch in parquet.iter_batches(batch_size=100_000):
        for raw in pa.Table.from_batches([batch]).to_pylist():
            products = normalize_product_codes([str(raw.get("product_code", ""))])
            if len(products) != 1:
                raise ValueError("bar requires one product_code")
            product = products[0]
            bar_time = _timestamp(raw.get("bar_time", ""), "bar_time")
            starts, intervals = accepted_index.get(product, ([], []))
            index = bisect.bisect_right(starts, bar_time) - 1
            if index < 0 or bar_time > intervals[index][1] or str(raw.get("exchange", "")).strip() != intervals[index][2]:
                raise ValueError("bar is outside accepted intervals")
            item = _validated_bars([raw], [{"product_code": product, "exchange": intervals[index][2], "start_time": intervals[index][0].isoformat(sep=" "), "end_time": intervals[index][1].isoformat(sep=" "), "status": "accepted", "evidence_sha256": "0" * 64}])[0]
            key = (str(item["product_code"]), _timestamp(item["bar_time"], "bar_time"))
            if previous is not None and key <= previous:
                raise ValueError("normalized parquet must be unique and ordered by product_code,bar_time")
            previous = key
            yield item


def _bundle_file(bundle_root: Path, value: object) -> Path:
    root = bundle_root.resolve(strict=True)
    relative = Path(str(value))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("bundle artifact path must be a portable relative path")
    lexical = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("bundle artifact may not use a symlink")
    candidate = lexical.resolve(strict=True)
    if candidate == root or root not in candidate.parents or not candidate.is_file():
        raise ValueError("bundle artifact escapes root or is missing")
    return candidate


def validate_bundle_artifacts(manifest: Mapping[str, object], bundle_root: Path) -> tuple[dict[str, object], list[dict[str, object]], int, str, Path]:
    """Verify every portable bundle artifact before any database connection is opened."""
    canonical, entries = _validated_manifest(manifest)
    bundle = manifest.get("artifact_bundle")
    if not isinstance(bundle, Mapping) or not isinstance(bundle.get("files"), Sequence):
        raise ValueError("manifest requires a portable artifact_bundle.files list")
    files = bundle["files"]
    required_roles = {"raw", "staged", "normalized", "evidence", "intervals"}
    seen_roles: set[str] = set(); normalized_path: Path | None = None
    for item in files:
        if not isinstance(item, Mapping):
            raise ValueError("bundle artifact entry must be an object")
        role = str(item.get("role", ""))
        path = _bundle_file(bundle_root, item.get("path", ""))
        declared_size = item.get("size_bytes")
        if role not in required_roles or not isinstance(declared_size, int) or declared_size < 0:
            raise ValueError("bundle artifact role or size is invalid")
        if path.stat().st_size != declared_size or _file_sha256(path) != _sha(item.get("sha256"), "bundle artifact sha256"):
            raise ValueError("bundle artifact hash or size differs from manifest")
        seen_roles.add(role)
        if role == "normalized":
            if normalized_path is not None:
                raise ValueError("bundle must contain exactly one normalized artifact")
            normalized_path = path
    if not required_roles <= seen_roles or normalized_path is None:
        raise ValueError("bundle is missing raw, staged, normalized, or evidence artifacts")
    staged_path=_bundle_file(bundle_root,next(item["path"] for item in files if isinstance(item,Mapping) and item.get("role")=="staged"))
    recomputed=_staged_product_coverage(staged_path)
    declared_coverage=canonical["source_lineage"]["product_coverage"]
    if set(recomputed)!=set(declared_coverage): raise ValueError("staged products differ from product_coverage")
    for product,actual in recomputed.items():
        declared=declared_coverage[product]
        expected={key:value for key,value in actual.items() if key not in {"valid_start","valid_end"}};expected.update({"actual_start":actual["actual_start"].isoformat(sep=" "),"actual_end":actual["actual_end"].isoformat(sep=" ")})
        if any(declared.get(key)!=value for key,value in expected.items()): raise ValueError("product_coverage differs from staged artifact")
    normalized_coverage=_normalized_product_coverage(normalized_path)
    if set(normalized_coverage)!={p for p,value in recomputed.items() if int(value["valid_rows"])>0}: raise ValueError("normalized products differ from staged coverage")
    for product,actual in normalized_coverage.items():
        staged=recomputed[product]
        if actual["rows"] != staged["valid_rows"] or actual["start"] != staged["valid_start"] or actual["end"] != staged["valid_end"]:
            raise ValueError("normalized coverage differs from staged valid rows")
    by_role = {role: [item for item in files if isinstance(item, Mapping) and item.get("role") == role] for role in required_roles}
    raw_manifest = bundle.get("raw_files", [])
    evidence_manifest = bundle.get("evidence_files", [])
    if not isinstance(raw_manifest, Sequence) or not isinstance(evidence_manifest, Sequence) or not raw_manifest or not evidence_manifest:
        raise ValueError("bundle requires raw_files and evidence_files")
    def descriptors(role: str) -> list[dict[str, object]]:
        result=[]
        for item in by_role[role]:
            result.append({key:value for key,value in dict(item).items() if key != "role"})
        return result
    if list(raw_manifest) != descriptors("raw") or list(evidence_manifest) != descriptors("evidence"):
        raise ValueError("raw_files/evidence_files must exactly equal role descriptors")
    paths=[str(item.get("path","")) for item in files if isinstance(item,Mapping)]; names=[str(item.get("logical_name","")) for item in files if isinstance(item,Mapping)]
    if len(paths)!=len(set(paths)) or len(names)!=len(set(names)) or len(by_role["staged"])!=1 or len(by_role["normalized"])!=1:
        raise ValueError("bundle descriptors require unique paths/logical names and exactly one staged/normalized artifact")
    if _canonical_hash(list(raw_manifest)) != str(canonical["source_lineage"]["raw_artifact_sha256"]):
        raise ValueError("lineage raw artifact hash does not bind bundle raw files")
    role_hashes = {str(item["role"]): [str(item["sha256"]) for item in by_role[str(item["role"])] ] for item in files if isinstance(item, Mapping) and str(item.get("role")) in {"staged", "normalized"}}
    if str(canonical["source_lineage"]["staged_artifact_sha256"]) not in role_hashes.get("staged", []) or str(canonical["source_lineage"]["normalized_artifact_sha256"]) not in role_hashes.get("normalized", []):
        raise ValueError("lineage staged or normalized hash does not bind bundle")
    evidence_hashes = {str(item.get("sha256")) for item in evidence_manifest if isinstance(item, Mapping)}
    if str(canonical["source_lineage"]["session_evidence_sha256"]) not in evidence_hashes:
        raise ValueError("lineage session evidence hash does not bind bundle evidence")
    actual_staged_count, actual_staged_rowset = _staged_rowset(staged_path)
    derived_staged_count, derived_staged_rowset = _derived_staged_rowset_from_raw(bundle_root, [dict(item) for item in raw_manifest])
    if actual_staged_count != derived_staged_count or actual_staged_rowset != derived_staged_rowset or actual_staged_rowset != str(canonical["source_lineage"]["staged_rowset_sha256"]):
        raise ValueError("staged artifact does not derive from copied raw artifacts")
    derived_count,derived_rowset=_derived_normalized_rowset_from_staged(staged_path,{str(item["path"]):str(item["sha256"]) for item in raw_manifest})
    if derived_count != int(canonical["normalized_row_count"]) or derived_rowset != str(canonical["source_lineage"]["normalized_rowset_sha256"]):
        raise ValueError("staged valid rows do not derive declared normalized rowset")
    interval_files = by_role["intervals"]
    if len(interval_files) != 1:
        raise ValueError("bundle must contain exactly one intervals artifact")
    interval_descriptor = {key:value for key,value in dict(interval_files[0]).items() if key != "role"}
    if dict(canonical["interval_artifact"]) != interval_descriptor:
        raise ValueError("manifest interval_artifact must exactly equal intervals role descriptor")
    interval_validation = validate_interval_artifact(_bundle_file(bundle_root, interval_files[0]["path"]), canonical["interval_artifact"])
    state=canonical["source_series_state"];coverage=canonical["source_lineage"]["product_coverage"]
    if int(state["row_count"]) != int(canonical["normalized_row_count"]) or state["first_bar_time"] != min(str(v["actual_start"]) for v in coverage.values()) or state["last_bar_time"] != max(str(v["actual_end"]) for v in coverage.values()):
        raise ValueError("source state does not match normalized/product coverage")
    raw=bundle.get("raw_files",[]);evidence=bundle.get("evidence_files",[])
    expected_generation=hashlib.sha256(json.dumps({"raw":raw,"evidence":evidence,"normalized":canonical["source_lineage"]["normalized_artifact_sha256"]},sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    if state["generation_id"] != expected_generation: raise ValueError("source generation does not bind immutable bundle artifacts")
    if set(interval_validation.product_counts) != set(canonical["source_lineage"]["product_coverage"]):
        raise ValueError("interval artifact products differ from product_coverage")
    for product, counts in interval_validation.product_counts.items():
        declared = canonical["source_lineage"]["product_coverage"].get(product)
        if not isinstance(declared, Mapping) or any(int(declared.get(f"{status}_interval_count", -1)) != count for status, count in counts.items()):
            raise ValueError("interval artifact product counts differ from product_coverage")
    canonical, entries, count, rowset = validate_normalized_partial_artifact({**dict(manifest), "_interval_path": str(_bundle_file(bundle_root, interval_files[0]["path"]))}, normalized_path)
    if count != derived_count or rowset != derived_rowset:
        raise ValueError("normalized artifact differs from staged-derived canonical rows")
    canonical = {**canonical, "interval_path": str(_bundle_file(bundle_root, interval_files[0]["path"]))}
    return canonical, entries, count, rowset, normalized_path


def validate_normalized_partial_artifact(manifest: Mapping[str, object], normalized_parquet_path: Path) -> tuple[dict[str, object], list[dict[str, object]], int, str]:
    """No-DB fail-closed artifact gate, reusable by release verification and publisher."""
    canonical, entries = _validated_manifest(manifest)
    if not normalized_parquet_path.is_file():
        raise ValueError("normalized parquet is missing")
    if _file_sha256(normalized_parquet_path) != str(canonical["source_lineage"]["normalized_artifact_sha256"]):
        raise ValueError("normalized parquet artifact hash differs from manifest")
    interval_path = Path(str(manifest["_interval_path"])) if manifest.get("_interval_path") else None
    compact = _accepted_interval_index(interval_path) if interval_path else None
    interval_index: dict[str, tuple[list[datetime], list[dict[str, object]]]] = {}
    for item in entries:
        if item["status"] == "accepted":
            starts, items = interval_index.setdefault(str(item["product_code"]), ([], []))
            starts.append(_timestamp(item["start_time"], "start_time")); items.append(item)
    actual_counts: dict[tuple[str, str], int] = {}
    for product, (starts, items) in interval_index.items():
        ordered = sorted(zip(starts, items, strict=True))
        starts[:] = [pair[0] for pair in ordered]; items[:] = [pair[1] for pair in ordered]
    count = 0; row_hash = hashlib.sha256()
    interval_source: Sequence[Mapping[str, object]] | Path = interval_path if interval_path else entries
    for item in _iter_normalized_parquet(normalized_parquet_path, interval_source):
        product=str(item["product_code"]); bar_time=_timestamp(item["bar_time"],"bar_time")
        if compact is not None:
            starts, tuples=compact[product]; index=bisect.bisect_right(starts,bar_time)-1
            interval_start=tuples[index][0]
            key=(product,interval_start.isoformat(sep=" "))
        else:
            starts, items = interval_index[product]; index = bisect.bisect_right(starts, bar_time) - 1
            key = (product, str(items[index]["start_time"]))
        actual_counts[key] = actual_counts.get(key, 0) + 1
        row_hash.update(canonical_normalized_row_bytes(item)); count += 1
    if compact is not None:
        for product,(_starts,tuples) in compact.items():
            for start,end,_exchange,declared in tuples:
                expected=int((end-start).total_seconds()//60)+1
                if declared != expected or actual_counts.get((product,start.isoformat(sep=" ")),0)!=expected: raise ValueError("accepted interval is not proven dense")
    else:
        for product, (_starts, items) in interval_index.items():
            for interval in items:
                start, end = _timestamp(interval["start_time"], "start_time"), _timestamp(interval["end_time"], "end_time")
                expected = int((end - start).total_seconds() // 60) + 1
                declared = interval["detail"].get("bar_count") if isinstance(interval["detail"], Mapping) else None
                if declared != expected or actual_counts.get((product, str(interval["start_time"])), 0) != expected:
                    raise ValueError("accepted interval is not proven dense")
    if count != int(canonical["normalized_row_count"]):
        raise ValueError("normalized parquet row count differs from manifest")
    if row_hash.hexdigest() != str(canonical["source_lineage"]["normalized_rowset_sha256"]):
        raise ValueError("normalized parquet rowset hash differs from manifest")
    return canonical, ([] if interval_path else entries), count, row_hash.hexdigest()


def publish_futures_1m_partial_manifest(manifest: Mapping[str, object], bundle_root: Path) -> dict[str, object]:
    """Bounded Parquet->COPY publication. The source rows never touch global facts."""
    canonical, entries, verified_count, verified_hash, normalized_parquet_path = validate_bundle_artifacts(manifest, bundle_root)
    if canonical.get("diagnostic_only"):
        raise ValueError("v3 diagnostic bundle is not publishable")
    _require_publish_authorization(canonical)
    version = partial_dataset_version(canonical)
    revision = partial_completeness_revision(canonical, version)
    manifest_sha256 = partial_manifest_sha256(canonical)
    with _connect() as connection:
        existing = connection.execute("select partial_completeness_revision,source_series_state from readmodel.future_1m_partial_publication where dataset_id=%s and dataset_version=%s", (canonical["dataset_id"], version)).fetchone()
        if existing is not None:
            prior = connection.execute("select source_lineage,authorization_json,manifest_sha256 from readmodel.future_1m_partial_revision where dataset_id=%s and dataset_version=%s and partial_completeness_revision=%s", (canonical["dataset_id"], version, revision)).fetchone()
            if prior is None:
                connection.execute("insert into readmodel.future_1m_partial_revision(dataset_id,dataset_version,partial_completeness_revision,manifest_sha256,source_lineage,authorization_json) values(%s,%s,%s,%s,%s::jsonb,%s::jsonb)", (canonical["dataset_id"],version,revision,manifest_sha256,json.dumps(canonical["source_lineage"],sort_keys=True),json.dumps(canonical["authorization"],sort_keys=True)))
                with connection.cursor() as cursor:
                    _executemany_interval_stream(cursor,"insert into readmodel.future_1m_partial_revision_interval(dataset_id,dataset_version,partial_completeness_revision,product_code,exchange,start_time,end_time,status,evidence_sha256,detail_json) values(%s,%s,%s,%s,%s,%s::timestamp,%s::timestamp,%s,%s,%s::jsonb)",_interval_rows(Path(str(canonical["interval_path"])),canonical["dataset_id"],version,revision))
            stored_state=_source_state(existing["source_series_state"])
            if prior is not None and (dict(prior["source_lineage"]) != canonical["source_lineage"] or dict(prior["authorization_json"]) != canonical["authorization"] or str(prior["manifest_sha256"]) != manifest_sha256):
                raise ValueError("existing partial revision conflicts with immutable manifest")
            return {"dataset_id": canonical["dataset_id"], "dataset_version": version, "partial_completeness_revision": revision, "generation_pin": generation_pin(stored_state), "idempotent": prior is not None}
        connection.execute(_PARTIAL_BAR_STAGE_DDL)
        count = 0
        row_hash = hashlib.sha256()
        with connection.cursor() as cursor:
            with cursor.copy("copy future_1m_partial_bar_stage(product_code,exchange,bar_time,open,high,low,close,volume,open_interest,adjustment_offset,source_key) from stdin") as copy:
                for item in _iter_normalized_parquet(normalized_parquet_path, Path(str(canonical["interval_path"]))):
                    copy.write_row(tuple(item[name] for name in ("product_code", "exchange", "bar_time", "open", "high", "low", "close", "volume", "open_interest", "adjustment_offset", "source_key")))
                    row_hash.update(canonical_normalized_row_bytes(item)); count += 1
        if count != verified_count:
            raise ValueError("normalized parquet row count differs from manifest")
        rowset_sha256 = row_hash.hexdigest()
        if rowset_sha256 != verified_hash:
            raise ValueError("normalized parquet rowset hash differs from manifest")
        connection.execute("insert into readmodel.future_1m_partial_publication(dataset_id,dataset_version,partial_completeness_revision,source_id,read_series_type,source_series_state,source_lineage,row_count,rowset_sha256,manifest_sha256) values(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)", (canonical["dataset_id"], version, revision, canonical["source_id"], canonical["read_series_type"], json.dumps(canonical["source_series_state"], sort_keys=True), json.dumps(canonical["source_lineage"], sort_keys=True), count, rowset_sha256, manifest_sha256))
        connection.execute("insert into readmodel.future_1m_partial_revision(dataset_id,dataset_version,partial_completeness_revision,manifest_sha256,source_lineage,authorization_json) values(%s,%s,%s,%s,%s::jsonb,%s::jsonb)", (canonical["dataset_id"],version,revision,manifest_sha256,json.dumps(canonical["source_lineage"],sort_keys=True),json.dumps(canonical["authorization"],sort_keys=True)))
        with connection.cursor() as cursor:
            _executemany_interval_stream(cursor,"insert into readmodel.future_1m_partial_interval(dataset_id,dataset_version,product_code,exchange,start_time,end_time,status,evidence_sha256,detail_json) values(%s,%s,%s,%s,%s::timestamp,%s::timestamp,%s,%s,%s::jsonb)",_interval_rows(Path(str(canonical["interval_path"])),canonical["dataset_id"],version))
            _executemany_interval_stream(cursor,"insert into readmodel.future_1m_partial_revision_interval(dataset_id,dataset_version,partial_completeness_revision,product_code,exchange,start_time,end_time,status,evidence_sha256,detail_json) values(%s,%s,%s,%s,%s,%s::timestamp,%s::timestamp,%s,%s,%s::jsonb)",_interval_rows(Path(str(canonical["interval_path"])),canonical["dataset_id"],version,revision))
            cursor.execute("insert into readmodel.future_1m_partial_bar(dataset_id,dataset_version,product_code,exchange,bar_time,open,high,low,close,volume,open_interest,adjustment_offset,source_key) select %s,%s,product_code,exchange,bar_time,open,high,low,close,volume,open_interest,adjustment_offset,source_key from future_1m_partial_bar_stage", (canonical["dataset_id"], version))
    return {"dataset_id": canonical["dataset_id"], "dataset_version": version, "partial_completeness_revision": revision, "generation_pin": generation_pin(canonical["source_series_state"]), "idempotent": False}


def _rows(batch: QueryBatch) -> list[dict[str, object]]:
    return list(batch.as_dicts())


def validate_futures_1m_partial_publication(dataset_id: str, dataset_version: str, partial_completeness_revision: str, generation: str, codes: str, start_time: str, end_time: str, *, include_intervals: bool = True) -> PartialPublicationEvidence:
    products = normalize_product_codes(codes)
    start, end = _timestamp(start_time, "start_time"), _timestamp(end_time, "end_time")
    if not products or start > end or not dataset_id.startswith(_DATASET_PREFIX) or not dataset_version or not partial_completeness_revision or not generation:
        raise PartialPublicationQueryError("dataset identity, revision, generation_pin, codes, and ordered time bounds are required")
    headers = _rows(_READ_CLIENT.query_batch("select publication.source_id,publication.read_series_type,publication.source_series_state,revision.source_lineage from readmodel.future_1m_partial_publication publication join readmodel.future_1m_partial_revision revision using(dataset_id,dataset_version) where publication.dataset_id=%s and publication.dataset_version=%s and revision.partial_completeness_revision=%s", (dataset_id, dataset_version, partial_completeness_revision), stage="futures_1m_partial_header"))
    if len(headers) != 1:
        raise PartialPublicationStaleError("unknown or stale futures partial publication identity")
    header = headers[0]
    frozen_state = _source_state(header["source_series_state"])
    if generation != generation_pin(frozen_state):
        raise PartialPublicationStaleError("generation_pin does not match partial publication")
    # Bars are copied into this immutable publication.  A later mutation of a
    # provider-backed global fact table must not alter or invalidate this
    # frozen snapshot; the pin proves the source-series generation observed at
    # materialization time and is bound into every cursor instead.
    if not include_intervals:
        return PartialPublicationEvidence(dataset_id, dataset_version, partial_completeness_revision, generation, str(header["source_id"]), str(header["read_series_type"]), dict(header["source_lineage"]), (), (), ())
    rows = _rows(_READ_CLIENT.query_batch("select product_code,exchange,start_time::text as start_time,end_time::text as end_time,status,evidence_sha256,detail_json from readmodel.future_1m_partial_revision_interval where dataset_id=%s and dataset_version=%s and partial_completeness_revision=%s and product_code=any(%s::text[]) and end_time >= %s::timestamp and start_time <= %s::timestamp order by product_code,start_time,end_time", (dataset_id, dataset_version, partial_completeness_revision, list(products), start.isoformat(sep=" "), end.isoformat(sep=" ")), stage="futures_1m_partial_intervals"))
    accepted: list[dict[str, object]] = []; skipped: list[dict[str, object]] = []; residual: list[dict[str, object]] = []
    for row in rows:
        item = {**row, "detail": row.get("detail_json", {}), "start_time": max(start, _timestamp(row["start_time"], "start_time")).isoformat(sep=" "), "end_time": min(end, _timestamp(row["end_time"], "end_time")).isoformat(sep=" ")}
        if item["start_time"] > item["end_time"]:
            continue
        if row["status"] == "accepted": accepted.append(item)
        elif row["status"] == "excluded": skipped.append(item)
        else: residual.append(item)
    # An absent declaration is not permission to return an empty-looking page.
    # Make every uncovered requested minute an explicit residual interval.
    for product in products:
        cursor = start
        product_rows = [item for item in rows if str(item["product_code"]) == product]
        for item in product_rows:
            interval_start = _timestamp(item["start_time"], "start_time")
            interval_end = _timestamp(item["end_time"], "end_time")
            if interval_end < cursor:
                continue
            if interval_start > cursor:
                residual.append({"product_code": product, "start_time": cursor.isoformat(sep=" "), "end_time": min(end, interval_start - timedelta(minutes=1)).isoformat(sep=" "), "status": "residual", "detail": {"reason": "undeclared_coverage"}})
            cursor = max(cursor, interval_end + timedelta(minutes=1))
            if cursor > end:
                break
        if cursor <= end:
            residual.append({"product_code": product, "start_time": cursor.isoformat(sep=" "), "end_time": end.isoformat(sep=" "), "status": "residual", "detail": {"reason": "undeclared_coverage"}})
    return PartialPublicationEvidence(dataset_id, dataset_version, partial_completeness_revision, generation, str(header["source_id"]), str(header["read_series_type"]), dict(header["source_lineage"]), tuple(accepted), tuple(skipped), tuple(residual))


def _query_pin(codes: str, start_time: str, end_time: str) -> str:
    return _canonical_hash({"codes": list(normalize_product_codes(codes)), "start_time": start_time, "end_time": end_time})


def coverage_contract_sha256(evidence: PartialPublicationEvidence, codes: str, start_time: str, end_time: str) -> str:
    return _canonical_hash({"algorithm":"fmp-coverage-clipping-synthetic-residual-v1","dataset_id":evidence.dataset_id,"dataset_version":evidence.dataset_version,"revision":evidence.partial_completeness_revision,"generation_pin":evidence.generation_pin,"products":list(normalize_product_codes(codes)),"start_time":start_time,"end_time":end_time})

def _cursor(value: str, evidence: PartialPublicationEvidence, query_pin: str = "") -> tuple[datetime, str] | None:
    if value == "":
        return None
    try:
        raw = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
        if raw["dataset_id"] != evidence.dataset_id or raw["dataset_version"] != evidence.dataset_version or raw["revision"] != evidence.partial_completeness_revision or raw["generation_pin"] != evidence.generation_pin or (query_pin and raw.get("query_pin") != query_pin):
            raise ValueError("cursor identity mismatch")
        return _timestamp(raw["bar_time"], "cursor.bar_time"), str(raw["product_code"])
    except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise PartialPublicationQueryError("invalid partial publication cursor") from exc


def _next_cursor(row: Mapping[str, object], evidence: PartialPublicationEvidence, query_pin: str = "") -> str:
    payload = {"dataset_id": evidence.dataset_id, "dataset_version": evidence.dataset_version, "revision": evidence.partial_completeness_revision, "generation_pin": evidence.generation_pin, "query_pin": query_pin, "bar_time": str(row["bar_time"]), "product_code": str(row["product_code"])}
    return base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode()


def read_futures_1m_partial_page(evidence: PartialPublicationEvidence, codes: str, start_time: str, end_time: str, limit: int, cursor: str = "") -> tuple[list[dict[str, object]], str]:
    """Read only the immutable materialized publication; never read global facts."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100_000:
        raise ValueError("limit must be between 1 and 100000")
    products = normalize_product_codes(codes)
    start, end = _timestamp(start_time, "start_time"), _timestamp(end_time, "end_time")
    bound_query = _query_pin(codes, start_time, end_time)
    after = _cursor(cursor, evidence, bound_query)
    after_time = after[0].isoformat(sep=" ") if after else start.isoformat(sep=" ")
    after_product = after[1] if after else ""
    rows = _rows(_READ_CLIENT.query_batch("select product_code,exchange,bar_time::text as bar_time,open,high,low,close,volume,open_interest,adjustment_offset,source_key from readmodel.future_1m_partial_bar where dataset_id=%s and dataset_version=%s and product_code=any(%s::text[]) and bar_time >= %s::timestamp and bar_time <= %s::timestamp and (bar_time > %s::timestamp or (bar_time=%s::timestamp and product_code > %s)) order by bar_time,product_code limit %s", (evidence.dataset_id, evidence.dataset_version, list(products), start.isoformat(sep=" "), end.isoformat(sep=" "), after_time, after_time, after_product, limit), stage="futures_1m_partial_page"))
    seen: set[tuple[str, str]] = set()
    previous: tuple[str, str] | None = None
    for row in rows:
        key = (str(row["bar_time"]), str(row["product_code"]))
        if key in seen or (previous is not None and key <= previous):
            raise RuntimeError("partial publication materialization is not unique and ordered")
        seen.add(key); previous = key
        open_, high, low, close = (float(row[name]) for name in ("open", "high", "low", "close"))
        if high < max(open_, close) or low > min(open_, close) or high < low:
            raise RuntimeError("partial publication materialization has invalid OHLC")
        if row.get("volume") is None:
            raise RuntimeError("partial publication materialization has null volume")
        for name in ("volume", "open_interest"):
            if row.get(name) is not None and float(row[name]) < 0:
                raise RuntimeError(f"partial publication materialization has negative {name}")
        row["series_type"] = evidence.read_series_type
    return rows, (_next_cursor(rows[-1], evidence, bound_query) if len(rows) == limit else "")


def _coverage_cursor(value: str, evidence: PartialPublicationEvidence, query_pin: str) -> tuple[str, str, str] | None:
    if not value:
        return None
    try:
        raw = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
        if raw["dataset_id"] != evidence.dataset_id or raw["dataset_version"] != evidence.dataset_version or raw["revision"] != evidence.partial_completeness_revision or raw["generation_pin"] != evidence.generation_pin or raw["query_pin"] != query_pin:
            raise PartialPublicationQueryError("coverage cursor identity mismatch")
        return str(raw["product_code"]), str(raw["start_time"]), str(raw["end_time"])
    except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        if isinstance(exc, PartialPublicationQueryError):
            raise
        raise PartialPublicationQueryError("invalid partial coverage cursor") from exc


def _next_coverage_cursor(item: Mapping[str, object], evidence: PartialPublicationEvidence, query_pin: str) -> str:
    payload = {"dataset_id": evidence.dataset_id, "dataset_version": evidence.dataset_version, "revision": evidence.partial_completeness_revision, "generation_pin": evidence.generation_pin, "query_pin": query_pin, "product_code": item["product_code"], "start_time": item["start_time"], "end_time": item["end_time"]}
    return base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode()


def read_futures_1m_partial_coverage_page(evidence: PartialPublicationEvidence, codes: str, start_time: str, end_time: str, limit: int, cursor: str = "") -> tuple[list[dict[str, object]], str, dict[str, object]]:
    """DB-side interval paging. Synthetic undeclared coverage is generated in the CTE."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
        raise PartialPublicationQueryError("limit must be between 1 and 10000")
    query_pin = _query_pin(codes, start_time, end_time)
    after = _coverage_cursor(cursor, evidence, query_pin)
    products = list(normalize_product_codes(codes)); start = _timestamp(start_time, "start_time"); end = _timestamp(end_time, "end_time")
    after_product, after_start, after_end = after if after else ("", "", "")
    # The aggregate CTE is intentionally SQL-side: no request loads the full
    # interval set merely to calculate a page or its completeness metadata.
    statement = """
    with requested(product_code) as (select unnest(%s::text[])), declared as (
      select i.product_code,i.exchange,greatest(i.start_time,%s::timestamp) start_time,least(i.end_time,%s::timestamp) end_time,i.status,i.evidence_sha256,i.detail_json
      from readmodel.future_1m_partial_revision_interval i join requested r using(product_code)
      where i.dataset_id=%s and i.dataset_version=%s and i.partial_completeness_revision=%s and i.end_time >= %s::timestamp and i.start_time <= %s::timestamp
    ), ordered as (
      select *,lag(end_time) over(partition by product_code order by start_time,end_time) previous_end from declared
    ), synthetic as (
      select r.product_code,''::text exchange,%s::timestamp start_time,(coalesce(min(d.start_time),%s::timestamp+interval '1 minute')-interval '1 minute') end_time,'residual'::text status,%s::text evidence_sha256,jsonb_build_object('reason','undeclared_coverage') detail_json from requested r left join declared d using(product_code) group by r.product_code having coalesce(min(d.start_time),%s::timestamp+interval '1 minute')>%s::timestamp
      union all select product_code,''::text,previous_end+interval '1 minute',start_time-interval '1 minute','residual',%s::text,jsonb_build_object('reason','undeclared_coverage') from ordered where previous_end is not null and start_time>previous_end+interval '1 minute'
      union all select r.product_code,''::text,max(d.end_time)+interval '1 minute',%s::timestamp,'residual',%s::text,jsonb_build_object('reason','undeclared_coverage') from requested r join declared d using(product_code) group by r.product_code having max(d.end_time)<%s::timestamp
    ), coverage as (select * from declared union all select * from synthetic), product_summary as (select product_code,jsonb_build_object('accepted_count',count(*) filter(where status='accepted'),'excluded_count',count(*) filter(where status='excluded'),'residual_count',count(*) filter(where status='residual')) value from coverage group by product_code), summary as (select jsonb_object_agg(product_code,value) products from product_summary), counted as (
      select *,count(*) over() interval_count,count(*) filter(where status='excluded') over() excluded_count,count(*) filter(where status='residual') over() residual_count from coverage
    ) select product_code,exchange,start_time::text,end_time::text,status,evidence_sha256,detail_json,interval_count,excluded_count,residual_count,summary.products from counted cross join summary
    where (product_code,start_time::text,end_time::text)>(%s,%s,%s) order by product_code,start_time,end_time limit %s
    """
    synthetic_hash = _canonical_hash({"dataset_id": evidence.dataset_id, "revision": evidence.partial_completeness_revision, "query_pin": query_pin, "kind": "undeclared_coverage"})
    rows = _rows(_READ_CLIENT.query_batch(statement, (products, start.isoformat(sep=" "), end.isoformat(sep=" "), evidence.dataset_id, evidence.dataset_version, evidence.partial_completeness_revision, start.isoformat(sep=" "), end.isoformat(sep=" "), start.isoformat(sep=" "), end.isoformat(sep=" "), synthetic_hash, end.isoformat(sep=" "), start.isoformat(sep=" "), synthetic_hash, end.isoformat(sep=" "), synthetic_hash, end.isoformat(sep=" "), after_product, after_start, after_end, limit + 1), stage="futures_1m_partial_coverage_page"))
    page, extra = rows[:limit], rows[limit:]
    products_summary = dict(page[0].get("products", {})) if page else {}
    summary = {"products": products_summary, "interval_count": int(page[0]["interval_count"]) if page else 0, "excluded_count": int(page[0]["excluded_count"]) if page else 0, "residual_count": int(page[0]["residual_count"]) if page else 0, "coverage_contract_sha256": coverage_contract_sha256(evidence,codes,start_time,end_time), "coverage_contract_scope":"immutable full-query coverage; SQL aggregate may scan all query intervals; default live acceptance limit=10000"}
    return page, (_next_coverage_cursor(page[-1], evidence, query_pin) if page and extra else ""), summary
