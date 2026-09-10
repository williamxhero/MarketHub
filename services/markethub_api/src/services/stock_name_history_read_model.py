from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

from fastapi import HTTPException
from quotemux.infra.db.read_client import ReadOnlyClient

from services.market_data_version import market_data_version_for_stock_catalog_state

_DATA_VERSION = re.compile(r"^mhf-v1-[0-9a-f]{64}$")

_MAPPING_QUERY = """
select mapping.data_version,mapping.catalog_version,mapping.status,
       mapping.serve_until_utc >= clock_timestamp() as retained,mapping.reason
from readmodel.stock_catalog_data_version mapping
where mapping.data_version=%s
"""

_CURRENT_BINDING_QUERY = """
select state.baseline_id,state.generation,current.catalog_version
from audit.market_data_version_state state
join readmodel.stock_catalog_current current on current.singleton=true
join readmodel.stock_catalog_version catalog
  on catalog.catalog_version=current.catalog_version and catalog.status='healthy'
join readmodel.stock_name_history_version history
  on history.catalog_version=current.catalog_version and history.status='healthy'
where state.singleton=true
"""

_SNAPSHOT_QUERY = """
select history.row_count,history.distinct_code_count,history.content_sha256,
       history.excluded_row_count,history.excluded_content_sha256
from readmodel.stock_name_history_version history
join readmodel.stock_catalog_version catalog using(catalog_version)
where history.catalog_version=%s and history.status='healthy' and catalog.status='healthy'
"""

_RETAIN_SECONDS = 24 * 60 * 60
_BINDING_CACHE: dict[str, tuple[str, float]] = {}
_BINDING_LOCK = threading.Lock()

_PAGE_QUERY = """
select history.code,history.name,history.start_date::text as start_date,
       coalesce(history.end_date::text,'') as end_date,
       coalesce(history.ann_date::text,'') as ann_date
from readmodel.stock_name_history_item history
where history.catalog_version=%s
order by history.code asc,history.start_date asc,history.end_date asc nulls last,
         history.name asc,history.market asc
limit %s offset %s
"""


def _fail(code: str, requested: str, *, reason: str = "") -> HTTPException:
    details: dict[str, str] = {"requested_version": requested}
    if reason:
        details["reason"] = reason
    return HTTPException(
        status_code=409,
        detail={
            "code": code,
            "message": "请求的股票名称历史版本不可服务。请重新读取 /api/health",
            "details": details,
        },
    )


def _resolve_catalog_version(snapshot: Any, requested: str) -> str:
    mapping_rows = snapshot.query_batch(_MAPPING_QUERY, (requested,)).as_dicts()
    if mapping_rows:
        mapping = mapping_rows[0]
        status = str(mapping.get("status", ""))
        if status == "quarantined":
            raise _fail(
                "CATALOG_DATA_VERSION_QUARANTINED",
                requested,
                reason=str(mapping.get("reason", "") or ""),
            )
        catalog_version = str(mapping.get("catalog_version", "") or "")
        if status == "healthy" and bool(mapping.get("retained")) and catalog_version:
            return catalog_version
        raise _fail("CATALOG_DATA_VERSION_STALE", requested)

    now = time.monotonic()
    with _BINDING_LOCK:
        cached = _BINDING_CACHE.get(requested)
        if cached is not None and cached[1] >= now:
            return cached[0]
        _BINDING_CACHE.pop(requested, None)
    current_rows = snapshot.query_batch(_CURRENT_BINDING_QUERY).as_dicts()
    if len(current_rows) == 1:
        current = current_rows[0]
        catalog_version = str(current.get("catalog_version", "") or "")
        current_version = market_data_version_for_stock_catalog_state(
            str(current.get("baseline_id", "") or ""),
            int(current.get("generation", 0) or 0),
            catalog_version,
            os.getenv("QUOTEMUX_ADJUSTMENT_BASE_DATE", "").strip(),
        )
        if current_version == requested:
            with _BINDING_LOCK:
                _BINDING_CACHE[requested] = (catalog_version, now + _RETAIN_SECONDS)
            return catalog_version
    raise _fail("CATALOG_DATA_VERSION_STALE", requested)


def read_stock_name_history_page(
    data_version: str,
    *,
    limit: int,
    offset: int,
    reader: Any | None = None,
) -> dict[str, object]:
    """Read one immutable name-history page through the least-privilege reader."""
    requested = data_version.strip()
    if not _DATA_VERSION.fullmatch(requested):
        raise _fail("CATALOG_DATA_VERSION_STALE", requested)
    actual_reader = ReadOnlyClient() if reader is None else reader
    with actual_reader.snapshot() as snapshot:
        catalog_version = _resolve_catalog_version(snapshot, requested)
        metadata_rows = snapshot.query_batch(_SNAPSHOT_QUERY, (catalog_version,)).as_dicts()
        if len(metadata_rows) != 1:
            raise _fail("STOCK_NAME_HISTORY_SNAPSHOT_UNAVAILABLE", requested)
        metadata = metadata_rows[0]
        rows = snapshot.query_batch(
            _PAGE_QUERY, (catalog_version, limit, offset), stage="stock_name_history_page"
        ).as_dicts()
    items = [
        {
            "code": str(row["code"]),
            "name": str(row["name"]),
            "start_date": str(row["start_date"]),
            "end_date": str(row.get("end_date", "") or ""),
            "ann_date": str(row.get("ann_date", "") or ""),
        }
        for row in rows
    ]
    return {
        "items": items,
        "total": int(metadata["row_count"]),
        "distinct_code": int(metadata["distinct_code_count"]),
        "limit": limit,
        "offset": offset,
        "data_version": requested,
        "catalog_version": catalog_version,
        "content_sha256": str(metadata["content_sha256"]),
        "excluded_source_rows": int(metadata.get("excluded_row_count", 0) or 0),
        "excluded_source_sha256": str(metadata.get("excluded_content_sha256", "") or ""),
    }
