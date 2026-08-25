from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from typing import Any

from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row

from quotemux.futures import normalize_product_codes
from quotemux import QuoteMuxPublicReader
from quotemux.infra.db.read_client import QueryBatch, ReadOnlyClient
from services.dataset_versions import current_dataset_version, dataset_version_from_state


DATASET_ID = "future_bar_1m"
_SERIES_TYPES = frozenset(("back_adjusted_continuous", "main_continuous"))
_PASSING_STATUSES = frozenset(("complete", "not_applicable", "known_no_bar"))
_BLOCKING_STATUSES = frozenset(("missing", "unknown"))
_ALL_STATUSES = _PASSING_STATUSES | _BLOCKING_STATUSES
_READ_CLIENT = ReadOnlyClient()
_SERIES_READER = QuoteMuxPublicReader()
_ONE_MINUTE = timedelta(minutes=1)

_DDL = """
create schema if not exists readmodel;
create table if not exists readmodel.future_1m_completeness_interval (
    dataset_version text not null,
    product_code text not null,
    exchange text not null,
    series_type text not null check (series_type in ('back_adjusted_continuous','main_continuous')),
    start_date date not null,
    end_date date not null,
    status text not null check (status in ('complete','not_applicable','known_no_bar','missing','unknown')),
    availability_ref text not null,
    session_rule_ref text not null,
    detail_json jsonb not null default '{}'::jsonb,
    manifest_sha256 text not null,
    published_at_utc timestamp with time zone not null default clock_timestamp(),
    primary key (dataset_version,product_code,exchange,series_type,start_date,end_date),
    check (start_date <= end_date)
);
create index if not exists future_1m_completeness_interval_lookup_idx
    on readmodel.future_1m_completeness_interval(dataset_version,series_type,product_code,start_date,end_date);
create table if not exists readmodel.future_1m_completeness_publication (
    dataset_version text primary key,
    back_adjusted_series_state jsonb not null,
    manifest_sha256 text not null,
    carried_from_dataset_version text,
    published_at_utc timestamp with time zone not null default clock_timestamp()
);
create table if not exists readmodel.future_1m_completeness_revision (
    dataset_version text not null,
    revision_sha256 text not null,
    back_adjusted_series_state jsonb not null,
    manifest_sha256 text not null,
    published_at_utc timestamp with time zone not null default clock_timestamp(),
    primary key (dataset_version,revision_sha256),
    unique (manifest_sha256)
);
create table if not exists readmodel.future_1m_completeness_revision_interval (
    dataset_version text not null,
    revision_sha256 text not null,
    product_code text not null,
    exchange text not null,
    series_type text not null check (series_type in ('back_adjusted_continuous','main_continuous')),
    start_time timestamp without time zone not null,
    end_time timestamp without time zone not null,
    status text not null check (status in ('complete','not_applicable','known_no_bar','missing','unknown')),
    availability_ref text not null,
    session_rule_ref text not null,
    evidence_sha256 text not null,
    detail_json jsonb not null default '{}'::jsonb,
    primary key (dataset_version,revision_sha256,product_code,exchange,series_type,start_time,end_time),
    foreign key (dataset_version,revision_sha256) references readmodel.future_1m_completeness_revision(dataset_version,revision_sha256),
    check (start_time <= end_time),
    check (evidence_sha256 ~ '^[0-9a-f]{64}$')
);
create index if not exists future_1m_completeness_revision_interval_lookup_idx
    on readmodel.future_1m_completeness_revision_interval(dataset_version,revision_sha256,series_type,product_code,start_time,end_time);
create table if not exists readmodel.future_1m_completeness_revision_activation (
    activation_id bigserial primary key,
    dataset_version text not null,
    revision_sha256 text not null,
    activated_at_utc timestamp with time zone not null default clock_timestamp(),
    foreign key (dataset_version,revision_sha256) references readmodel.future_1m_completeness_revision(dataset_version,revision_sha256)
);
create index if not exists future_1m_completeness_revision_activation_latest_idx
    on readmodel.future_1m_completeness_revision_activation(dataset_version,activation_id desc);
create or replace view readmodel.future_1m_completeness_active_revision as
select distinct on (dataset_version) dataset_version,revision_sha256,activation_id,activated_at_utc
from readmodel.future_1m_completeness_revision_activation
order by dataset_version,activation_id desc;
"""


@dataclass(frozen=True)
class Futures1mCompletenessEvidence:
    dataset_version: str
    completeness_revision: str = ""


def _date(value: str, field: str) -> date:
    try:
        return datetime.fromisoformat(value).date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def _incomplete(dataset_version: str, reason: str, items: list[dict[str, object]]) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "code": "DATA_INCOMPLETE",
            "message": "期货 1m 已发布完整性状态不允许返回部分数据",
            "details": {
                "dataset_id": DATASET_ID,
                "dataset_version": dataset_version,
                "reason": reason,
                "gap_sample": items[:100],
                "repair_endpoint": "/api/admin/data-repairs",
            },
        },
    )


def _rows(batch: QueryBatch) -> list[dict[str, object]]:
    return list(batch.as_dicts())


def _normalized_back_adjusted_series_state(value: Mapping[str, object]) -> dict[str, object]:
    if str(value.get("series_type", "back_adjusted_continuous")) != "back_adjusted_continuous":
        raise ValueError("back_adjusted_series_state must be back_adjusted_continuous")
    try:
        normalized = {
            "generation": int(value["generation"]), "row_count": int(value["row_count"]),
            "first_bar_time": str(value.get("first_bar_time") or ""), "last_bar_time": str(value.get("last_bar_time") or ""),
            "transaction_id": int(value["transaction_id"]), "operation": str(value["operation"]),
            "delta_fingerprint": str(value["delta_fingerprint"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("back_adjusted_series_state is incomplete") from exc
    if normalized["generation"] < 1 or normalized["row_count"] < 0 or not normalized["operation"] or not normalized["delta_fingerprint"]:
        raise ValueError("back_adjusted_series_state is invalid")
    return normalized


def can_carry_forward_back_adjusted_completeness(published_state: Mapping[str, object], current_state: Mapping[str, object]) -> bool:
    """A main-series write may carry forward only byte-identical BA lineage."""
    published = _normalized_back_adjusted_series_state(published_state)
    current = _normalized_back_adjusted_series_state(current_state)
    return json.dumps(published, sort_keys=True, separators=(",", ":")) == json.dumps(current, sort_keys=True, separators=(",", ":"))


def validate_published_futures_1m_completeness(
    codes: str,
    series_type: str,
    start_time: str,
    end_time: str,
    dataset_version: str = "",
    expected_completeness_revision: str = "",
) -> Futures1mCompletenessEvidence:
    """Read only the immutable completeness manifest before any page is fetched."""
    if series_type not in _SERIES_TYPES:
        raise ValueError(f"series_type must be one of: {', '.join(sorted(_SERIES_TYPES))}")
    products = normalize_product_codes(codes)
    if not products:
        raise ValueError("codes 不能为空")
    start_timestamp = _timestamp(start_time, "start_time")
    end_timestamp = _timestamp(end_time, "end_time")
    start = start_timestamp.date()
    end = end_timestamp.date()
    if start_timestamp > end_timestamp:
        raise ValueError("start_time must not be after end_time")

    current_version = current_dataset_version(DATASET_ID)
    if current_version == "" or (dataset_version and dataset_version != current_version):
        _incomplete(
            dataset_version or current_version,
            "dataset_version_mismatch",
            [{"requested_version": dataset_version, "current_version": current_version}],
        )
    version = current_version
    state = _rows(_READ_CLIENT.query_batch(
        "select coverage_ready,status,complete,error_message from readmodel.dataset_build_state "
        "where dataset_id=%s and dataset_version=%s",
        (DATASET_ID, version),
        stage="futures_1m_completeness_state",
    ))
    if len(state) != 1 or not bool(state[0].get("coverage_ready")) or str(state[0].get("status", "")) != "online":
        _incomplete(version, "unpublished", [{"state": state[0] if state else {}}])

    active_revisions = _rows(_READ_CLIENT.query_batch(
        "select revision_sha256 from readmodel.future_1m_completeness_active_revision where dataset_version=%s",
        (version,),
        stage="futures_1m_completeness_active_revision",
    ))
    if len(active_revisions) > 1:
        _incomplete(version, "active_revision_ambiguous", active_revisions)
    if active_revisions:
        revision = str(active_revisions[0].get("revision_sha256", ""))
        if not revision:
            _incomplete(version, "active_revision_invalid", active_revisions)
        if expected_completeness_revision and expected_completeness_revision != revision:
            _incomplete(version, "completeness_revision_mismatch", [{"requested_revision": expected_completeness_revision, "active_revision": revision}])
        _validate_revision_intervals(products, series_type, start_timestamp, end_timestamp, version, revision)
        return Futures1mCompletenessEvidence(version, revision)
    if expected_completeness_revision:
        _incomplete(version, "completeness_revision_unpublished", [{"requested_revision": expected_completeness_revision}])
    _validate_legacy_date_intervals(products, series_type, start, end, version)
    return Futures1mCompletenessEvidence(version)


def _validate_legacy_date_intervals(products: Sequence[str], series_type: str, start: date, end: date, version: str) -> None:
    intervals = _rows(_READ_CLIENT.query_batch(
        "select product_code,exchange,series_type,start_date,end_date,status,availability_ref,session_rule_ref,detail_json "
        "from readmodel.future_1m_completeness_interval "
        "where dataset_version=%s and series_type=%s and product_code=any(%s::text[]) "
        "and end_date >= %s::date and start_date <= %s::date "
        "order by product_code,start_date,end_date",
        (version, series_type, list(products), start.isoformat(), end.isoformat()),
        stage="futures_1m_completeness_intervals",
    ))
    failures: list[dict[str, object]] = []
    for product in products:
        cursor = start
        product_rows = [row for row in intervals if str(row["product_code"]) == product]
        for row in product_rows:
            interval_start = date.fromisoformat(str(row["start_date"]))
            interval_end = date.fromisoformat(str(row["end_date"]))
            if interval_end < cursor:
                continue
            if interval_start > cursor:
                failures.append({"product_code": product, "reason": "unknown", "start_date": cursor.isoformat(), "end_date": min(end, interval_start - timedelta(days=1)).isoformat()})
            status = str(row["status"])
            if status not in _PASSING_STATUSES:
                failures.append({
                    "product_code": product, "exchange": str(row["exchange"]), "series_type": series_type,
                    "reason": status, "start_date": max(start, interval_start).isoformat(), "end_date": min(end, interval_end).isoformat(),
                    "availability_ref": str(row["availability_ref"]), "session_rule_ref": str(row["session_rule_ref"]),
                    "detail": row.get("detail_json", {}),
                })
            cursor = max(cursor, interval_end + timedelta(days=1))
            if cursor > end:
                break
        if cursor <= end:
            failures.append({"product_code": product, "reason": "unknown", "start_date": cursor.isoformat(), "end_date": end.isoformat()})
    if failures:
        _incomplete(version, "missing_or_unknown_interval", failures)


def _validate_revision_intervals(
    products: Sequence[str], series_type: str, start: datetime, end: datetime, version: str, revision: str,
) -> None:
    intervals = _rows(_READ_CLIENT.query_batch(
        "select product_code,exchange,series_type,start_time,end_time,status,availability_ref,session_rule_ref,evidence_sha256,detail_json "
        "from readmodel.future_1m_completeness_revision_interval "
        "where dataset_version=%s and revision_sha256=%s and series_type=%s and product_code=any(%s::text[]) "
        "and end_time >= %s::timestamp and start_time <= %s::timestamp "
        "order by product_code,start_time,end_time",
        (version, revision, series_type, list(products), start.isoformat(sep=" "), end.isoformat(sep=" ")),
        stage="futures_1m_completeness_revision_intervals",
    ))
    failures: list[dict[str, object]] = []
    for product in products:
        cursor = start
        for row in (item for item in intervals if str(item["product_code"]) == product):
            interval_start = _timestamp(str(row["start_time"]), "start_time")
            interval_end = _timestamp(str(row["end_time"]), "end_time")
            if interval_end < cursor:
                continue
            if interval_start > cursor:
                failures.append({"product_code": product, "reason": "unknown", "start_time": cursor.isoformat(sep=" "), "end_time": min(end, interval_start - _ONE_MINUTE).isoformat(sep=" ")})
            status = str(row["status"])
            if status not in _PASSING_STATUSES:
                failures.append({
                    "product_code": product, "exchange": str(row["exchange"]), "series_type": series_type,
                    "reason": status, "start_time": max(start, interval_start).isoformat(sep=" "), "end_time": min(end, interval_end).isoformat(sep=" "),
                    "availability_ref": str(row["availability_ref"]), "session_rule_ref": str(row["session_rule_ref"]),
                    "evidence_sha256": str(row["evidence_sha256"]), "detail": row.get("detail_json", {}),
                })
            cursor = max(cursor, interval_end + _ONE_MINUTE)
            if cursor > end:
                break
        if cursor <= end:
            failures.append({"product_code": product, "reason": "unknown", "start_time": cursor.isoformat(sep=" "), "end_time": end.isoformat(sep=" ")})
    if failures:
        _incomplete(version, "missing_or_unknown_interval", failures)


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"], port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"], user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"], connect_timeout=10, row_factory=dict_row,
        application_name="markethub-futures-1m-completeness-publisher",
    )


def bootstrap_futures_1m_completeness_schema() -> None:
    """Write-only migration seam; public reads never call this."""
    with _connect() as connection:
        connection.execute(_DDL)


def _validated_entries(entries: object) -> list[dict[str, object]]:
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)) or not entries:
        raise ValueError("manifest entries must be a non-empty list")
    result: list[dict[str, object]] = []
    for raw in entries:
        if not isinstance(raw, Mapping):
            raise ValueError("manifest entry must be an object")
        product = str(raw.get("product_code", "")).strip()
        exchange = str(raw.get("exchange", "")).strip()
        series = str(raw.get("series_type", "")).strip()
        status = str(raw.get("status", "")).strip()
        availability_ref = str(raw.get("availability_ref", "")).strip()
        session_rule_ref = str(raw.get("session_rule_ref", "")).strip()
        start = _date(str(raw.get("start_date", "")), "start_date")
        end = _date(str(raw.get("end_date", "")), "end_date")
        if not product or not exchange or series not in _SERIES_TYPES or status not in _ALL_STATUSES:
            raise ValueError("manifest entry has invalid product, exchange, series_type, or status")
        if not availability_ref or not session_rule_ref or start > end:
            raise ValueError("manifest entry requires availability_ref, session_rule_ref, and ordered dates")
        detail = raw.get("detail", {})
        json.dumps(detail, sort_keys=True, separators=(",", ":"), default=str)
        result.append({
            "product_code": product, "exchange": exchange, "series_type": series,
            "start_date": start.isoformat(), "end_date": end.isoformat(), "status": status,
            "availability_ref": availability_ref, "session_rule_ref": session_rule_ref, "detail": detail,
        })
    result.sort(key=lambda item: (str(item["product_code"]), str(item["exchange"]), str(item["series_type"]), str(item["start_date"]), str(item["end_date"])))
    previous: dict[tuple[str, str, str], date] = {}
    for item in result:
        key = (str(item["product_code"]), str(item["exchange"]), str(item["series_type"]))
        start = date.fromisoformat(str(item["start_date"]))
        if key in previous and start <= previous[key]:
            raise ValueError(f"manifest intervals overlap: {key}")
        previous[key] = date.fromisoformat(str(item["end_date"]))
    return result


def publish_validated_futures_1m_completeness_manifest(manifest: Mapping[str, object]) -> dict[str, object]:
    """Atomically publish an already-validated source manifest for one immutable dataset version."""
    if str(manifest.get("dataset_id", "")) != DATASET_ID:
        raise ValueError(f"manifest dataset_id must be {DATASET_ID}")
    version = str(manifest.get("dataset_version", "")).strip()
    entries = _validated_entries(manifest.get("entries"))
    state_value = manifest.get("back_adjusted_series_state")
    if not isinstance(state_value, Mapping):
        raise ValueError("manifest requires back_adjusted_series_state")
    back_adjusted_state = _normalized_back_adjusted_series_state(state_value)
    canonical = {"dataset_id": DATASET_ID, "dataset_version": version, "entries": entries, "back_adjusted_series_state": back_adjusted_state}
    checksum = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    with _connect() as connection:
        state = connection.execute("select baseline_id,generation from audit.dataset_version_state where dataset_id=%s", (DATASET_ID,)).fetchone()
        if state is None:
            raise RuntimeError("future_bar_1m dataset version state unavailable")
        generation = int(state["generation"])
        expected = dataset_version_from_state(DATASET_ID, str(state["baseline_id"]), generation)
        if version == "" or version != expected:
            raise ValueError(f"manifest dataset_version does not match current immutable dataset: {version} != {expected}")
        existing = connection.execute(
            "select status,checksum_sha256 from readmodel.dataset_build_state where dataset_id=%s and dataset_version=%s",
            (DATASET_ID, version),
        ).fetchone()
        if existing is not None:
            if str(existing["status"]) == "online" and str(existing["checksum_sha256"] or "") == checksum:
                return {"dataset_id": DATASET_ID, "dataset_version": version, "status": "online", "checksum_sha256": checksum, "entries": len(entries), "idempotent": True}
            raise RuntimeError(f"immutable futures completeness state already exists: {version}")
        connection.execute(
            "insert into readmodel.dataset_build_state(dataset_id,dataset_version,status,source_generation,coverage_ready,complete,row_count,checksum_sha256,built_at_utc,updated_at_utc) "
            "values(%s,%s,'building',%s,false,false,0,%s,null,clock_timestamp())",
            (DATASET_ID, version, generation, checksum),
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                "insert into readmodel.future_1m_completeness_interval(dataset_version,product_code,exchange,series_type,start_date,end_date,status,availability_ref,session_rule_ref,detail_json,manifest_sha256) "
                "values(%s,%s,%s,%s,%s::date,%s::date,%s,%s,%s,%s::jsonb,%s)",
                [(version, item["product_code"], item["exchange"], item["series_type"], item["start_date"], item["end_date"], item["status"], item["availability_ref"], item["session_rule_ref"], json.dumps(item["detail"], sort_keys=True), checksum) for item in entries],
            )
        connection.execute(
            "insert into readmodel.future_1m_completeness_publication(dataset_version,back_adjusted_series_state,manifest_sha256) values(%s,%s::jsonb,%s)",
            (version, json.dumps(back_adjusted_state, sort_keys=True), checksum),
        )
        connection.execute(
            "update readmodel.dataset_build_state set status='online',coverage_ready=true,complete=true,row_count=%s,built_at_utc=clock_timestamp(),error_message='',updated_at_utc=clock_timestamp() "
            "where dataset_id=%s and dataset_version=%s",
            (len(entries), DATASET_ID, version),
        )
    return {"dataset_id": DATASET_ID, "dataset_version": version, "status": "online", "checksum_sha256": checksum, "entries": len(entries), "idempotent": False}


def _validated_revision_entries(entries: object) -> list[dict[str, object]]:
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)) or not entries:
        raise ValueError("revision entries must be a non-empty list")
    result: list[dict[str, object]] = []
    for raw in entries:
        if not isinstance(raw, Mapping):
            raise ValueError("revision entry must be an object")
        product = str(raw.get("product_code", "")).strip()
        exchange = str(raw.get("exchange", "")).strip()
        series = str(raw.get("series_type", "")).strip()
        status = str(raw.get("status", "")).strip()
        availability_ref = str(raw.get("availability_ref", "")).strip()
        session_rule_ref = str(raw.get("session_rule_ref", "")).strip()
        evidence_sha256 = str(raw.get("evidence_sha256", "")).strip().lower()
        start = _timestamp(str(raw.get("start_time", "")), "start_time")
        end = _timestamp(str(raw.get("end_time", "")), "end_time")
        if not product or not exchange or series not in _SERIES_TYPES or status not in _ALL_STATUSES:
            raise ValueError("revision entry has invalid product, exchange, series_type, or status")
        if not availability_ref or not session_rule_ref or start > end:
            raise ValueError("revision entry requires availability_ref, session_rule_ref, and ordered times")
        if len(evidence_sha256) != 64 or any(char not in "0123456789abcdef" for char in evidence_sha256):
            raise ValueError("revision entry requires a SHA-256 evidence hash")
        detail = raw.get("detail", {})
        json.dumps(detail, sort_keys=True, separators=(",", ":"), default=str)
        result.append({
            "product_code": product, "exchange": exchange, "series_type": series, "status": status,
            "start_time": start.isoformat(sep=" "), "end_time": end.isoformat(sep=" "),
            "availability_ref": availability_ref, "session_rule_ref": session_rule_ref,
            "evidence_sha256": evidence_sha256, "detail": detail,
        })
    result.sort(key=lambda item: (str(item["product_code"]), str(item["series_type"]), str(item["start_time"]), str(item["end_time"]), str(item["exchange"])))
    previous: dict[tuple[str, str], datetime] = {}
    for item in result:
        key = (str(item["product_code"]), str(item["series_type"]))
        start = _timestamp(str(item["start_time"]), "start_time")
        if key in previous and start <= previous[key]:
            raise ValueError(f"revision intervals overlap: {key}")
        previous[key] = _timestamp(str(item["end_time"]), "end_time")
    return result


def publish_validated_futures_1m_completeness_revision(manifest: Mapping[str, object]) -> dict[str, object]:
    """Append an immutable, timestamp-granular completeness revision; it is inactive until explicitly activated."""
    if str(manifest.get("dataset_id", "")) != DATASET_ID:
        raise ValueError(f"manifest dataset_id must be {DATASET_ID}")
    version = str(manifest.get("dataset_version", "")).strip()
    entries = _validated_revision_entries(manifest.get("entries"))
    state_value = manifest.get("back_adjusted_series_state")
    if not isinstance(state_value, Mapping):
        raise ValueError("revision manifest requires back_adjusted_series_state")
    back_adjusted_state = _normalized_back_adjusted_series_state(state_value)
    canonical = {"dataset_id": DATASET_ID, "dataset_version": version, "entries": entries, "back_adjusted_series_state": back_adjusted_state}
    checksum = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    requested_revision = str(manifest.get("revision_sha256", "")).strip().lower()
    if requested_revision and requested_revision != checksum:
        raise ValueError("revision_sha256 does not match immutable manifest")
    with _connect() as connection:
        state = connection.execute("select baseline_id,generation,status,coverage_ready from readmodel.dataset_build_state where dataset_id=%s and dataset_version=%s", (DATASET_ID, version)).fetchone()
        if state is None or str(state["status"]) != "online" or not bool(state["coverage_ready"]):
            raise RuntimeError("future_bar_1m immutable dataset is not online")
        current = current_dataset_version(DATASET_ID)
        if version == "" or version != current:
            raise ValueError(f"revision dataset_version does not match current immutable dataset: {version} != {current}")
        published = connection.execute("select back_adjusted_series_state from readmodel.future_1m_completeness_publication where dataset_version=%s", (version,)).fetchone()
        if published is None or not can_carry_forward_back_adjusted_completeness(published["back_adjusted_series_state"], back_adjusted_state):
            raise ValueError("revision back-adjusted lineage does not match immutable dataset publication")
        existing = connection.execute("select manifest_sha256 from readmodel.future_1m_completeness_revision where dataset_version=%s and revision_sha256=%s", (version, checksum)).fetchone()
        if existing is not None:
            return {"dataset_id": DATASET_ID, "dataset_version": version, "revision_sha256": checksum, "entries": len(entries), "idempotent": True}
        connection.execute(
            "insert into readmodel.future_1m_completeness_revision(dataset_version,revision_sha256,back_adjusted_series_state,manifest_sha256) values(%s,%s,%s::jsonb,%s)",
            (version, checksum, json.dumps(back_adjusted_state, sort_keys=True), checksum),
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                "insert into readmodel.future_1m_completeness_revision_interval(dataset_version,revision_sha256,product_code,exchange,series_type,start_time,end_time,status,availability_ref,session_rule_ref,evidence_sha256,detail_json) values(%s,%s,%s,%s,%s,%s::timestamp,%s::timestamp,%s,%s,%s,%s,%s::jsonb)",
                [(version, checksum, item["product_code"], item["exchange"], item["series_type"], item["start_time"], item["end_time"], item["status"], item["availability_ref"], item["session_rule_ref"], item["evidence_sha256"], json.dumps(item["detail"], sort_keys=True)) for item in entries],
            )
    return {"dataset_id": DATASET_ID, "dataset_version": version, "revision_sha256": checksum, "entries": len(entries), "idempotent": False}


def activate_futures_1m_completeness_revision(dataset_version: str, revision_sha256: str) -> dict[str, object]:
    """Append an activation event. Existing publications, revisions, and activations are never updated."""
    version = str(dataset_version).strip()
    revision = str(revision_sha256).strip().lower()
    if version != current_dataset_version(DATASET_ID):
        raise ValueError("activation dataset_version does not match current immutable dataset")
    with _connect() as connection:
        revision_row = connection.execute("select revision_sha256 from readmodel.future_1m_completeness_revision where dataset_version=%s and revision_sha256=%s", (version, revision)).fetchone()
        if revision_row is None:
            raise ValueError("unknown immutable completeness revision")
        active = connection.execute("select revision_sha256,activation_id from readmodel.future_1m_completeness_active_revision where dataset_version=%s", (version,)).fetchone()
        if active is not None and str(active["revision_sha256"]) == revision:
            return {"dataset_id": DATASET_ID, "dataset_version": version, "revision_sha256": revision, "activation_id": int(active["activation_id"]), "idempotent": True}
        activation = connection.execute("insert into readmodel.future_1m_completeness_revision_activation(dataset_version,revision_sha256) values(%s,%s) returning activation_id", (version, revision)).fetchone()
    return {"dataset_id": DATASET_ID, "dataset_version": version, "revision_sha256": revision, "activation_id": int(activation["activation_id"]), "idempotent": False}


def carry_forward_current_back_adjusted_completeness() -> dict[str, object]:
    """Formal main-series capture publication hook; never infers a session grid."""
    current_version = current_dataset_version(DATASET_ID)
    if current_version == "":
        raise RuntimeError("future_bar_1m current dataset version unavailable")
    series_rows = _SERIES_READER.list_futures_series_state_batch("back_adjusted_continuous").as_dicts()
    if len(series_rows) != 1:
        raise RuntimeError("back-adjusted series lineage state unavailable")
    current_state = _normalized_back_adjusted_series_state(series_rows[0])
    with _connect() as connection:
        existing = connection.execute(
            "select publication.dataset_version from readmodel.future_1m_completeness_publication publication join readmodel.dataset_build_state state on state.dataset_id=%s and state.dataset_version=publication.dataset_version where publication.dataset_version=%s and state.status='online' and state.coverage_ready",
            (DATASET_ID, current_version),
        ).fetchone()
        if existing is not None:
            return {"dataset_id": DATASET_ID, "dataset_version": current_version, "carried": False, "reason": "already_online"}
        previous = connection.execute(
            "select publication.dataset_version,publication.back_adjusted_series_state,publication.manifest_sha256 from readmodel.future_1m_completeness_publication publication join readmodel.dataset_build_state state on state.dataset_id=%s and state.dataset_version=publication.dataset_version where publication.dataset_version<>%s and state.status='online' and state.coverage_ready order by publication.published_at_utc desc limit 1",
            (DATASET_ID, current_version),
        ).fetchone()
        if previous is None or not can_carry_forward_back_adjusted_completeness(previous["back_adjusted_series_state"], current_state):
            raise RuntimeError("back-adjusted completeness cannot carry forward: immutable series lineage changed or is unpublished")
        prior_version = str(previous["dataset_version"])
        copied = connection.execute(
            "insert into readmodel.future_1m_completeness_interval(dataset_version,product_code,exchange,series_type,start_date,end_date,status,availability_ref,session_rule_ref,detail_json,manifest_sha256) select %s,product_code,exchange,series_type,start_date,end_date,status,availability_ref,session_rule_ref,detail_json,manifest_sha256 from readmodel.future_1m_completeness_interval where dataset_version=%s",
            (current_version, prior_version),
        ).rowcount
        connection.execute(
            "insert into readmodel.future_1m_completeness_publication(dataset_version,back_adjusted_series_state,manifest_sha256,carried_from_dataset_version) values(%s,%s::jsonb,%s,%s)",
            (current_version, json.dumps(current_state, sort_keys=True), str(previous["manifest_sha256"]), prior_version),
        )
        state = connection.execute("select baseline_id,generation from audit.dataset_version_state where dataset_id=%s", (DATASET_ID,)).fetchone()
        if state is None or dataset_version_from_state(DATASET_ID, str(state["baseline_id"]), int(state["generation"])) != current_version:
            raise RuntimeError("future_bar_1m dataset version changed during carry-forward")
        connection.execute(
            "insert into readmodel.dataset_build_state(dataset_id,dataset_version,status,source_generation,coverage_ready,complete,row_count,checksum_sha256,built_at_utc,updated_at_utc) values(%s,%s,'online',%s,true,true,%s,%s,clock_timestamp(),clock_timestamp())",
            (DATASET_ID, current_version, int(state["generation"]), copied, str(previous["manifest_sha256"])),
        )
    return {"dataset_id": DATASET_ID, "dataset_version": current_version, "carried": True, "carried_from_dataset_version": prior_version, "intervals": copied}
