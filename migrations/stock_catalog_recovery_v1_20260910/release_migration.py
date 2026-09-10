"""Fail-closed expand/reconcile/publish entrypoint for stock catalog recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.request import urlopen

import psycopg
from psycopg.rows import dict_row

FORMAT_VERSION = "markethub-stock-authority-bundle-v1"
APPLY_TOKEN = "SPEC-3-CONTROLLER-APPLY"
NO_GO_EXIT = 20
REQUIRED_QUOTE_MUX_COMMIT = "b57653327ad4a7e48a323eb21534663a3686b7bf"
REQUIRED_QUOTE_MUX_PACKAGES_COMMIT = "7af2bc7c0c78788f0358035725590fce9f4c7f77"
LEGACY_KNOWN_DIRTY_DATA_VERSION = (
    "mhf-v1-02f1aa9d6e2eb0553d88c53e0d0023a070a786e94896a66fb4696e407a00065f"
)
DATA_VERSION = re.compile(r"^mhf-v1-[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REHEARSAL_DATABASE = re.compile(r"(?:spec3|rehears|test)", re.IGNORECASE)
CAPTURE_MODE = "direct_tushare_pro_stock_basic_no_cache_no_local_write"
AUTHORITY_COLUMNS = sorted(
    (
        "area",
        "delist_date",
        "industry",
        "list_date",
        "list_status",
        "market",
        "name",
        "symbol",
        "ts_code",
    )
)
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


class NoGo(RuntimeError):
    def __init__(self, reason: str, **details: object) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _load_bundle(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise NoGo("authority bundle must contain an object")
    actual_hash = str(payload.pop("bundle_sha256", ""))
    expected_hash = _canonical_sha256(payload)
    payload["bundle_sha256"] = actual_hash
    if actual_hash != expected_hash:
        raise NoGo(
            "authority bundle hash mismatch",
            expected_bundle_sha256=expected_hash,
            actual_bundle_sha256=actual_hash,
        )
    if payload.get("format_version") != FORMAT_VERSION:
        raise NoGo("unsupported authority bundle format")
    return payload


def _shard_counts(bundle: dict[str, Any]) -> dict[str, int]:
    shards = bundle.get("shards")
    if not isinstance(shards, dict):
        raise NoGo("authority bundle omits shards")
    counts: dict[str, int] = {}
    for shard in ("listed", "pending", "delisted"):
        rows = shards.get(shard)
        if not isinstance(rows, list):
            raise NoGo(f"authority shard is not a list: {shard}")
        counts[shard] = len(rows)
    return counts


def _authority_shards(bundle: dict[str, Any]) -> dict[str, list[SimpleNamespace]]:
    shards_payload = bundle["shards"]
    invalid = [
        shard
        for shard in ("listed", "pending", "delisted")
        if any(not isinstance(row, dict) for row in shards_payload[shard])
    ]
    if invalid:
        raise NoGo("authority shard contains a non-object row", shards=invalid)
    return {
        shard: [SimpleNamespace(**row) for row in shards_payload[shard]]
        for shard in ("listed", "pending", "delisted")
    }


def _is_canonical_provider_identity(symbol: object, ts_code: object) -> bool:
    normalized_symbol = str(symbol).strip()
    normalized_ts_code = str(ts_code).strip().upper()
    return re.fullmatch(r"[0-9]{6}", normalized_symbol, flags=re.ASCII) is not None and (
        normalized_ts_code
        in {
            f"{normalized_symbol}.SH",
            f"{normalized_symbol}.SZ",
            f"{normalized_symbol}.BJ",
        }
    )


def _validate_raw_receipts(bundle: dict[str, Any], counts: dict[str, int]) -> None:
    if bundle.get("capture_mode") != CAPTURE_MODE:
        raise NoGo("authority bundle capture mode is invalid")
    source = bundle.get("authority_source")
    if not isinstance(source, dict) or source.get("provider") != "tushare":
        raise NoGo("authority source is invalid")
    receipts = source.get("raw_receipt")
    if not isinstance(receipts, dict):
        raise NoGo("authority raw receipt is missing")

    expected_status = {"listed": "L", "pending": "P", "delisted": "D"}
    empty_hash = _canonical_sha256([])
    for shard, status in expected_status.items():
        receipt = receipts.get(shard)
        if not isinstance(receipt, dict):
            raise NoGo(f"authority raw receipt is missing or invalid: {shard}")
        row_count = receipt.get("row_count")
        provider_row_count = receipt.get("provider_row_count")
        accepted_row_count = receipt.get("accepted_row_count")
        rejected_row_count = receipt.get("rejected_row_count")
        rejected_rows = receipt.get("rejected_rows")
        columns = receipt.get("columns")
        payload_hash = receipt.get("payload_sha256")
        normalized_hash = receipt.get("normalized_sha256")
        if receipt.get("list_status") != status:
            raise NoGo(f"authority raw receipt status mismatch: {shard}")
        if isinstance(row_count, bool) or row_count != counts[shard]:
            raise NoGo(f"authority raw receipt row count mismatch: {shard}")
        integer_counts = (provider_row_count, accepted_row_count, rejected_row_count)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_counts):
            raise NoGo(f"authority raw receipt accounting is invalid: {shard}")
        if (
            accepted_row_count != counts[shard]
            or provider_row_count != accepted_row_count + rejected_row_count
        ):
            raise NoGo(f"authority raw receipt accounting mismatch: {shard}")
        if receipt.get("rejection_policy") != (
            "canonical_six_digit_symbol_and_matching_ts_code"
        ):
            raise NoGo(f"authority raw receipt rejection policy mismatch: {shard}")
        if not isinstance(rejected_rows, list) or len(rejected_rows) != rejected_row_count:
            raise NoGo(f"authority raw receipt rejected rows mismatch: {shard}")
        if any(
            not isinstance(row, dict)
            or set(row) != {"reason", "symbol", "ts_code"}
            or row.get("reason") != "noncanonical_stock_identifier"
            or not isinstance(row.get("symbol"), str)
            or not isinstance(row.get("ts_code"), str)
            or _is_canonical_provider_identity(row.get("symbol"), row.get("ts_code"))
            for row in rejected_rows
        ):
            raise NoGo(f"authority raw receipt rejected row is invalid: {shard}")
        if rejected_rows != sorted(
            rejected_rows,
            key=lambda row: (str(row["symbol"]), str(row["ts_code"])),
        ):
            raise NoGo(f"authority raw receipt rejected rows are not sorted: {shard}")
        if receipt.get("rejected_rows_sha256") != _canonical_sha256(rejected_rows):
            raise NoGo(f"authority raw receipt rejected rows hash mismatch: {shard}")
        if columns != AUTHORITY_COLUMNS:
            raise NoGo(f"authority raw receipt columns mismatch: {shard}")
        if not isinstance(payload_hash, str) or SHA256.fullmatch(payload_hash) is None:
            raise NoGo(f"authority raw receipt payload hash is invalid: {shard}")
        if normalized_hash != _canonical_sha256(bundle["shards"][shard]):
            raise NoGo(f"authority raw receipt normalized hash mismatch: {shard}")
        if provider_row_count == 0 and payload_hash != empty_hash:
            raise NoGo(f"authority empty raw receipt payload hash mismatch: {shard}")


def _prepare_authority(bundle: dict[str, Any]) -> object:
    counts = _shard_counts(bundle)
    if counts["listed"] == 0:
        raise NoGo(
            "stock authority listed shard is empty",
            shard_counts=counts,
        )
    _validate_raw_receipts(bundle, counts)
    shards = _authority_shards(bundle)
    try:
        refreshed_at = datetime.fromisoformat(str(bundle["captured_at_utc"]))
        fresh_through = date.fromisoformat(str(bundle["fresh_through"]))
    except (KeyError, ValueError) as exc:
        raise NoGo("authority bundle timestamps are invalid") from exc
    if refreshed_at.tzinfo is None or refreshed_at.utcoffset() is None:
        raise NoGo("authority capture timestamp must be timezone-aware")
    from quotemux.stock_reference_authority import (
        StockAuthorityInputError,
        prepare_stock_authority_input,
    )

    try:
        return prepare_stock_authority_input(
            shards,
            source_refreshed_at_utc=refreshed_at.astimezone(UTC),
            fresh_through=fresh_through,
        )
    except StockAuthorityInputError as exc:
        raise NoGo(exc.reason, shard_counts=exc.shard_counts) from exc


def _connect(*, read_only: bool = False) -> psycopg.Connection[Any]:
    connection = psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        connect_timeout=10,
        row_factory=dict_row,
        application_name="markethub-stock-catalog-recovery",
    )
    if read_only:
        connection.execute("set transaction read only")
    return connection


def _database_baseline(connection: psycopg.Connection[Any]) -> dict[str, object]:
    database_row = connection.execute("select current_database() as value").fetchone()
    database_name = str(database_row["value"])
    schema_rows = connection.execute(
        "select table_schema,table_name,column_name,data_type,is_nullable,"
        "coalesce(column_default,'') as column_default from information_schema.columns "
        "where table_schema in ('ref','audit','readmodel') and table_name=any(%s) "
        "order by table_schema,table_name,ordinal_position",
        (list(SCHEMA_TABLES),),
    ).fetchall()
    constraint_rows = connection.execute(
        "select namespace.nspname,relation.relname,constraint_row.conname,"
        "constraint_row.contype,pg_get_constraintdef(constraint_row.oid,true) as definition "
        "from pg_constraint constraint_row "
        "join pg_class relation on relation.oid=constraint_row.conrelid "
        "join pg_namespace namespace on namespace.oid=relation.relnamespace "
        "where namespace.nspname in ('ref','audit','readmodel') "
        "and relation.relname=any(%s) "
        "order by namespace.nspname,relation.relname,constraint_row.conname",
        (list(SCHEMA_TABLES),),
    ).fetchall()
    relation_rows = connection.execute(
        "select name,to_regclass(name)::text as relation_name "
        "from unnest(%s::text[]) as relation_name_row(name) order by name",
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
        "select market,code,coalesce(name,'') as name,coalesce(industry,'') as industry,"
        "coalesce(listing_board,'') as listing_board,coalesce(listed_date::text,'') as listed_date,"
        "coalesce(delisted_date::text,'') as delisted_date,coalesce(area,'') as area "
        "from ref.stock where code<>'000000' order by market,code"
    ).fetchall()
    trade_day = connection.execute(LATEST_COMPLETED_TRADING_DAY_QUERY).fetchone()
    schema_payload = {
        "columns": [list(row.values()) for row in schema_rows],
        "constraints": [list(row.values()) for row in constraint_rows],
        "relations": [list(row.values()) for row in relation_rows],
    }
    stock_payload = [list(row.values()) for row in stock_rows]
    return {
        "database_name": database_name,
        "latest_completed_trading_day": (
            "" if trade_day is None else str(trade_day["trade_date"] or "")
        ),
        "stock_schema_sha256": _canonical_sha256(schema_payload),
        "legacy_stock_row_count": len(stock_payload),
        "legacy_stock_sha256": _canonical_sha256(stock_payload),
    }


def _health_data_version(url: str) -> str:
    with urlopen(url, timeout=10) as response:  # nosec B310 -- explicit operator URL
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or str(payload.get("status", "")) != "ok":
        raise NoGo("MarketHub health is not ok")
    value = str(payload.get("data_version", ""))
    if not DATA_VERSION.fullmatch(value):
        raise NoGo("MarketHub health returned an invalid data_version")
    return value


def _validate_release_inputs(args: argparse.Namespace) -> dict[str, str]:
    if args.release_inputs is None:
        if args.action != "rehearse":
            raise NoGo("candidate release-inputs.json is required")
        return {}
    payload = json.loads(args.release_inputs.read_text(encoding="utf-8"))
    expected = {
        "market_hub_commit": args.expected_market_hub_commit,
        "quote_mux_commit": args.expected_quote_mux_commit,
        "quote_mux_packages_commit": args.expected_quote_mux_packages_commit,
    }
    for key, value in expected.items():
        if value and str(payload.get(key, "")) != value:
            raise NoGo(
                f"release input mismatch: {key}",
                expected=value,
                actual=str(payload.get(key, "")),
            )
    actual_quote_mux = str(payload.get("quote_mux_commit", ""))
    if actual_quote_mux != REQUIRED_QUOTE_MUX_COMMIT:
        raise NoGo(
            "release does not contain the accepted QuoteMux authority predecessor",
            expected=REQUIRED_QUOTE_MUX_COMMIT,
            actual=actual_quote_mux,
        )
    actual_packages = str(payload.get("quote_mux_packages_commit", ""))
    if actual_packages != REQUIRED_QUOTE_MUX_PACKAGES_COMMIT:
        raise NoGo(
            "release does not contain the accepted QuoteMux_Packages authority provider",
            expected=REQUIRED_QUOTE_MUX_PACKAGES_COMMIT,
            actual=actual_packages,
        )
    return {key: str(payload.get(key, "")) for key in expected}


def _production_preflight(
    bundle: dict[str, Any], args: argparse.Namespace, *, allow_database_mismatch: bool = False
) -> dict[str, object]:
    authority = _prepare_authority(bundle)
    release_inputs = _validate_release_inputs(args)
    captured_database = bundle.get("database")
    if not isinstance(captured_database, dict):
        raise NoGo("authority bundle omits database baseline")
    with _connect(read_only=True) as connection:
        actual_database = _database_baseline(connection)
        connection.rollback()
    if not allow_database_mismatch:
        for key in (
            "database_name",
            "stock_schema_sha256",
            "legacy_stock_row_count",
            "legacy_stock_sha256",
        ):
            if actual_database[key] != captured_database.get(key):
                raise NoGo(
                    f"production stock baseline changed: {key}",
                    expected=captured_database.get(key),
                    actual=actual_database[key],
                )
    if actual_database["latest_completed_trading_day"] != bundle.get("fresh_through"):
        raise NoGo(
            "authority input is not fresh through the latest completed trading day",
            authority_fresh_through=bundle.get("fresh_through"),
            latest_completed_trading_day=actual_database["latest_completed_trading_day"],
        )
    if args.health_url:
        actual_version = _health_data_version(args.health_url)
        expected_version = str(bundle.get("health", {}).get("data_version", ""))
        if actual_version != expected_version:
            raise NoGo(
                "MarketHub data_version changed after authority capture",
                expected=expected_version,
                actual=actual_version,
            )
    return {
        "authority_input_id": authority.input_id,
        "authority_content_sha256": authority.content_sha256,
        "shard_counts": dict(authority.shard_counts),
        "candidate_count": len(authority.items),
        "database": actual_database,
        "release_inputs": release_inputs,
    }


def _dirty_versions(bundle: dict[str, Any], values: list[str]) -> tuple[str, ...]:
    candidates = [LEGACY_KNOWN_DIRTY_DATA_VERSION, *values]
    health = bundle.get("health")
    if isinstance(health, dict):
        candidates.append(str(health.get("data_version", "")))
    normalized = tuple(sorted({value.strip() for value in candidates if value.strip()}))
    invalid = [value for value in normalized if DATA_VERSION.fullmatch(value) is None]
    if invalid:
        raise NoGo("invalid dirty data_version", values=invalid)
    return normalized


def _result_payload(value: object) -> dict[str, object]:
    return {
        field: getattr(value, field)
        for field in value.__dataclass_fields__  # type: ignore[attr-defined]
    }


def _verify_database(
    *,
    run_key: str,
    dirty_versions: tuple[str, ...],
    expected_data_version: str = "",
    expected_input_id: str = "",
    expected_input_sha256: str = "",
) -> dict[str, object]:
    with _connect(read_only=True) as connection:
        current = connection.execute(
            "select current.catalog_version,version.content_sha256,version.row_count,"
            "version.status,current.activated_at_utc::text as activated_at_utc "
            "from readmodel.stock_catalog_current current "
            "join readmodel.stock_catalog_version version using(catalog_version) "
            "where current.singleton=true"
        ).fetchone()
        if current is None or current["status"] != "healthy":
            raise NoGo("no healthy current stock catalog")
        quality = connection.execute(
            "select count(*)::int as row_count,"
            "count(*) filter(where btrim(name)='')::int as blank_name_count,"
            "count(*)-count(distinct code) as duplicate_code_count "
            "from readmodel.stock_catalog_item where catalog_version=%s",
            (current["catalog_version"],),
        ).fetchone()
        items = connection.execute(
            "select code,name,exchange,market,list_status,"
            "coalesce(list_date::text,'') as list_date,"
            "coalesce(delist_date::text,'') as delist_date,industry,listing_board,area "
            "from readmodel.stock_catalog_item where catalog_version=%s order by code,exchange",
            (current["catalog_version"],),
        ).fetchall()
        reconciliation = connection.execute(
            "select run_key,input_id,input_sha256,fresh_through::text as fresh_through,"
            "candidate_count,existing_count,provisional_count,inserted_count,promoted_count,"
            "renamed_count,missing_count,conflict_count,transaction_result,"
            "normalized_output_sha256,audit_content_sha256 "
            "from audit.stock_reference_reconciliation where run_key=%s",
            (run_key,),
        ).fetchone()
        mappings = connection.execute(
            "select mapping.data_version,mapping.catalog_version,mapping.status,"
            "mapping.serve_until_utc::text as serve_until_utc,version.status as catalog_status,"
            "mapping.serve_until_utc>=clock_timestamp() as retained "
            "from readmodel.stock_catalog_data_version mapping "
            "left join readmodel.stock_catalog_version version using(catalog_version) "
            "order by mapping.data_version"
        ).fetchall()
        connection.rollback()
    if quality is None or int(quality["row_count"]) != int(current["row_count"]):
        raise NoGo("published stock catalog row count mismatch")
    if int(quality["blank_name_count"]) != 0 or int(quality["duplicate_code_count"]) != 0:
        raise NoGo("published stock catalog failed name or duplicate gate")
    content_sha256 = _canonical_sha256(items)
    if content_sha256 != current["content_sha256"]:
        raise NoGo("published stock catalog content hash mismatch")
    if reconciliation is None or reconciliation["transaction_result"] != "committed":
        raise NoGo("stock authority reconciliation audit is unavailable")
    if expected_input_id and reconciliation["input_id"] != expected_input_id:
        raise NoGo("reconciliation input_id does not match the frozen authority bundle")
    if expected_input_sha256 and reconciliation["input_sha256"] != expected_input_sha256:
        raise NoGo("reconciliation input hash does not match the frozen authority bundle")
    by_version = {str(row["data_version"]): row for row in mappings}
    for dirty in dirty_versions:
        row = by_version.get(dirty)
        if row is None or row["status"] != "quarantined" or row["catalog_version"] is not None:
            raise NoGo("dirty stock catalog data_version is not quarantined", data_version=dirty)
    healthy = [
        row
        for row in mappings
        if row["status"] == "healthy"
        and row["catalog_version"] is not None
        and row["catalog_status"] == "healthy"
        and bool(row["retained"])
    ]
    if expected_data_version:
        expected_mapping = by_version.get(expected_data_version)
        if (
            expected_mapping is None
            or expected_mapping not in healthy
            or expected_mapping["catalog_version"] != current["catalog_version"]
        ):
            raise NoGo("expected current healthy stock catalog data_version is unavailable")
    payload = {
        "catalog_version": str(current["catalog_version"]),
        "content_sha256": content_sha256,
        "row_count": int(quality["row_count"]),
        "blank_name_count": 0,
        "duplicate_code_count": 0,
        "reconciliation": dict(reconciliation),
        "healthy_rollback_data_versions": [str(row["data_version"]) for row in healthy],
        "quarantined_data_versions": list(dirty_versions),
    }
    payload["verification_sha256"] = _canonical_sha256(payload)
    return payload


def _apply(
    bundle: dict[str, Any], args: argparse.Namespace, *, rehearsal: bool
) -> dict[str, object]:
    if not rehearsal and args.apply_token != APPLY_TOKEN:
        raise NoGo("controller apply token is required")
    database_name = os.environ.get("MARKETHUB_DB_NAME", "")
    if rehearsal and REHEARSAL_DATABASE.search(database_name) is None:
        raise NoGo(
            "rehearse requires an isolated database name containing spec3, rehearsal, or test"
        )
    preflight = _production_preflight(bundle, args, allow_database_mismatch=rehearsal)
    authority = _prepare_authority(bundle)
    dirty = _dirty_versions(bundle, args.known_dirty_data_version)
    from quotemux.stock_reference_authority import (
        apply_stock_reference_authority_migration,
        freeze_stock_authority_input,
        reconcile_stock_authority_input,
    )
    from services.market_data_version import market_data_version_for_stock_catalog
    from services.stock_catalog_candidate import build_current_stock_catalog_candidate
    from services.stock_catalog_publication import publish_stock_catalog_candidate

    args.mutation_started = True
    apply_stock_reference_authority_migration()
    frozen = freeze_stock_authority_input(
        _authority_shards(bundle),
        source_refreshed_at_utc=authority.source_refreshed_at_utc,
        fresh_through=authority.fresh_through,
    )
    if frozen.input_id != authority.input_id or frozen.content_sha256 != authority.content_sha256:
        raise RuntimeError("persisted authority input differs from the validated bundle")
    first = reconcile_stock_authority_input(frozen, run_key=args.run_key)
    replay = reconcile_stock_authority_input(frozen, run_key=args.run_key)
    first_payload = _result_payload(first)
    replay_payload = _result_payload(replay)
    for field in ("input_id", "normalized_output_sha256", "audit_content_sha256"):
        if first_payload[field] != replay_payload[field]:
            raise NoGo("same-run-key reconciliation replay changed evidence", field=field)
    candidate = build_current_stock_catalog_candidate()
    data_version = market_data_version_for_stock_catalog(candidate.version)
    if not DATA_VERSION.fullmatch(data_version):
        raise NoGo("catalog candidate could not mint a valid data_version")
    publication = publish_stock_catalog_candidate(
        candidate,
        data_version=data_version,
        known_dirty_data_versions=dirty,
    )
    verification = _verify_database(
        run_key=args.run_key,
        dirty_versions=dirty,
        expected_data_version=data_version,
        expected_input_id=authority.input_id,
        expected_input_sha256=authority.content_sha256,
    )
    return {
        "decision": "go",
        "mode": "rehearse" if rehearsal else "apply",
        "bundle_sha256": bundle["bundle_sha256"],
        "preflight": preflight,
        "reconciliation": first_payload,
        "replay": replay_payload,
        "publication": _result_payload(publication),
        "verification": verification,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("preflight", "rehearse", "apply", "verify"):
        action = actions.add_parser(name)
        action.add_argument("--bundle", type=Path, required=True)
        action.add_argument("--run-key", required=name != "preflight", default="")
        action.add_argument("--known-dirty-data-version", action="append", default=[])
        action.add_argument("--health-url", default="")
        action.add_argument("--release-inputs", type=Path)
        action.add_argument("--expected-market-hub-commit", default="")
        action.add_argument("--expected-quote-mux-commit", default="")
        action.add_argument("--expected-quote-mux-packages-commit", default="")
        if name == "apply":
            action.add_argument("--apply-token", required=True)
        if name == "verify":
            action.add_argument("--expected-data-version", default="")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        bundle = _load_bundle(args.bundle)
        dirty = _dirty_versions(bundle, args.known_dirty_data_version)
        if args.action == "preflight":
            result = {
                "decision": "go",
                "mode": "preflight",
                "bundle_sha256": bundle["bundle_sha256"],
                **_production_preflight(bundle, args),
                "known_dirty_data_versions": list(dirty),
                "mutation_performed": False,
            }
        elif args.action == "verify":
            authority = _prepare_authority(bundle)
            _validate_release_inputs(args)
            result = {
                "decision": "go",
                "mode": "verify",
                "bundle_sha256": bundle["bundle_sha256"],
                **_verify_database(
                    run_key=args.run_key,
                    dirty_versions=dirty,
                    expected_data_version=args.expected_data_version,
                    expected_input_id=authority.input_id,
                    expected_input_sha256=authority.content_sha256,
                ),
                "mutation_performed": False,
            }
        else:
            result = _apply(bundle, args, rehearsal=args.action == "rehearse")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    except NoGo as exc:
        print(
            json.dumps(
                {
                    "decision": "no-go",
                    "mode": args.action,
                    "reason": exc.reason,
                    "details": exc.details,
                    "mutation_performed": bool(getattr(args, "mutation_started", False)),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        )
        return NO_GO_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
