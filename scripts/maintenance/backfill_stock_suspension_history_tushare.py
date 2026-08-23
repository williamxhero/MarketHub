from __future__ import annotations

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
from typing import Any
from urllib.request import urlopen

import pandas as pd
import psycopg
from psycopg.rows import dict_row


CONTRACT = "markethub-stock-suspension-tushare-remediation-v1"
SOURCE = "Tushare.suspend_d"
SOURCE_MARKER = "suspend_type_S_full_day_no_daily"
BAOSTOCK_SOURCE = "BaoStock.query_history_k_data_plus"
BAOSTOCK_MARKER = "tradestatus_0_zero_volume_amount"
BSE_INCEPTION = date(2021, 11, 15)
BAOSTOCK_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus,isST"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _load_service_environment() -> None:
    for raw in Path("/data/markethub/env/markethub.env").read_text(encoding="utf-8").splitlines():
        if raw and not raw.startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            os.environ.setdefault(key, value)
    os.environ.setdefault("MARKETHUB_RUNTIME_ROOT", "/data/markethub")
    os.environ.setdefault("MARKETHUB_DATA_ROOT", "/data/markethub/store")
    os.environ.setdefault("QUOTEMUX_RUNTIME_ROOT", "/data/markethub/runtime")
    os.environ.setdefault("QUOTEMUX_PACKAGE_REPO_SPEC", "/data/MarketHub2/current/QuoteMux_Packages")
    os.environ.setdefault("QUOTEMUX_PACKAGE_VENV_ROOT", f"/data/markethub/package_venvs/{os.getenv('MARKETHUB_RELEASE', '')}")
    for value in ("/data/MarketHub2/current/QuoteMux/src", "/data/MarketHub2/current/MarketHub/services/markethub_api/src"):
        if value not in sys.path:
            sys.path.insert(0, value)


def _health(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise RuntimeError(f"unhealthy MarketHub response: {payload}")
    return payload


def _text(value: object) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def _records(frame: Any) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for raw in frame.to_dict(orient="records"):
        output.append({str(key): None if pd.isna(value) else value.item() if hasattr(value, "item") else value for key, value in raw.items()})
    return output


def _provider_code(market: str, code: str) -> str:
    suffixes = {"SHSE": "SH", "SZSE": "SZ", "BJSE": "BJ"}
    if market not in suffixes:
        raise ValueError(f"unsupported market: {market}")
    return f"{code}.{suffixes[market]}"


def _source_numeric_zero(value: object) -> bool:
    text = str(value).strip()
    if text == "":
        return False
    try:
        return float(text) == 0.0
    except ValueError:
        return False


def _stored_suspension_volume_is_valid(value: object) -> bool:
    """A suspended placeholder may predate the zero-volume normalization."""
    if value is None:
        return True
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def _targets(audit_path: Path) -> tuple[dict[str, Any], dict[tuple[str, str], tuple[date, ...]]]:
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if audit.get("contract") != "markethub-stock-daily-all-a-audit-v1" or audit.get("scope") != "exhaustive":
        raise ValueError("unexpected source audit contract")
    grouped: dict[tuple[str, str], set[date]] = {}
    for row in audit.get("gaps", []):
        if (
            row.get("gap_kind") != "stored_suspended"
            or not bool(row.get("is_suspended"))
            or not _stored_suspension_volume_is_valid(row.get("volume"))
        ):
            raise ValueError(f"target is not an existing suspended placeholder: {row}")
        key = (str(row["market"]), str(row["code"]))
        grouped.setdefault(key, set()).add(date.fromisoformat(str(row["trade_date"])))
    targets = {key: tuple(sorted(values)) for key, values in grouped.items()}
    if sum(map(len, targets.values())) != int(audit.get("gap_rows", -1)):
        raise ValueError("source audit target accounting mismatch")
    return audit, targets


def probe(audit_path: Path, output_root: Path, health_url: str) -> dict[str, object]:
    _load_service_environment()
    from quotemux.settings import QuoteMuxSettings
    from quotemux.source_packages.instance_context import use_source_instance
    from quotemux_packages.tushare.rate_limit import call_tushare_api
    from quotemux_packages.tushare.source import get_ts_pro

    audit, targets = _targets(audit_path)
    before_health = _health(health_url)
    settings = QuoteMuxSettings()
    instances = settings.get_contract_source_instances("stocks.factors.adj", ("tushare",))
    instance = next((item for item in instances if item.package_id == "tushare"), None)
    if instance is None:
        raise RuntimeError("tushare source instance unavailable")
    raw: dict[str, dict[str, object]] = {}
    qualified: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    residuals: list[dict[str, object]] = []
    pending: list[dict[str, object]] = []
    with use_source_instance(instance):
        provider = get_ts_pro()
        if provider is None:
            raise RuntimeError("Tushare provider unavailable")
        for (market, code), dates in sorted(targets.items()):
            provider_code = _provider_code(market, code)
            kwargs = {"ts_code": provider_code, "start_date": min(dates).strftime("%Y%m%d"), "end_date": max(dates).strftime("%Y%m%d")}
            daily = _records(call_tushare_api("daily", provider.daily, **kwargs))
            suspensions = _records(call_tushare_api("suspend_d", provider.suspend_d, **kwargs))
            raw[f"{market}:{code}"] = {"provider_code": provider_code, "request": kwargs, "daily": daily, "suspend_d": suspensions}
            daily_by_date = {str(row.get("trade_date", "")): row for row in daily}
            suspension_by_date: dict[str, list[dict[str, object]]] = {}
            for row in suspensions:
                suspension_by_date.setdefault(_text(row.get("trade_date")), []).append(row)
            for target_date in dates:
                key = target_date.strftime("%Y%m%d")
                source_rows = [row for row in suspension_by_date.get(key, []) if _text(row.get("suspend_type")).upper() == "S" and _text(row.get("suspend_timing")) == ""]
                if key in daily_by_date:
                    residuals.append({"market": market, "code": code, "trade_date": target_date, "reason": "provider_has_daily_row"})
                elif len(source_rows) != 1:
                    pending.append({"market": market, "code": code, "trade_date": target_date, "reason": f"tushare_full_day_suspend_records={len(source_rows)}"})
                else:
                    qualified.append({"market": market, "code": code, "trade_date": target_date, "provider_code": provider_code, "source": SOURCE, "source_marker": SOURCE_MARKER, "source_record": source_rows[0]})
    baostock_raw: dict[str, object] = {}
    baostock_targets: dict[tuple[str, str], list[date]] = {}
    for row in pending:
        target_date = row["trade_date"]
        assert isinstance(target_date, date)
        if row["market"] == "BJSE" and target_date < BSE_INCEPTION:
            excluded.append({**row, "reason": "bjse_before_market_inception", "market_inception": BSE_INCEPTION})
        elif row["market"] in {"SHSE", "SZSE"}:
            baostock_targets.setdefault((str(row["market"]), str(row["code"])), []).append(target_date)
        else:
            residuals.append(row)
    if baostock_targets:
        import baostock as bs

        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock login failed: {login.error_code} {login.error_msg}")
        try:
            for (market, code), dates in sorted(baostock_targets.items()):
                prefix = "sh" if market == "SHSE" else "sz"
                result = bs.query_history_k_data_plus(
                    f"{prefix}.{code}", BAOSTOCK_FIELDS,
                    start_date=min(dates).isoformat(), end_date=max(dates).isoformat(), frequency="d", adjustflag="3",
                )
                values: list[list[str]] = []
                while result.error_code == "0" and result.next():
                    values.append(result.get_row_data())
                if result.error_code != "0":
                    raise RuntimeError(f"BaoStock query failed for {code}: {result.error_code} {result.error_msg}")
                records = [dict(zip(result.fields, value, strict=True)) for value in values]
                baostock_raw[f"{market}:{code}"] = {"fields": list(result.fields), "rows": values}
                by_date = {str(record.get("date", "")): record for record in records}
                for target_date in dates:
                    record = by_date.get(target_date.isoformat())
                    if (
                        record is not None
                        and str(record.get("code", "")) == f"{prefix}.{code}"
                        and str(record.get("adjustflag", "")) == "3"
                        and str(record.get("tradestatus", "")) == "0"
                        and _source_numeric_zero(record.get("volume"))
                        and _source_numeric_zero(record.get("amount"))
                    ):
                        qualified.append({"market": market, "code": code, "trade_date": target_date, "provider_code": f"{prefix}.{code}", "source": BAOSTOCK_SOURCE, "source_marker": BAOSTOCK_MARKER, "source_record": record})
                    else:
                        residuals.append({"market": market, "code": code, "trade_date": target_date, "reason": "baostock_not_full_day_suspended"})
        finally:
            bs.logout()
    after_health = _health(health_url)
    if (before_health.get("version"), before_health.get("data_version")) != (after_health.get("version"), after_health.get("data_version")):
        raise RuntimeError("live release/data version drifted during source probe")
    target_count = sum(map(len, targets.values()))
    accounted = len(qualified) + len(excluded) + len(residuals)
    qualified_keys = {(row["market"], row["code"], row["trade_date"]) for row in qualified}
    if accounted != target_count or len(qualified_keys) != len(qualified):
        raise RuntimeError(f"source accounting mismatch: target={target_count} accounted={accounted}")
    partial = output_root.with_name(output_root.name + ".partial")
    if output_root.exists() or partial.exists():
        raise FileExistsError(output_root if output_root.exists() else partial)
    partial.mkdir(parents=True)
    raw_path, normalized_path, residual_path, excluded_path = partial / "source_raw.json", partial / "qualified.json", partial / "residuals.json", partial / "excluded.json"
    _write_json(raw_path, {"captured_at_utc": datetime.now(timezone.utc), "tushare": {"source_instance_id": instance.instance_id, "provider_version": importlib.metadata.version("tushare"), "responses": raw}, "baostock": {"provider_version": importlib.metadata.version("baostock") if baostock_targets else None, "responses": baostock_raw}})
    _write_json(normalized_path, qualified)
    _write_json(residual_path, residuals)
    _write_json(excluded_path, excluded)
    manifest = {
        "contract": CONTRACT,
        "created_at_utc": datetime.now(timezone.utc),
        "release": before_health["version"],
        "target_data_version": before_health["data_version"],
        "sources": [SOURCE, BAOSTOCK_SOURCE],
        "source_audit": str(audit_path),
        "source_audit_sha256": _sha256(audit_path),
        "target_rows": target_count,
        "target_instruments": len(targets),
        "qualified_rows": len(qualified),
        "excluded_rows": len(excluded),
        "residual_rows": len(residuals),
        "residual_reasons": dict(Counter(str(row["reason"]) for row in residuals)),
        "files": {path.name: {"bytes": path.stat().st_size, "sha256": _sha256(path)} for path in (raw_path, normalized_path, residual_path, excluded_path)},
    }
    _write_json(partial / "manifest.json", manifest)
    partial.replace(output_root)
    return manifest


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(host=os.environ["MARKETHUB_DB_HOST"], port=int(os.environ["MARKETHUB_DB_PORT"]), dbname=os.environ["MARKETHUB_DB_NAME"], user=os.environ["MARKETHUB_DB_USER"], password=os.environ["MARKETHUB_DB_PASSWORD"], connect_timeout=10, application_name="markethub-stock-suspension-remediation", row_factory=dict_row)


def apply(root: Path, health_url: str, commit: bool) -> dict[str, object]:
    _load_service_environment()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("contract") != CONTRACT or int(manifest.get("residual_rows", -1)) != 0:
        raise ValueError("artifact is incomplete or has the wrong contract")
    if int(manifest.get("qualified_rows", -1)) + int(manifest.get("excluded_rows", -1)) != int(manifest.get("target_rows", -1)):
        raise ValueError("artifact target accounting mismatch")
    for name, expected in manifest["files"].items():
        path = root / name
        if not path.is_file() or path.stat().st_size != int(expected["bytes"]) or _sha256(path) != expected["sha256"]:
            raise ValueError(f"artifact hash mismatch: {name}")
    rows = json.loads((root / "qualified.json").read_text(encoding="utf-8"))
    if len(rows) != int(manifest["qualified_rows"]):
        raise ValueError("qualified row count mismatch")
    before_health = _health(health_url)
    if (before_health.get("version"), before_health.get("data_version")) != (manifest["release"], manifest["target_data_version"]):
        raise RuntimeError("live release/data version drifted from frozen source artifact")
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            insert into fact.stock_suspension_history
              (market,code,suspend_start_date,suspend_end_date,resume_date,status,source,source_marker,captured_at_utc,data_version,loaded_at)
            select x.market,x.code,x.trade_date,x.trade_date,null,'suspended',x.source,x.source_marker,%s::timestamptz,%s,now()
            from jsonb_to_recordset(%s::jsonb) as x(market text,code text,trade_date date,source text,source_marker text)
            where not exists (
                select 1 from fact.stock_suspension_history h
                where h.market=x.market and h.code=x.code and h.status='suspended'
                  and x.trade_date between h.suspend_start_date and h.suspend_end_date
            )
            order by x.market,x.code,x.trade_date
            """,
            (manifest["created_at_utc"], manifest["target_data_version"], json.dumps(rows, default=str)),
        )
        inserted = cursor.rowcount
        cursor.execute(
            """select count(*)::int as covered from jsonb_to_recordset(%s::jsonb) as x(market text,code text,trade_date date)
               where exists (select 1 from fact.stock_suspension_history h where h.market=x.market and h.code=x.code and h.status='suspended' and x.trade_date between h.suspend_start_date and h.suspend_end_date)""",
            (json.dumps(rows, default=str),),
        )
        covered = int(cursor.fetchone()["covered"])
        if covered != len(rows):
            raise RuntimeError(f"post-insert exact coverage mismatch: {covered}/{len(rows)}")
        if commit:
            connection.commit()
        else:
            connection.rollback()
    result = {"contract": CONTRACT, "mode": "apply" if commit else "dry-run", "artifact": str(root), "target_rows": len(rows), "inserted_rows": inserted, "covered_rows": covered, "before_health": before_health, "finished_at_utc": datetime.now(timezone.utc)}
    if commit:
        result["after_health"] = _health(health_url)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe_parser = subparsers.add_parser("probe")
    probe_parser.add_argument("--audit", type=Path, required=True)
    probe_parser.add_argument("--output-root", type=Path, required=True)
    probe_parser.add_argument("--health-url", default="http://127.0.0.1:8803/api/health")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--artifact", type=Path, required=True)
    apply_parser.add_argument("--health-url", default="http://127.0.0.1:8803/api/health")
    apply_parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    result = probe(args.audit, args.output_root, args.health_url) if args.command == "probe" else apply(args.artifact, args.health_url, args.commit)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
