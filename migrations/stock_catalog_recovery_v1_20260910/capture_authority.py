"""Capture a no-cache, no-local-write Tushare stock authority bundle.

The script is intended to run in the configured Tushare package venv.  It
calls the provider client directly instead of the QuoteMux cache/rate-limit
wrapper and writes its only output to stdout.  The controller decides where
that stdout is persisted.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import psycopg

sys.dont_write_bytecode = True

FORMAT_VERSION = "markethub-stock-authority-bundle-v1"
SCHEMA_TABLES = (
    "stock",
    "stock_authority_input",
    "stock_authority_input_item",
    "stock_reference_reconciliation",
    "stock_catalog_version",
    "stock_catalog_item",
    "stock_catalog_current",
    "stock_catalog_data_version",
    "stock_catalog_publication_attempt",
)
LATEST_COMPLETED_TRADING_DAY_QUERY = """
with local_time as (
    select now() at time zone 'Asia/Shanghai' as value
)
select max(calendar.trade_date)::text as trade_date
from ref.trade_calendar calendar, local_time
where calendar.exchange in ('SSE','SHSE','SZSE','BSE','BJSE')
  and calendar.is_open
  and calendar.trade_date <= case
      when local_time.value::time < time '15:30' then local_time.value::date - 1
      else local_time.value::date
  end
"""


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _load_env(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        connect_timeout=10,
        application_name="markethub-stock-authority-read-only-capture",
    )


def _database_baseline() -> dict[str, object]:
    with _database_connection() as connection:
        connection.execute("set transaction read only")
        database_name = str(connection.execute("select current_database()").fetchone()[0])
        schema_rows = connection.execute(
            "select table_schema,table_name,column_name,data_type,is_nullable,"
            "coalesce(column_default,'') from information_schema.columns "
            "where table_schema in ('ref','audit','readmodel') and table_name=any(%s) "
            "order by table_schema,table_name,ordinal_position",
            (list(SCHEMA_TABLES),),
        ).fetchall()
        constraint_rows = connection.execute(
            "select namespace.nspname,relation.relname,constraint_row.conname,"
            "constraint_row.contype,pg_get_constraintdef(constraint_row.oid,true) "
            "from pg_constraint constraint_row "
            "join pg_class relation on relation.oid=constraint_row.conrelid "
            "join pg_namespace namespace on namespace.oid=relation.relnamespace "
            "where namespace.nspname in ('ref','audit','readmodel') "
            "and relation.relname=any(%s) "
            "order by namespace.nspname,relation.relname,constraint_row.conname",
            (list(SCHEMA_TABLES),),
        ).fetchall()
        relation_rows = connection.execute(
            "select name,to_regclass(name)::text "
            "from unnest(%s::text[]) as relation_name(name) order by name",
            (
                [
                    "audit.stock_authority_input",
                    "audit.stock_authority_input_item",
                    "audit.stock_reference_reconciliation",
                    "audit.stock_catalog_publication_attempt",
                    "readmodel.stock_catalog_version",
                    "readmodel.stock_catalog_item",
                    "readmodel.stock_catalog_current",
                    "readmodel.stock_catalog_data_version",
                ],
            ),
        ).fetchall()
        stock_rows = connection.execute(
            "select market,code,coalesce(name,''),coalesce(industry,''),"
            "coalesce(listing_board,''),coalesce(listed_date::text,''),"
            "coalesce(delisted_date::text,''),coalesce(area,'') "
            "from ref.stock where code<>'000000' order by market,code"
        ).fetchall()
        trade_day_row = connection.execute(LATEST_COMPLETED_TRADING_DAY_QUERY).fetchone()
        connection.rollback()

    schema_payload = {
        "columns": [list(row) for row in schema_rows],
        "constraints": [list(row) for row in constraint_rows],
        "relations": [list(row) for row in relation_rows],
    }
    stock_payload = [list(row) for row in stock_rows]
    blank_codes = sorted(str(row[1]) for row in stock_rows if str(row[2]).strip() == "")
    return {
        "database_name": database_name,
        "latest_completed_trading_day": (
            "" if trade_day_row is None else str(trade_day_row[0] or "")
        ),
        "stock_schema_sha256": _canonical_sha256(schema_payload),
        "stock_schema": schema_payload,
        "legacy_stock_row_count": len(stock_payload),
        "legacy_stock_sha256": _canonical_sha256(stock_payload),
        "legacy_blank_name_codes": blank_codes,
        "legacy_stock_rows": stock_payload,
    }


def _health(url: str) -> dict[str, object]:
    with urlopen(url, timeout=10) as response:  # nosec B310 -- explicit operator URL
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("MarketHub health payload must be an object")
    return {
        "url": url,
        "status": str(payload.get("status", "")),
        "release": str(payload.get("version", payload.get("release", ""))),
        "data_version": str(payload.get("data_version", "")),
    }


def _source_instance() -> object:
    from quotemux.settings import QuoteMuxSettings

    instances = tuple(
        instance
        for instance in QuoteMuxSettings().get_contract_source_instances(
            "stocks.catalog", ("tushare",)
        )
        if instance.package_id == "tushare"
    )
    if len(instances) != 1:
        count = len(instances)
        raise RuntimeError(
            f"stocks.catalog requires exactly one Tushare authority instance; found={count}"
        )
    return instances[0]


def _exchange(ts_code: str) -> str:
    suffix = ts_code.rsplit(".", 1)[-1].upper()
    return {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix, "")


def _normalized_row(row: dict[str, object]) -> dict[str, str]:
    code = str(row.get("symbol", "")).strip().zfill(6)
    return {
        "code": code,
        "name": str(row.get("name", "") or "").strip(),
        "exchange": _exchange(str(row.get("ts_code", ""))),
        "market": str(row.get("market", "") or "").strip(),
        "list_status": str(row.get("list_status", "") or "").strip().upper(),
        "list_date": str(row.get("list_date", "") or "").strip(),
        "delist_date": str(row.get("delist_date", "") or "").strip(),
        "industry": str(row.get("industry", "") or "").strip(),
        "listing_board": str(row.get("market", "") or "").strip(),
        "area": str(row.get("area", "") or "").strip(),
    }


def _is_canonical_stock_identity(row: dict[str, object]) -> bool:
    symbol = str(row.get("symbol", "")).strip()
    ts_code = str(row.get("ts_code", "")).strip().upper()
    return re.fullmatch(r"[0-9]{6}", symbol, flags=re.ASCII) is not None and ts_code in {
        f"{symbol}.SH",
        f"{symbol}.SZ",
        f"{symbol}.BJ",
    }


def _partition_stock_basic_rows(
    raw_rows: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    accepted = [row for row in raw_rows if _is_canonical_stock_identity(row)]
    rejected = sorted(
        (
            {
                "reason": "noncanonical_stock_identifier",
                "symbol": str(row.get("symbol", "")).strip(),
                "ts_code": str(row.get("ts_code", "")).strip(),
            }
            for row in raw_rows
            if not _is_canonical_stock_identity(row)
        ),
        key=lambda row: (row["symbol"], row["ts_code"]),
    )
    return accepted, rejected


def _authority_shards(
    instance: object,
) -> tuple[dict[str, list[dict[str, str]]], dict[str, object]]:
    os.environ["QUOTEMUX_SOURCE_INSTANCE"] = json.dumps(instance.to_dict(), ensure_ascii=False)
    from quotemux_packages.tushare import source

    pro = source.get_ts_pro()
    if pro is None:
        raise RuntimeError("configured Tushare client is unavailable")
    fields = "ts_code,symbol,name,area,industry,market,list_date,delist_date,list_status"
    shards: dict[str, list[dict[str, str]]] = {}
    raw_receipt: dict[str, object] = {}
    for shard, status in (("listed", "L"), ("pending", "P"), ("delisted", "D")):
        # Deliberately bypass call_tushare_api and _load_stock_basic_frame:
        # neither cache data nor local rate-limit state may be written by capture.
        frame = pro.stock_basic(exchange="", list_status=status, fields=fields)
        if frame is None:
            raise RuntimeError(f"raw Tushare stock_basic returned None for {status}")
        raw_rows = frame.fillna("").astype(str).to_dict("records")
        accepted_rows, rejected_rows = _partition_stock_basic_rows(raw_rows)
        normalized = sorted(
            (_normalized_row(row) for row in accepted_rows),
            key=lambda row: (row["code"], row["exchange"]),
        )
        shards[shard] = normalized
        raw_receipt[shard] = {
            "list_status": status,
            "row_count": len(normalized),
            "provider_row_count": len(raw_rows),
            "accepted_row_count": len(normalized),
            "rejected_row_count": len(rejected_rows),
            "rejection_policy": "canonical_six_digit_symbol_and_matching_ts_code",
            "rejected_rows": rejected_rows,
            "rejected_rows_sha256": _canonical_sha256(rejected_rows),
            "columns": sorted(str(column) for column in frame.columns),
            "payload_sha256": _canonical_sha256(raw_rows),
            "normalized_sha256": _canonical_sha256(normalized),
        }
    return shards, raw_receipt


def capture(args: argparse.Namespace) -> dict[str, object]:
    _load_env(args.env_file)
    instance = _source_instance()
    database = _database_baseline()
    shards, raw_receipt = _authority_shards(instance)
    captured_at = datetime.now(UTC).isoformat()
    release_inputs = json.loads(args.release_inputs.read_text(encoding="utf-8"))
    if not isinstance(release_inputs, dict):
        raise RuntimeError("release-inputs.json must contain an object")

    from quotemux_packages.tushare import source

    source_path = Path(source.__file__).resolve()
    manifest_path = source_path.with_name("quotemux_package.json")
    provider_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload: dict[str, object] = {
        "format_version": FORMAT_VERSION,
        "captured_at_utc": captured_at,
        "fresh_through": database["latest_completed_trading_day"],
        "capture_mode": "direct_tushare_pro_stock_basic_no_cache_no_local_write",
        "health": _health(args.health_url),
        "release_inputs": release_inputs,
        "database": database,
        "authority_source": {
            "provider": "tushare",
            "instance_id": str(getattr(instance, "instance_id", "")),
            "source_package_version": str(provider_manifest.get("version", "")),
            "tushare_client_distribution_version": importlib.metadata.version("tushare"),
            "provider_source_sha256": _file_sha256(source_path),
            "provider_manifest_sha256": _file_sha256(manifest_path),
            "raw_receipt": raw_receipt,
        },
        "shards": shards,
    }
    payload["bundle_sha256"] = _canonical_sha256(payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--release-inputs", type=Path, required=True)
    parser.add_argument("--health-url", required=True)
    return parser


def main() -> int:
    print(json.dumps(capture(_parser().parse_args()), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
