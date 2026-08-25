from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from typing import Any

from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row

from quotemux.futures import normalize_product_codes
from quotemux.infra.db.read_client import QueryBatch, ReadOnlyClient
from services.dataset_versions import current_dataset_version, dataset_version_from_state


DATASET_ID = "future_bar_1m"
_SERIES_TYPES = frozenset(("back_adjusted_continuous", "main_continuous"))
_PASSING_STATUSES = frozenset(("complete", "not_applicable", "known_no_bar"))
_BLOCKING_STATUSES = frozenset(("missing", "unknown"))
_ALL_STATUSES = _PASSING_STATUSES | _BLOCKING_STATUSES
_READ_CLIENT = ReadOnlyClient()

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
"""


def _date(value: str, field: str) -> date:
    try:
        return datetime.fromisoformat(value).date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc


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


def validate_published_futures_1m_completeness(
    codes: str,
    series_type: str,
    start_time: str,
    end_time: str,
    dataset_version: str = "",
) -> str:
    """Read only the immutable completeness manifest before any page is fetched."""
    if series_type not in _SERIES_TYPES:
        raise ValueError(f"series_type must be one of: {', '.join(sorted(_SERIES_TYPES))}")
    products = normalize_product_codes(codes)
    if not products:
        raise ValueError("codes 不能为空")
    start = _date(start_time, "start_time")
    end = _date(end_time, "end_time")
    if start > end:
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
    return version


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
    canonical = {"dataset_id": DATASET_ID, "dataset_version": version, "entries": entries}
    checksum = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    with _connect() as connection:
        connection.execute(_DDL)
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
        connection.executemany(
            "insert into readmodel.future_1m_completeness_interval(dataset_version,product_code,exchange,series_type,start_date,end_date,status,availability_ref,session_rule_ref,detail_json,manifest_sha256) "
            "values(%s,%s,%s,%s,%s::date,%s::date,%s,%s,%s,%s::jsonb,%s)",
            [(version, item["product_code"], item["exchange"], item["series_type"], item["start_date"], item["end_date"], item["status"], item["availability_ref"], item["session_rule_ref"], json.dumps(item["detail"], sort_keys=True), checksum) for item in entries],
        )
        connection.execute(
            "update readmodel.dataset_build_state set status='online',coverage_ready=true,complete=true,row_count=%s,built_at_utc=clock_timestamp(),error_message='',updated_at_utc=clock_timestamp() "
            "where dataset_id=%s and dataset_version=%s",
            (len(entries), DATASET_ID, version),
        )
    return {"dataset_id": DATASET_ID, "dataset_version": version, "status": "online", "checksum_sha256": checksum, "entries": len(entries), "idempotent": False}
