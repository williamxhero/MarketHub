from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

from services.dataset_versions import dataset_version_from_state


DATASET_ID = "future_contract_reference"


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"], port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"], user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"], connect_timeout=10, row_factory=dict_row,
        application_name="markethub-future-contract-reference-readmodel",
    )


def finalize_future_contract_reference_state() -> dict[str, object]:
    """Mark only the atomically published QuoteMux catalog generation online."""
    with _connect() as connection:
        state = connection.execute(
            "select baseline_id,generation from audit.dataset_version_state where dataset_id=%s",
            (DATASET_ID,),
        ).fetchone()
        snapshot = connection.execute(
            "select snapshot.snapshot_id,snapshot.row_count,snapshot.product_count,snapshot.content_checksum "
            "from ref.future_contract_catalog_publication publication "
            "join ref.future_contract_catalog_snapshot snapshot on snapshot.snapshot_id=publication.snapshot_id "
            "where publication.scope_include_expired=false and snapshot.complete=true",
        ).fetchone()
        if state is None or snapshot is None:
            raise RuntimeError("future contract catalog publication unavailable")
        generation = int(state["generation"])
        dataset_version = dataset_version_from_state(DATASET_ID, str(state["baseline_id"]), generation)
        checksum = str(snapshot["content_checksum"] or "")
        if len(checksum) != 64:
            checksum = hashlib.sha256(json.dumps(dict(snapshot), sort_keys=True, default=str).encode()).hexdigest()
        connection.execute(
            "insert into readmodel.dataset_build_state(dataset_id,dataset_version,status,source_generation,coverage_ready,complete,row_count,checksum_sha256,built_at_utc,updated_at_utc) "
            "values(%s,%s,'online',%s,true,true,%s,%s,clock_timestamp(),clock_timestamp()) on conflict(dataset_id,dataset_version) do update set "
            "status='online',source_generation=excluded.source_generation,coverage_ready=true,complete=true,row_count=excluded.row_count,"
            "checksum_sha256=excluded.checksum_sha256,built_at_utc=clock_timestamp(),error_message='',updated_at_utc=clock_timestamp()",
            (DATASET_ID, dataset_version, generation, int(snapshot["row_count"]), checksum),
        )
    return {
        "dataset_id": DATASET_ID,
        "dataset_version": dataset_version,
        "generation": generation,
        "snapshot_id": str(snapshot["snapshot_id"]),
        "row_count": int(snapshot["row_count"]),
        "product_count": int(snapshot["product_count"]),
        "checksum_sha256": checksum,
        "complete": True,
    }
