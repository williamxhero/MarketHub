from __future__ import annotations

"""Acquire, validate, import, and verify exact Tushare daily/suspension facts."""

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Iterable
from urllib.request import urlopen

import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd


PARQUET_COLUMNS = (
    "market",
    "code",
    "trade_date",
    "classification",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "pct_chg",
    "volume",
    "amount",
    "adj_factor",
    "is_st",
    "provider_code",
    "source_status",
    "suspend_timing",
)
DAILY_SOURCE = "Tushare.daily"
SUSPENSION_SOURCE = "Tushare.suspend_d"
SUSPENSION_MARKER = "suspend_type_S_full_day_no_daily"
BSE_MAPPING_SOURCE_URL = "https://www.bse.cn/service/code_mapping.html"
BSE_PROVIDER_ALIASES = {"920680": ("839680.BJ",)}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def parse_failure_details(path: Path, codes: set[str]) -> tuple[str, dict[str, tuple[date, ...]]]:
    outer = load_json(path)
    if outer.get("contract") == "markethub-stock-daily-all-a-audit-v1":
        scope = outer.get("scope")
        if scope not in {"exhaustive", "exhaustive_filtered_exact_keys"}:
            raise ValueError("daily audit target scope is not exhaustive")
        if scope == "exhaustive_filtered_exact_keys" and not isinstance(outer.get("derived_from"), dict):
            raise ValueError("filtered daily audit has no source provenance")
        selected_dates: dict[str, set[date]] = {}
        for entry in outer.get("gaps", []):
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code", "")).zfill(6)
            if code not in codes:
                continue
            selected_dates.setdefault(code, set()).add(date.fromisoformat(str(entry.get("trade_date", ""))))
        selected = {code: tuple(sorted(values)) for code, values in selected_dates.items()}
        if set(selected) != codes:
            raise ValueError(f"daily audit does not contain every requested code: {sorted(codes - set(selected))}")
        return "", selected
    error = str(outer.get("error", ""))
    marker = "details="
    if marker not in error:
        raise ValueError("adapter failure has no structured details")
    details, _ = json.JSONDecoder().raw_decode(error.split(marker, 1)[1])
    if not isinstance(details, dict):
        raise ValueError("adapter failure details are not an object")
    selected: dict[str, tuple[date, ...]] = {}
    for entry in details.get("codes", []):
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code", "")).zfill(6)
        if code not in codes:
            continue
        dates = tuple(sorted({date.fromisoformat(str(value)) for value in entry.get("missing_trade_dates", [])}))
        if dates:
            selected[code] = dates
    if set(selected) != codes:
        raise ValueError(f"adapter failure does not contain every requested code: {sorted(codes - set(selected))}")
    return str(details.get("data_version", "")), selected


def market_for_code(code: str) -> str:
    if code.startswith(("600", "601", "603", "605", "688")):
        return "SHSE"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return "SZSE"
    if code.startswith(("4", "8", "920")):
        return "BJSE"
    raise ValueError(f"unsupported exact Tushare repair code: {code}")


def provider_code_for(code: str) -> str:
    market = market_for_code(code)
    suffix = {"SHSE": "SH", "SZSE": "SZ", "BJSE": "BJ"}[market]
    return f"{code}.{suffix}"


def provider_codes_for(code: str) -> tuple[str, ...]:
    return (provider_code_for(code), *BSE_PROVIDER_ALIASES.get(code, ()))


def _normalized_ts_code(value: object) -> str:
    text = _text(value).upper()
    if not text:
        return ""
    code, separator, suffix = text.partition(".")
    if len(code) != 6 or not code.isdigit() or separator != "." or suffix != "BJ":
        raise ValueError(f"invalid Tushare BSE mapping code: {value}")
    return f"{code}.BJ"


def build_bse_provider_identities(
    records: list[dict[str, object]],
    codes: set[str],
) -> dict[str, tuple[str, tuple[str, ...]]]:
    old_to_new: dict[str, str] = {}
    new_to_old: dict[str, str] = {}
    for record in records:
        old_ts_code = _normalized_ts_code(record.get("o_code"))
        new_ts_code = _normalized_ts_code(record.get("n_code"))
        old_code = old_ts_code.split(".", 1)[0]
        new_code = new_ts_code.split(".", 1)[0]
        if old_code in old_to_new and old_to_new[old_code] != new_ts_code:
            raise ValueError(f"conflicting Tushare BSE mapping for old code: {old_code}")
        if new_code in new_to_old and new_to_old[new_code] != old_ts_code:
            raise ValueError(f"conflicting Tushare BSE mapping for new code: {new_code}")
        old_to_new[old_code] = new_ts_code
        new_to_old[new_code] = old_ts_code
    identities: dict[str, tuple[str, tuple[str, ...]]] = {}
    for code in sorted(codes):
        if market_for_code(code) != "BJSE":
            identities[code] = (provider_code_for(code), ())
            continue
        if code.startswith(("4", "8")):
            primary = old_to_new.get(code)
            if primary is None:
                raise ValueError(f"Tushare BSE mapping unavailable for old code: {code}")
            identities[code] = (primary, (provider_code_for(code),))
            continue
        old_alias = new_to_old.get(code)
        aliases = (old_alias,) if old_alias else BSE_PROVIDER_ALIASES.get(code, ())
        identities[code] = (provider_code_for(code), aliases)
    return identities


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _number(record: dict[str, object], field: str) -> float:
    value = record.get(field)
    if value is None or _text(value) == "":
        raise ValueError(f"missing numeric field {field}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"non-finite numeric field {field}")
    return result


def _is_full_day_suspension(record: dict[str, object]) -> bool:
    return _text(record.get("suspend_type")).upper() == "S" and _text(record.get("suspend_timing")) == ""


def classify_tushare_target(
    code: str,
    target_date: date,
    daily_record: dict[str, object] | None,
    suspension_records: list[dict[str, object]],
    adj_factor: float | None,
    risk_records: list[dict[str, object]],
    *,
    primary_provider_code: str | None = None,
    provider_aliases: tuple[str, ...] = (),
) -> tuple[dict[str, object] | None, str | None]:
    provider_code = primary_provider_code or provider_code_for(code)
    allowed_provider_codes = {provider_code, *provider_aliases}
    if primary_provider_code is None:
        allowed_provider_codes.update(provider_codes_for(code))
    for record in suspension_records:
        if _text(record.get("ts_code")) not in allowed_provider_codes:
            raise ValueError(f"suspension identity mismatch: {code}/{target_date}")
        if _text(record.get("trade_date")) != target_date.strftime("%Y%m%d"):
            raise ValueError(f"suspension date mismatch: {code}/{target_date}")
    full_day = [record for record in suspension_records if _is_full_day_suspension(record)]
    intraday = [
        record
        for record in suspension_records
        if _text(record.get("suspend_type")).upper() == "S" and _text(record.get("suspend_timing")) != ""
    ]
    if daily_record is not None:
        if full_day:
            raise ValueError(f"conflicting full-day suspension and daily row: {code}/{target_date}")
        if _text(daily_record.get("ts_code")) != provider_code:
            raise ValueError(f"Tushare identity mismatch: {code}/{target_date}")
        if _text(daily_record.get("trade_date")) != target_date.strftime("%Y%m%d"):
            raise ValueError(f"Tushare date mismatch: {code}/{target_date}")
        if adj_factor is None or not math.isfinite(float(adj_factor)) or float(adj_factor) <= 0:
            return None, "missing_positive_adj_factor"
        values = {
            field: _number(daily_record, field)
            for field in ("open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount")
        }
        if values["high"] < max(values["open"], values["low"], values["close"]):
            raise ValueError(f"invalid OHLC high: {code}/{target_date}")
        if values["low"] > min(values["open"], values["high"], values["close"]):
            raise ValueError(f"invalid OHLC low: {code}/{target_date}")
        scaled_volume = values["vol"] * 100.0
        if scaled_volume < 0 or values["amount"] < 0 or not math.isclose(scaled_volume, round(scaled_volume), abs_tol=1e-6):
            raise ValueError(f"invalid Tushare volume/amount: {code}/{target_date}")
        is_st = any(
            _text(record.get("ts_code")) == provider_code
            and _text(record.get("trade_date")) == target_date.strftime("%Y%m%d")
            for record in risk_records
        )
        return {
            "market": market_for_code(code),
            "code": code,
            "trade_date": target_date,
            "classification": "traded_daily",
            "open": values["open"],
            "high": values["high"],
            "low": values["low"],
            "close": values["close"],
            "pre_close": values["pre_close"],
            "change": values["change"],
            "pct_chg": values["pct_chg"],
            "volume": int(round(scaled_volume)),
            "amount": values["amount"] * 1000.0,
            "adj_factor": float(adj_factor),
            "is_st": is_st,
            "provider_code": provider_code,
            "source_status": "daily",
            "suspend_timing": _text(intraday[0].get("suspend_timing")) if intraday else None,
        }, None
    if full_day:
        if len(full_day) != 1:
            raise ValueError(f"duplicate full-day suspension evidence: {code}/{target_date}")
        return {
            "market": market_for_code(code),
            "code": code,
            "trade_date": target_date,
            "classification": "suspended",
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "pre_close": None,
            "change": None,
            "pct_chg": None,
            "volume": None,
            "amount": None,
            "adj_factor": None,
            "is_st": False,
            "provider_code": _text(full_day[0].get("ts_code")),
            "source_status": "suspend_d:S",
            "suspend_timing": None,
        }, None
    if intraday:
        return None, "intraday_suspension_without_daily"
    return None, "no_daily_or_full_day_suspension"


def _load_service_environment() -> None:
    env_path = Path("/data/markethub/env/markethub.env")
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        if raw and not raw.startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            os.environ.setdefault(key, value)
    release = os.getenv("MARKETHUB_RELEASE", "")
    os.environ.setdefault("MARKETHUB_RUNTIME_ROOT", "/data/markethub")
    os.environ.setdefault("MARKETHUB_DATA_ROOT", "/data/markethub/store")
    os.environ.setdefault("QUOTEMUX_RUNTIME_ROOT", "/data/markethub/runtime")
    os.environ.setdefault("QUOTEMUX_PACKAGE_REPO_SPEC", "/data/MarketHub2/current/QuoteMux_Packages")
    os.environ.setdefault("QUOTEMUX_PACKAGE_VENV_ROOT", f"/data/markethub/package_venvs/{release}")
    for value in (
        "/data/MarketHub2/current/QuoteMux/src",
        "/data/MarketHub2/current/MarketHub/services/markethub_api/src",
    ):
        if value not in sys.path:
            sys.path.insert(0, value)


def _plain_records(frame: Any) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for raw in frame.to_dict(orient="records"):
        record: dict[str, object] = {}
        for key, value in raw.items():
            if value is None or (not isinstance(value, (str, bytes)) and bool(getattr(value, "size", 1) == 1) and bool(pd.isna(value))):
                record[str(key)] = None
            elif hasattr(value, "item"):
                record[str(key)] = value.item()
            else:
                record[str(key)] = value
        records.append(record)
    return records


def _fetch_tushare(
    targets: dict[str, tuple[date, ...]],
) -> tuple[
    str,
    dict[str, dict[str, list[dict[str, object]]]],
    dict[str, tuple[str, tuple[str, ...]]],
    list[dict[str, object]],
]:
    _load_service_environment()
    from quotemux.settings import QuoteMuxSettings
    from quotemux.source_packages.instance_context import use_source_instance
    from quotemux_packages.tushare.rate_limit import call_tushare_api
    from quotemux_packages.tushare.source import get_ts_pro

    settings = QuoteMuxSettings()
    instances = settings.get_contract_source_instances("stocks.factors.adj", ("tushare",))
    instance = next((item for item in instances if item.package_id == "tushare"), None)
    if instance is None:
        raise RuntimeError("tushare source instance unavailable")
    output: dict[str, dict[str, list[dict[str, object]]]] = {}
    with use_source_instance(instance):
        provider = get_ts_pro()
        if provider is None:
            raise RuntimeError("Tushare provider unavailable")
        mapping_frame = call_tushare_api("bse_mapping", provider.bse_mapping)
        bse_mapping_records = _plain_records(mapping_frame)
        provider_identities = build_bse_provider_identities(bse_mapping_records, set(targets))
        for code, dates in sorted(targets.items()):
            ts_code, aliases = provider_identities[code]
            kwargs = {
                "ts_code": ts_code,
                "start_date": min(dates).strftime("%Y%m%d"),
                "end_date": max(dates).strftime("%Y%m%d"),
            }
            per_code: dict[str, list[dict[str, object]]] = {}
            for api_name in ("daily", "adj_factor", "suspend_d", "stock_st"):
                fetcher = getattr(provider, api_name, None)
                if not callable(fetcher):
                    raise RuntimeError(f"Tushare endpoint unavailable: {api_name}")
                frame = call_tushare_api(api_name, fetcher, **kwargs)
                per_code[api_name] = _plain_records(frame)
            per_code["suspend_d_alias"] = []
            for alias in aliases:
                alias_frame = call_tushare_api(
                    "suspend_d",
                    provider.suspend_d,
                    ts_code=alias,
                    start_date=kwargs["start_date"],
                    end_date=kwargs["end_date"],
                )
                per_code["suspend_d_alias"].extend(_plain_records(alias_frame))
            output[code] = per_code
    return instance.instance_id, output, provider_identities, bse_mapping_records


def health(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise RuntimeError(f"unhealthy MarketHub response: {payload}")
    return payload


def _index_unique(records: list[dict[str, object]], field: str, code: str, source: str) -> dict[str, dict[str, object]]:
    indexed: dict[str, dict[str, object]] = {}
    for record in records:
        key = _text(record.get(field))
        if key in indexed:
            raise ValueError(f"duplicate {source} date for {code}/{key}")
        indexed[key] = record
    return indexed


def export_artifact(
    failure_path: Path,
    output_root: Path,
    codes: set[str],
    expected_count: int,
    expected_release: str,
    health_url: str,
) -> dict[str, Any]:
    source_audit_data_version, targets = parse_failure_details(failure_path, codes)
    if sum(map(len, targets.values())) != expected_count:
        raise ValueError("unexpected target count")
    before_health = health(health_url)
    if before_health.get("version") != expected_release:
        raise RuntimeError("live release does not match expected release")
    if not source_audit_data_version:
        source_audit_data_version = str(before_health.get("data_version", ""))
    source_instance_id, raw, provider_identities, bse_mapping_records = _fetch_tushare(targets)
    after_health = health(health_url)
    if after_health.get("version") != before_health.get("version") or after_health.get("data_version") != before_health.get("data_version"):
        raise RuntimeError("live release/data version drifted during Tushare export")
    rows: list[dict[str, object]] = []
    residuals: list[dict[str, object]] = []
    for code, dates in sorted(targets.items()):
        provider = raw[code]
        primary_provider_code, provider_aliases = provider_identities[code]
        daily = _index_unique(provider["daily"], "trade_date", code, "daily")
        factors = _index_unique(provider["adj_factor"], "trade_date", code, "adj_factor")
        suspensions: dict[str, list[dict[str, object]]] = {}
        for record in provider["suspend_d"] + provider["suspend_d_alias"]:
            suspensions.setdefault(_text(record.get("trade_date")), []).append(record)
        risks: dict[str, list[dict[str, object]]] = {}
        for record in provider["stock_st"]:
            risks.setdefault(_text(record.get("trade_date")), []).append(record)
        for target_date in dates:
            key = target_date.strftime("%Y%m%d")
            factor_record = factors.get(key)
            factor = float(factor_record["adj_factor"]) if factor_record and _text(factor_record.get("adj_factor")) else None
            normalized, reason = classify_tushare_target(
                code,
                target_date,
                daily.get(key),
                suspensions.get(key, []),
                factor,
                risks.get(key, []),
                primary_provider_code=primary_provider_code,
                provider_aliases=provider_aliases,
            )
            if normalized is None:
                residuals.append({
                    "market": market_for_code(code),
                    "code": code,
                    "trade_date": target_date.isoformat(),
                    "provider_code": primary_provider_code,
                    "reason": reason,
                })
            else:
                rows.append(normalized)
    qualified_keys = {(row["market"], row["code"], row["trade_date"]) for row in rows}
    residual_keys = {(row["market"], row["code"], row["trade_date"]) for row in residuals}
    if len(qualified_keys) != len(rows) or len(residual_keys) != len(residuals) or qualified_keys & residual_keys:
        raise ValueError("duplicate or overlapping normalized target keys")
    if len(rows) + len(residuals) != expected_count:
        raise ValueError("normalized target accounting mismatch")

    partial = output_root.with_name(output_root.name + ".partial")
    if output_root.exists() or partial.exists():
        raise FileExistsError(output_root if output_root.exists() else partial)
    partial.mkdir(parents=True)
    raw_path = partial / "tushare_raw.json"
    parquet_path = partial / "exact_daily_gap.parquet"
    residual_path = partial / "residuals.json"
    raw_payload = {
        "contract": "qm-tushare-exact-daily-raw-v1",
        "provider_version": importlib.metadata.version("tushare"),
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_instance_id": source_instance_id,
        "source_audit_data_version": source_audit_data_version,
        "target_data_version": before_health["data_version"],
        "failure_sha256": sha256(failure_path),
        "targets": {code: [value.isoformat() for value in dates] for code, dates in sorted(targets.items())},
        "responses": raw,
        "authoritative_bse_code_mapping": {
            "source_url": BSE_MAPPING_SOURCE_URL,
            "tushare_api": "bse_mapping",
            "records": bse_mapping_records,
            "canonical_to_provider_identity": {
                code: {"primary": primary, "aliases": list(aliases)}
                for code, (primary, aliases) in sorted(provider_identities.items())
                if market_for_code(code) == "BJSE"
            },
        },
    }
    write_json(raw_path, raw_payload)
    write_json(residual_path, {"contract": "qm-tushare-exact-daily-residuals-v1", "items": residuals})
    table = pa.Table.from_pylist(rows) if rows else pa.table({column: [] for column in PARQUET_COLUMNS})
    if tuple(table.column_names) != PARQUET_COLUMNS:
        raise ValueError(f"unexpected normalized schema: {table.column_names}")
    pq.write_table(table, parquet_path, compression="snappy")
    counts = Counter(str(row["classification"]) for row in rows)
    manifest = {
        "contract": "qm-tushare-exact-daily-backfill-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "expected_release": expected_release,
        "source_audit_data_version": source_audit_data_version,
        "target_data_version": before_health["data_version"],
        "failure_path": str(failure_path),
        "failure_sha256": sha256(failure_path),
        "sources": [
            DAILY_SOURCE,
            "Tushare.adj_factor",
            SUSPENSION_SOURCE,
            "Tushare.stock_st",
            BSE_MAPPING_SOURCE_URL,
        ],
        "source_units": {"vol": "lots", "amount": "thousand_CNY"},
        "normalized_units": {"volume": "shares", "amount": "CNY", "pct_chg": "percent"},
        "suspension_semantics": SUSPENSION_MARKER,
        "target_count": expected_count,
        "qualified_count": len(rows),
        "residual_count": len(residuals),
        "traded_daily_count": counts["traded_daily"],
        "suspended_count": counts["suspended"],
        "files": {
            raw_path.name: {"bytes": raw_path.stat().st_size, "sha256": sha256(raw_path)},
            parquet_path.name: {"bytes": parquet_path.stat().st_size, "sha256": sha256(parquet_path), "rows": len(rows)},
            residual_path.name: {"bytes": residual_path.stat().st_size, "sha256": sha256(residual_path), "rows": len(residuals)},
        },
    }
    write_json(partial / "manifest.json", manifest)
    partial.replace(output_root)
    return manifest


def load_env(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw and not raw.startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            os.environ.setdefault(key, value)


def connect():
    import psycopg

    return psycopg.connect(
        host=os.getenv("MARKETHUB_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("MARKETHUB_DB_PORT", "5432")),
        dbname=os.getenv("MARKETHUB_DB_NAME", "datalake_dev"),
        user=os.getenv("MARKETHUB_DB_USER", "markethub"),
        password=os.getenv("MARKETHUB_DB_PASSWORD", ""),
        connect_timeout=30,
    )


def validate_artifact(root: Path) -> tuple[dict[str, Any], list[dict[str, object]]]:
    manifest = load_json(root / "manifest.json")
    if manifest.get("contract") not in {
        "qm-tushare-exact-daily-backfill-v1",
        "qm-bshare-exact-daily-backfill-v1",
    }:
        raise ValueError("unexpected artifact contract")
    for name, expected in manifest.get("files", {}).items():
        path = root / str(name)
        if not path.is_file() or path.stat().st_size != int(expected["bytes"]) or sha256(path) != expected["sha256"]:
            raise ValueError(f"artifact hash/size mismatch: {name}")
    parquet = pq.ParquetFile(root / "exact_daily_gap.parquet")
    if tuple(parquet.schema_arrow.names) != PARQUET_COLUMNS:
        raise ValueError("normalized parquet schema mismatch")
    codecs = {
        str(parquet.metadata.row_group(group).column(column).compression).upper()
        for group in range(parquet.metadata.num_row_groups)
        for column in range(parquet.metadata.row_group(group).num_columns)
    }
    if codecs != {"SNAPPY"}:
        raise ValueError(f"normalized parquet codec mismatch: {sorted(codecs)}")
    rows = parquet.read().to_pylist()
    if len(rows) != int(manifest["qualified_count"]):
        raise ValueError("normalized row count mismatch")
    if len({(row["market"], row["code"], row["trade_date"]) for row in rows}) != len(rows):
        raise ValueError("normalized duplicate keys")
    counts = Counter(str(row["classification"]) for row in rows)
    if counts != Counter({"traded_daily": int(manifest["traded_daily_count"]), "suspended": int(manifest["suspended_count"])}):
        raise ValueError("normalized classification counts mismatch")
    for row in rows:
        if row["market"] not in {"SZSE", "SHSE", "BJSE"} or row["classification"] not in {"traded_daily", "suspended"}:
            raise ValueError("unsupported normalized identity/classification")
        if row["classification"] == "traded_daily":
            required = ("open", "high", "low", "close", "pre_close", "volume", "amount", "adj_factor", "pct_chg")
            if any(row[field] is None for field in required) or float(row["adj_factor"]) <= 0:
                raise ValueError(f"traded row lacks required values: {row['code']}/{row['trade_date']}")
        elif any(row[field] is not None for field in ("open", "high", "low", "close", "volume", "amount", "adj_factor")):
            raise ValueError(f"suspension row contains fabricated market values: {row['code']}/{row['trade_date']}")
    return manifest, rows


def _create_stage(cursor) -> None:
    cursor.execute(
        """
        create temp table qm_tushare_exact_daily_stage (
            market varchar not null, code varchar not null, trade_date date not null,
            classification text not null, open double precision, high double precision,
            low double precision, close double precision, pre_close double precision,
            change double precision, pct_chg double precision, volume bigint,
            amount double precision, adj_factor double precision, is_st boolean not null,
            primary key (market, code, trade_date)
        ) on commit drop
        """
    )


def _copy_stage(cursor, rows: Iterable[dict[str, object]]) -> None:
    columns = (
        "market", "code", "trade_date", "classification", "open", "high", "low", "close",
        "pre_close", "change", "pct_chg", "volume", "amount", "adj_factor", "is_st",
    )
    with cursor.copy(
        "copy qm_tushare_exact_daily_stage (" + ",".join(columns) + ") from stdin"
    ) as copy:
        for row in rows:
            copy.write_row(tuple(row[column] for column in columns))


def _coverage(cursor, data_version: str) -> dict[str, int]:
    cursor.execute(
        """
        select
          (select count(*) from qm_tushare_exact_daily_stage)::int as qualified_rows,
          (select count(*) from qm_tushare_exact_daily_stage where classification='traded_daily')::int as traded_targets,
          (select count(*) from qm_tushare_exact_daily_stage where classification='suspended')::int as suspended_targets,
          (select count(*) from qm_tushare_exact_daily_stage s join fact.stock_daily_1d d using (market,code,trade_date))::int as daily_rows,
          (select count(*) from qm_tushare_exact_daily_stage s join fact.stock_daily_1d d using (market,code,trade_date)
           where s.classification='traded_daily')::int as traded_daily_rows,
          (select count(*) from qm_tushare_exact_daily_stage s join fact.stock_daily_1d d using (market,code,trade_date)
           where s.classification='suspended'
             and (not coalesce(d.is_suspended,false) or coalesce(d.volume,0)<>0))::int as conflicting_suspended_daily_rows,
          (select count(*) from qm_tushare_exact_daily_stage s where s.classification='suspended' and exists (
             select 1 from fact.stock_suspension_history h
             where h.market=s.market and h.code=s.code and h.status='suspended'
               and s.trade_date between h.suspend_start_date and h.suspend_end_date
          ))::int as suspension_rows,
          (select count(*) from qm_tushare_exact_daily_stage s
           where s.classification='traded_daily' and not exists (
             select 1 from fact.stock_daily_1d d
             where d.market=s.market and d.code=s.code and d.trade_date=s.trade_date
               and d.open is not distinct from s.open and d.high is not distinct from s.high
               and d.low is not distinct from s.low and d.close is not distinct from s.close
               and d.pre_close is not distinct from s.pre_close and d.change is not distinct from s.change
               and d.pct_chg is not distinct from s.pct_chg and d.volume is not distinct from s.volume
               and d.amount is not distinct from s.amount and d.adj_factor is not distinct from s.adj_factor
               and d.is_st is not distinct from s.is_st and d.is_suspended=false
           ))::int as daily_value_mismatches,
          (select count(*) from qm_tushare_exact_daily_stage s
           where s.classification='suspended' and not exists (
             select 1 from fact.stock_suspension_history h
             where h.market=s.market and h.code=s.code
               and h.suspend_start_date=s.trade_date and h.suspend_end_date=s.trade_date
               and h.status='suspended' and h.source=%s and h.source_marker=%s
               and h.data_version=%s
           ))::int as suspension_lineage_mismatches
        """,
        (SUSPENSION_SOURCE, SUSPENSION_MARKER, data_version),
    )
    return dict(cursor.fetchone())


def _assert_import_preconditions(coverage: dict[str, int]) -> None:
    if (
        coverage["traded_daily_rows"] != 0
        or coverage["suspension_rows"] != 0
        or coverage["conflicting_suspended_daily_rows"] != 0
    ):
        raise RuntimeError(f"target facts changed since baseline: {coverage}")


def _assert_import_postconditions(coverage: dict[str, int]) -> None:
    if (
        coverage["traded_daily_rows"] != coverage["traded_targets"]
        or coverage["suspension_rows"] != coverage["suspended_targets"]
        or coverage["daily_value_mismatches"] != 0
        or coverage["suspension_lineage_mismatches"] != 0
        or coverage["conflicting_suspended_daily_rows"] != 0
    ):
        raise RuntimeError(f"post-insert exact coverage mismatch: {coverage}")


def import_artifact(root: Path, env_path: Path, audit_path: Path, health_url: str, apply: bool) -> dict[str, Any]:
    import psycopg

    manifest, rows = validate_artifact(root)
    load_env(env_path)
    before_health = health(health_url)
    if before_health.get("version") != manifest["expected_release"]:
        raise RuntimeError("live release drifted from frozen artifact")
    if before_health.get("data_version") != manifest["target_data_version"]:
        raise RuntimeError("live data version drifted from frozen artifact")
    with connect() as connection, connection.cursor(row_factory=psycopg.rows.dict_row) as cursor:
        _create_stage(cursor)
        _copy_stage(cursor, rows)
        before = _coverage(cursor, str(manifest["target_data_version"]))
        _assert_import_preconditions(before)
        inserted_daily = 0
        inserted_suspensions = 0
        if apply:
            cursor.execute(
                """
                insert into fact.stock_daily_1d
                  (market, code, trade_date, open, high, low, close, volume, amount,
                   adj_factor, is_suspended, is_st, pre_close, change, pct_chg, loaded_at)
                select market, code, trade_date, open, high, low, close, volume, amount,
                       adj_factor, false, is_st, pre_close, change, pct_chg, now()
                from qm_tushare_exact_daily_stage where classification='traded_daily'
                order by market, code, trade_date
                on conflict (market, code, trade_date) do nothing
                """
            )
            inserted_daily = cursor.rowcount
            cursor.execute(
                """
                insert into fact.stock_suspension_history
                  (market, code, suspend_start_date, suspend_end_date, resume_date,
                   status, source, source_marker, captured_at_utc, data_version, loaded_at)
                select market, code, trade_date, trade_date, null, 'suspended', %s, %s,
                       %s::timestamptz, %s, now()
                from qm_tushare_exact_daily_stage where classification='suspended'
                order by market, code, trade_date
                on conflict (market, code, suspend_start_date, data_version) do nothing
                """,
                (SUSPENSION_SOURCE, SUSPENSION_MARKER, manifest["created_at_utc"], manifest["target_data_version"]),
            )
            inserted_suspensions = cursor.rowcount
            after = _coverage(cursor, str(manifest["target_data_version"]))
            _assert_import_postconditions(after)
            connection.commit()
        else:
            after = before
            connection.rollback()
    after_health = health(health_url)
    report = {
        "contract": "qm-exact-daily-import-v1",
        "artifact_contract": manifest["contract"],
        "mode": "apply" if apply else "dry-run",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "artifact_root": str(root),
        "manifest_sha256": sha256(root / "manifest.json"),
        "before_health": before_health,
        "after_health": after_health,
        "before": before,
        "after": after,
        "inserted_daily_rows": inserted_daily,
        "inserted_suspension_rows": inserted_suspensions,
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(audit_path, report)
    return report


def verify_import(root: Path, env_path: Path, audit_path: Path, health_url: str) -> dict[str, Any]:
    import psycopg

    manifest, rows = validate_artifact(root)
    load_env(env_path)
    current_health = health(health_url)
    if current_health.get("version") != manifest["expected_release"]:
        raise RuntimeError("live release drifted from frozen artifact")
    with connect() as connection, connection.cursor(row_factory=psycopg.rows.dict_row) as cursor:
        _create_stage(cursor)
        _copy_stage(cursor, rows)
        coverage = _coverage(cursor, str(manifest["target_data_version"]))
        connection.rollback()
    if (
        coverage["qualified_rows"] != int(manifest["qualified_count"])
        or coverage["traded_daily_rows"] != int(manifest["traded_daily_count"])
        or coverage["suspension_rows"] != int(manifest["suspended_count"])
        or coverage["daily_value_mismatches"] != 0
        or coverage["suspension_lineage_mismatches"] != 0
        or coverage["conflicting_suspended_daily_rows"] != 0
    ):
        raise RuntimeError(f"exact imported values/lineage are incomplete: {coverage}")
    report = {
        "contract": "qm-exact-daily-verification-v1",
        "artifact_contract": manifest["contract"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "artifact_root": str(root),
        "manifest_sha256": sha256(root / "manifest.json"),
        "health": current_health,
        "coverage": coverage,
        "status": "verified_complete",
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(audit_path, report)
    return report


def parse_codes(value: str) -> set[str]:
    codes = {item.strip().zfill(6) for item in value.split(",") if item.strip()}
    for code in codes:
        market_for_code(code)
    return codes


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export")
    export.add_argument("--failure-json", type=Path, required=True)
    export.add_argument("--output-root", type=Path, required=True)
    export.add_argument("--codes", type=parse_codes, required=True)
    export.add_argument("--expected-count", type=int, required=True)
    export.add_argument("--expected-release", required=True)
    export.add_argument("--health-url", default="http://127.0.0.1:8803/api/health")
    for name in ("import", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("--artifact-root", type=Path, required=True)
        command.add_argument("--env", type=Path, default=Path("/data/markethub/env/markethub.env"))
        command.add_argument("--audit-output", type=Path, required=True)
        command.add_argument("--health-url", default="http://127.0.0.1:8803/api/health")
        if name == "import":
            command.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command == "export":
        result = export_artifact(
            args.failure_json,
            args.output_root,
            args.codes,
            args.expected_count,
            args.expected_release,
            args.health_url,
        )
    elif args.command == "import":
        result = import_artifact(args.artifact_root, args.env, args.audit_output, args.health_url, args.apply)
    else:
        result = verify_import(args.artifact_root, args.env, args.audit_output, args.health_url)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
