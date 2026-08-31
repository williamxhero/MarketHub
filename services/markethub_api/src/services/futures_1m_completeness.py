from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
import re
from typing import Any

from fastapi import HTTPException
import psycopg
from psycopg import sql
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
create table if not exists readmodel.future_1m_completeness_rebuild (
    rebuild_id bigserial primary key,
    dataset_id text not null,
    dataset_version text not null,
    previous_dataset_version text not null,
    lineage_generation bigint not null,
    lineage_transaction_id bigint not null,
    back_adjusted_series_state jsonb not null,
    previous_back_adjusted_series_state jsonb not null,
    status text not null check (status in ('rebuild_pending','rebuild_running','published','failed_closed','superseded')),
    reason text not null,
    next_action text not null,
    attempt_count integer not null default 0,
    error_json jsonb not null default '{}'::jsonb,
    created_at_utc timestamp with time zone not null default clock_timestamp(),
    updated_at_utc timestamp with time zone not null default clock_timestamp(),
    unique (dataset_id,dataset_version,lineage_generation,lineage_transaction_id)
);
create index if not exists future_1m_completeness_rebuild_status_idx
    on readmodel.future_1m_completeness_rebuild(status,created_at_utc);
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
    revision_sha256 text,
    mode text not null check (mode in ('revision','legacy')),
    activated_at_utc timestamp with time zone not null default clock_timestamp(),
    foreign key (dataset_version,revision_sha256) references readmodel.future_1m_completeness_revision(dataset_version,revision_sha256),
    check ((mode = 'revision' and revision_sha256 is not null) or (mode = 'legacy' and revision_sha256 is null))
);
create index if not exists future_1m_completeness_revision_activation_latest_idx
    on readmodel.future_1m_completeness_revision_activation(dataset_version,activation_id desc);
create or replace view readmodel.future_1m_completeness_active_revision as
select distinct on (dataset_version) dataset_version,revision_sha256,mode,activation_id,activated_at_utc
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


def _enqueue_futures_1m_completeness_rebuild(
    connection: psycopg.Connection[Any],
    dataset_version: str,
    previous_dataset_version: str,
    previous_state: Mapping[str, object],
    current_state: Mapping[str, object],
) -> dict[str, object]:
    row = connection.execute(
        "insert into readmodel.future_1m_completeness_rebuild("
        "dataset_id,dataset_version,previous_dataset_version,lineage_generation,lineage_transaction_id,"
        "back_adjusted_series_state,previous_back_adjusted_series_state,status,reason,next_action) "
        "values(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,'rebuild_pending','back_adjusted_lineage_changed',"
        "'process_futures_1m_completeness_rebuild') "
        "on conflict(dataset_id,dataset_version,lineage_generation,lineage_transaction_id) do update "
        "set updated_at_utc=readmodel.future_1m_completeness_rebuild.updated_at_utc "
        "returning rebuild_id,status,created_at_utc,updated_at_utc",
        (
            DATASET_ID,
            dataset_version,
            previous_dataset_version,
            int(current_state["generation"]),
            int(current_state["transaction_id"]),
            json.dumps(dict(current_state), sort_keys=True),
            json.dumps(dict(previous_state), sort_keys=True),
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("futures 1m completeness rebuild could not be persisted")
    return dict(row)


def _is_conservative_insert_rebuild(
    previous_state: Mapping[str, object], current_state: Mapping[str, object],
) -> bool:
    previous = _normalized_back_adjusted_series_state(previous_state)
    current = _normalized_back_adjusted_series_state(current_state)
    return (
        current["operation"] == "insert"
        and int(current["generation"]) > int(previous["generation"])
        and int(current["transaction_id"]) > int(previous["transaction_id"])
        and int(current["row_count"]) >= int(previous["row_count"])
        and str(current["first_bar_time"]) <= str(previous["first_bar_time"])
        and str(current["last_bar_time"]) >= str(previous["last_bar_time"])
    )


def process_next_futures_1m_completeness_rebuild() -> dict[str, object]:
    """Publish one conservative insert-only rebuild in a single transaction.

    Existing interval assertions are retained without upgrading any missing or
    unknown interval.  This is safe only for a strictly monotonic insert
    lineage; every other mutation remains fail-closed for a full audit.
    """
    observed_rows = list(_SERIES_READER.list_futures_series_state_batch("back_adjusted_continuous").as_dicts())
    with _connect() as connection:
        rebuild = connection.execute(
            "select rebuild_id,dataset_version,previous_dataset_version,back_adjusted_series_state,"
            "previous_back_adjusted_series_state from readmodel.future_1m_completeness_rebuild "
            "where status='rebuild_pending' order by created_at_utc,rebuild_id for update skip locked limit 1"
        ).fetchone()
        if rebuild is None:
            return {"outcome": "idle", "reason": "no_pending_rebuild", "dataset_id": DATASET_ID}
        rebuild_id = int(rebuild["rebuild_id"])
        connection.execute(
            "update readmodel.future_1m_completeness_rebuild set status='rebuild_running',"
            "attempt_count=attempt_count+1,updated_at_utc=clock_timestamp() where rebuild_id=%s returning attempt_count",
            (rebuild_id,),
        ).fetchone()
        queued_state = _normalized_back_adjusted_series_state(rebuild["back_adjusted_series_state"])
        previous_state = _normalized_back_adjusted_series_state(rebuild["previous_back_adjusted_series_state"])
        current_version = current_dataset_version(DATASET_ID)
        observed_state = (_normalized_back_adjusted_series_state(observed_rows[0]) if len(observed_rows) == 1 else None)
        if (
            current_version != str(rebuild["dataset_version"])
            or observed_state is None
            or not can_carry_forward_back_adjusted_completeness(queued_state, observed_state)
        ):
            connection.execute(
                "update readmodel.future_1m_completeness_rebuild set status='superseded',"
                "reason='lineage_changed_during_rebuild',next_action='enqueue_current_lineage',"
                "updated_at_utc=clock_timestamp() where rebuild_id=%s",
                (rebuild_id,),
            )
            result: dict[str, object] = {
                "outcome": "superseded", "reason": "lineage_changed_during_rebuild",
                "rebuild_id": rebuild_id, "dataset_id": DATASET_ID,
                "dataset_version": str(rebuild["dataset_version"]),
                "observed_dataset_version": current_version,
                "observed_back_adjusted_series_state": observed_state or observed_rows,
                "next_action": {"action": "enqueue_current_lineage"},
            }
            if current_version == str(rebuild["dataset_version"]) and observed_state is not None:
                replacement = _enqueue_futures_1m_completeness_rebuild(
                    connection, current_version, str(rebuild["previous_dataset_version"]),
                    previous_state, observed_state,
                )
                result["replacement_rebuild_id"] = int(replacement["rebuild_id"])
                result["next_action"] = {"action": "process_futures_1m_completeness_rebuild"}
            return result
        if not _is_conservative_insert_rebuild(previous_state, queued_state):
            connection.execute(
                "update readmodel.future_1m_completeness_rebuild set status='failed_closed',"
                "reason='full_completeness_audit_required',next_action='run_full_futures_1m_completeness_audit',"
                "updated_at_utc=clock_timestamp() where rebuild_id=%s",
                (rebuild_id,),
            )
            return {
                "outcome": "failed_closed", "reason": "full_completeness_audit_required",
                "rebuild_id": rebuild_id, "dataset_id": DATASET_ID,
                "dataset_version": current_version,
                "current_back_adjusted_series_state": queued_state,
                "next_action": {"action": "run_full_futures_1m_completeness_audit"},
            }
        manifest_checksum = hashlib.sha256(json.dumps({
            "contract": "conservative-insert-rebuild-v1",
            "previous_dataset_version": str(rebuild["previous_dataset_version"]),
            "dataset_version": current_version,
            "back_adjusted_series_state": queued_state,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        copied = connection.execute(
            "insert into readmodel.future_1m_completeness_interval("
            "dataset_version,product_code,exchange,series_type,start_date,end_date,status,availability_ref,"
            "session_rule_ref,detail_json,manifest_sha256) "
            "select %s,product_code,exchange,series_type,start_date,end_date,status,availability_ref,session_rule_ref,"
            "detail_json || jsonb_build_object('rebuild_contract','conservative-insert-rebuild-v1'),%s "
            "from readmodel.future_1m_completeness_interval where dataset_version=%s on conflict do nothing",
            (current_version, manifest_checksum, str(rebuild["previous_dataset_version"])),
        ).rowcount
        connection.execute(
            "insert into readmodel.future_1m_completeness_publication("
            "dataset_version,back_adjusted_series_state,manifest_sha256,carried_from_dataset_version) "
            "values(%s,%s::jsonb,%s,%s) on conflict(dataset_version) do nothing",
            (current_version, json.dumps(queued_state, sort_keys=True), manifest_checksum, str(rebuild["previous_dataset_version"])),
        )
        state = connection.execute(
            "select baseline_id,generation from audit.dataset_version_state where dataset_id=%s", (DATASET_ID,),
        ).fetchone()
        if state is None or dataset_version_from_state(DATASET_ID, str(state["baseline_id"]), int(state["generation"])) != current_version:
            raise RuntimeError("future_bar_1m dataset version changed during completeness rebuild")
        connection.execute(
            "insert into readmodel.dataset_build_state("
            "dataset_id,dataset_version,status,source_generation,coverage_ready,complete,row_count,checksum_sha256,built_at_utc,updated_at_utc) "
            "values(%s,%s,'online',%s,true,true,%s,%s,clock_timestamp(),clock_timestamp()) "
            "on conflict(dataset_id,dataset_version) do update set status='online',coverage_ready=true,complete=true,"
            "row_count=excluded.row_count,checksum_sha256=excluded.checksum_sha256,updated_at_utc=clock_timestamp()",
            (DATASET_ID, current_version, int(state["generation"]), copied, manifest_checksum),
        )
        connection.execute(
            "update readmodel.future_1m_completeness_rebuild set status='published',reason='verified_conservative_insert_rebuild',"
            "next_action='none',updated_at_utc=clock_timestamp() where rebuild_id=%s returning updated_at_utc",
            (rebuild_id,),
        ).fetchone()
    return {
        "outcome": "published", "reason": "verified_conservative_insert_rebuild",
        "rebuild_id": rebuild_id, "dataset_id": DATASET_ID, "dataset_version": current_version,
        "intervals": copied, "manifest_sha256": manifest_checksum,
        "current_back_adjusted_series_state": queued_state, "next_action": {"action": "none"},
    }


def list_futures_1m_completeness_rebuilds(limit: int = 50) -> list[dict[str, object]]:
    bounded_limit = max(1, min(int(limit), 200))
    with _connect() as connection:
        rows = connection.execute(
            "select rebuild_id,dataset_id,dataset_version,previous_dataset_version,lineage_generation,"
            "lineage_transaction_id,status,reason,next_action,attempt_count,error_json,created_at_utc,updated_at_utc "
            "from readmodel.future_1m_completeness_rebuild order by rebuild_id desc limit %s",
            (bounded_limit,),
        ).fetchall()
    return [{
        **dict(row),
        "next_action": {"action": str(row["next_action"])},
    } for row in rows]


def retry_futures_1m_completeness_rebuild(rebuild_id: int) -> dict[str, object]:
    with _connect() as connection:
        row = connection.execute(
            "update readmodel.future_1m_completeness_rebuild set status='rebuild_pending',reason='operator_retry',"
            "next_action='process_futures_1m_completeness_rebuild',error_json='{}'::jsonb,"
            "updated_at_utc=clock_timestamp() where rebuild_id=%s and status in ('failed_closed','superseded') "
            "returning rebuild_id,dataset_id,dataset_version,previous_dataset_version,lineage_generation,"
            "lineage_transaction_id,status,reason,next_action,attempt_count,error_json,created_at_utc,updated_at_utc",
            (int(rebuild_id),),
        ).fetchone()
    if row is None:
        raise KeyError(f"futures 1m completeness rebuild is not recoverable: {rebuild_id}")
    return {**dict(row), "next_action": {"action": str(row["next_action"])}}


def get_futures_1m_completeness_rebuild_health() -> dict[str, object]:
    warning_seconds = max(1, int(os.getenv("MARKETHUB_FUTURES_1M_REBUILD_WARNING_SECONDS", "900")))
    critical_seconds = max(warning_seconds, int(os.getenv("MARKETHUB_FUTURES_1M_REBUILD_CRITICAL_SECONDS", "3600")))
    with _connect() as connection:
        row = connection.execute(
            "select count(*) filter(where status in ('rebuild_pending','rebuild_running')) as pending,"
            "count(*) filter(where status='failed_closed') as failed_closed,"
            "coalesce(extract(epoch from (clock_timestamp()-min(created_at_utc) "
            "filter(where status in ('rebuild_pending','rebuild_running')))),0)::bigint as oldest_pending_seconds "
            "from readmodel.future_1m_completeness_rebuild"
        ).fetchone() or {}
    pending = int(row.get("pending", 0) or 0)
    failed_closed = int(row.get("failed_closed", 0) or 0)
    oldest = int(row.get("oldest_pending_seconds", 0) or 0)
    status = "unhealthy" if failed_closed or oldest >= critical_seconds else "warning" if oldest >= warning_seconds else "healthy"
    return {
        "status": status, "pending": pending, "failed_closed": failed_closed,
        "oldest_pending_seconds": oldest, "warning_seconds": warning_seconds,
        "critical_seconds": critical_seconds,
    }


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
        "select revision_sha256,mode from readmodel.future_1m_completeness_active_revision where dataset_version=%s",
        (version,),
        stage="futures_1m_completeness_active_revision",
    ))
    if len(active_revisions) > 1:
        _incomplete(version, "active_revision_ambiguous", active_revisions)
    if active_revisions:
        if str(active_revisions[0].get("mode", "revision")) == "legacy":
            if expected_completeness_revision:
                _incomplete(version, "completeness_revision_unpublished", [{"requested_revision": expected_completeness_revision}])
            _validate_legacy_date_intervals(products, series_type, start, end, version)
            return Futures1mCompletenessEvidence(version)
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
        host=os.getenv("MARKETHUB_FUTURES_COMPLETENESS_MIGRATION_DB_HOST", os.environ["MARKETHUB_DB_HOST"]),
        port=int(os.getenv("MARKETHUB_FUTURES_COMPLETENESS_MIGRATION_DB_PORT", os.environ["MARKETHUB_DB_PORT"])),
        dbname=os.getenv("MARKETHUB_FUTURES_COMPLETENESS_MIGRATION_DB_NAME", os.environ["MARKETHUB_DB_NAME"]),
        user=os.getenv("MARKETHUB_FUTURES_COMPLETENESS_MIGRATION_DB_USER", os.environ["MARKETHUB_DB_USER"]),
        password=os.getenv("MARKETHUB_FUTURES_COMPLETENESS_MIGRATION_DB_PASSWORD", os.environ["MARKETHUB_DB_PASSWORD"]),
        connect_timeout=10, row_factory=dict_row,
        application_name="markethub-futures-1m-completeness-publisher",
    )


def _application_read_role() -> str:
    role = os.environ["MARKETHUB_DB_USER"].strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role) is None:
        raise RuntimeError("MARKETHUB_DB_USER must be a PostgreSQL identifier")
    return role


def bootstrap_futures_1m_completeness_schema() -> None:
    """Write-only migration seam; public reads never call this."""
    with _connect() as connection:
        connection.execute(_DDL)
        read_role = sql.Identifier(_application_read_role())
        connection.execute(
            sql.SQL("revoke all privileges on table readmodel.future_1m_completeness_active_revision from {}")
            .format(read_role)
        )
        connection.execute(
            sql.SQL("grant select on table readmodel.future_1m_completeness_revision, "
                    "readmodel.future_1m_completeness_revision_interval, "
                    "readmodel.future_1m_completeness_revision_activation, "
                    "readmodel.future_1m_completeness_active_revision to {}")
            .format(read_role)
        )
        connection.execute(
            sql.SQL("revoke insert, update, delete on table readmodel.future_1m_completeness_revision, "
                    "readmodel.future_1m_completeness_revision_interval, "
                    "readmodel.future_1m_completeness_revision_activation from {}")
            .format(read_role)
        )
        connection.execute(
            sql.SQL("revoke truncate, references, trigger on table readmodel.future_1m_completeness_revision, "
                    "readmodel.future_1m_completeness_revision_interval, "
                    "readmodel.future_1m_completeness_revision_activation from {}")
            .format(read_role)
        )
        connection.execute(
            sql.SQL("revoke usage, select, update on sequence "
                    "readmodel.future_1m_completeness_revision_activation_activation_id_seq from {}")
            .format(read_role)
        )
        for relation, privilege in (
            *( (relation, privilege)
               for relation in (
                   "readmodel.future_1m_completeness_revision",
                   "readmodel.future_1m_completeness_revision_interval",
                   "readmodel.future_1m_completeness_revision_activation",
               )
               for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER") ),
            *( ("readmodel.future_1m_completeness_revision_activation_activation_id_seq", privilege)
               for privilege in ("USAGE", "SELECT", "UPDATE") ),
            *( ("readmodel.future_1m_completeness_active_revision", privilege)
               for privilege in ("INSERT", "UPDATE", "DELETE", "REFERENCES", "TRIGGER") ),
        ):
            check = "has_sequence_privilege" if relation.endswith("_seq") else "has_table_privilege"
            row = connection.execute(f"select {check}(%s, %s, %s) as allowed", (_application_read_role(), relation, privilege)).fetchone()
            if row is None or bool(row["allowed"]):
                raise RuntimeError(f"immutable futures completeness application role retains {privilege} on {relation}")


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
        state = connection.execute("select status,coverage_ready from readmodel.dataset_build_state where dataset_id=%s and dataset_version=%s", (DATASET_ID, version)).fetchone()
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
        active = connection.execute("select revision_sha256,mode,activation_id from readmodel.future_1m_completeness_active_revision where dataset_version=%s", (version,)).fetchone()
        if active is not None and str(active["mode"]) == "revision" and str(active["revision_sha256"]) == revision:
            return {"dataset_id": DATASET_ID, "dataset_version": version, "revision_sha256": revision, "activation_id": int(active["activation_id"]), "idempotent": True}
        activation = connection.execute("insert into readmodel.future_1m_completeness_revision_activation(dataset_version,revision_sha256,mode) values(%s,%s,'revision') returning activation_id", (version, revision)).fetchone()
    return {"dataset_id": DATASET_ID, "dataset_version": version, "revision_sha256": revision, "activation_id": int(activation["activation_id"]), "idempotent": False}


def restore_legacy_futures_1m_completeness(dataset_version: str) -> dict[str, object]:
    """Append a legacy-pointer activation without changing any immutable publication or revision."""
    version = str(dataset_version).strip()
    if version != current_dataset_version(DATASET_ID):
        raise ValueError("legacy restore dataset_version does not match current immutable dataset")
    with _connect() as connection:
        active = connection.execute("select mode,activation_id from readmodel.future_1m_completeness_active_revision where dataset_version=%s", (version,)).fetchone()
        if active is not None and str(active["mode"]) == "legacy":
            return {"dataset_id": DATASET_ID, "dataset_version": version, "mode": "legacy", "activation_id": int(active["activation_id"]), "idempotent": True}
        activation = connection.execute("insert into readmodel.future_1m_completeness_revision_activation(dataset_version,revision_sha256,mode) values(%s,null,'legacy') returning activation_id", (version,)).fetchone()
    return {"dataset_id": DATASET_ID, "dataset_version": version, "mode": "legacy", "activation_id": int(activation["activation_id"]), "idempotent": False}


def carry_forward_current_back_adjusted_completeness() -> dict[str, object]:
    """Formal main-series capture publication hook; never infers a session grid.

    A capture may advance the immutable dataset without changing the
    back-adjusted lineage.  Only that exact case is safe to carry forward.
    Valid lineage changes require a new completeness build; unavailable or
    unpublished lineage remains fail-closed without invalidating the capture.
    """
    current_version = current_dataset_version(DATASET_ID)
    if current_version == "":
        return {
            "outcome": "failed_closed",
            "reason": "future_1m_dataset_version_unavailable",
            "dataset_id": DATASET_ID,
            "dataset_version": "",
            "next_action": {"action": "retry_after_dataset_version_is_published"},
        }
    series_rows = list(_SERIES_READER.list_futures_series_state_batch("back_adjusted_continuous").as_dicts())
    if len(series_rows) != 1:
        return {
            "outcome": "failed_closed",
            "reason": "back_adjusted_lineage_unavailable",
            "dataset_id": DATASET_ID,
            "dataset_version": current_version,
            "observed_back_adjusted_series_states": series_rows,
            "next_action": {"action": "repair_or_publish_back_adjusted_lineage"},
        }
    try:
        current_state = _normalized_back_adjusted_series_state(series_rows[0])
    except ValueError as exc:
        return {
            "outcome": "failed_closed",
            "reason": "back_adjusted_lineage_invalid",
            "dataset_id": DATASET_ID,
            "dataset_version": current_version,
            "observed_back_adjusted_series_state": series_rows[0],
            "error": str(exc),
            "next_action": {"action": "repair_or_publish_back_adjusted_lineage"},
        }
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
        if previous is None:
            return {
                "outcome": "failed_closed",
                "reason": "back_adjusted_lineage_unpublished",
                "dataset_id": DATASET_ID,
                "dataset_version": current_version,
                "current_back_adjusted_series_state": current_state,
                "next_action": {"action": "publish_verified_futures_1m_completeness"},
            }
        prior_version = str(previous["dataset_version"])
        previous_state = _normalized_back_adjusted_series_state(previous["back_adjusted_series_state"])
        if not can_carry_forward_back_adjusted_completeness(previous_state, current_state):
            rebuild = _enqueue_futures_1m_completeness_rebuild(
                connection, current_version, prior_version, previous_state, current_state,
            )
            return {
                "outcome": str(rebuild["status"]),
                "reason": "back_adjusted_lineage_changed",
                "rebuild_id": int(rebuild["rebuild_id"]),
                "dataset_id": DATASET_ID,
                "dataset_version": current_version,
                "previous_dataset_version": prior_version,
                "previous_back_adjusted_series_state": previous_state,
                "current_back_adjusted_series_state": current_state,
                "next_action": {"action": "process_futures_1m_completeness_rebuild"},
            }
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
    return {
        "dataset_id": DATASET_ID,
        "dataset_version": current_version,
        "carried": True,
        "carried_from_dataset_version": prior_version,
        "intervals": copied,
        "current_back_adjusted_series_state": current_state,
    }
