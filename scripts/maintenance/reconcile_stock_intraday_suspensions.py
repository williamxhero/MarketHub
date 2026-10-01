"""Confirm zero-turnover daily rows against source-native full-day suspensions.

Only Tushare suspend_d=S with no daily bar can exclude a code-day from the
240-minute capture obligation. Ambiguous rows remain visible and fail the job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
import psycopg


SOURCE = "Tushare.suspend_d"
SOURCE_MARKER = "suspend_type_S_full_day_no_daily"
API = "http://127.0.0.1:8803"
TASK_CENTER = "http://127.0.0.1:8810"


def service_environment() -> None:
    for raw in Path("/data/markethub/env/markethub.env").read_text(encoding="utf-8").splitlines():
        if raw and not raw.startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            os.environ.setdefault(key, value)
    os.environ.setdefault("MARKETHUB_RUNTIME_ROOT", "/data/markethub")
    os.environ.setdefault("MARKETHUB_DATA_ROOT", "/data/markethub/store")
    os.environ.setdefault("QUOTEMUX_RUNTIME_ROOT", "/data/markethub/runtime")
    os.environ.setdefault("QUOTEMUX_PACKAGE_REPO_SPEC", "/data/MarketHub2/current/QuoteMux_Packages")
    os.environ.setdefault("QUOTEMUX_PACKAGE_VENV_ROOT", f"/data/markethub/package_venvs/{os.getenv('MARKETHUB_RELEASE', '')}")
    for path in ("/data/MarketHub2/current/QuoteMux/src", "/data/MarketHub2/current/MarketHub/services/markethub_api/src"):
        if path not in sys.path:
            sys.path.insert(0, path)


def api_json(base: str, path: str, *, post: bool = False) -> object:
    request = Request(f"{base}{path}", data=b"" if post else None, method="POST" if post else "GET")
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def db_connection() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"], port=os.environ["MARKETHUB_DB_PORT"],
        dbname=os.environ["MARKETHUB_DB_NAME"], user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"], connect_timeout=15,
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def records(frame: object) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for raw in frame.to_dict(orient="records"):
        output.append({str(key): None if pd.isna(value) else value.item() if hasattr(value, "item") else value
                       for key, value in raw.items()})
    return output


def candidates(connection: psycopg.Connection, target_date: str) -> list[tuple[str, str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """select market, btrim(code), trade_date::text
               from fact.stock_daily_1d
               where trade_date = coalesce(%s::date, (select max(trade_date) from fact.stock_daily_1d))
                 and volume = 0 and amount = 0 and is_suspended is false
               order by market, code""",
            (target_date or None,),
        )
        result = [(str(market), str(code), str(day)) for market, code, day in cursor.fetchall()]
    if len(result) > 100:
        raise RuntimeError(f"zero-turnover candidate count {len(result)} exceeds safety bound 100")
    return result


def source_probe(targets: list[tuple[str, str, str]]) -> tuple[list[dict], list[dict], list[dict]]:
    from quotemux.settings import QuoteMuxSettings
    from quotemux.source_packages.instance_context import use_source_instance
    from quotemux_packages.tushare.rate_limit import call_tushare_api
    from quotemux_packages.tushare.source import get_ts_pro

    instance = next((item for item in QuoteMuxSettings().get_contract_source_instances(
        "stocks.factors.adj", ("tushare",)) if item.package_id == "tushare"), None)
    if instance is None:
        raise RuntimeError("Tushare source instance unavailable")
    raw, qualified, residual = [], [], []
    suffix = {"SHSE": "SH", "SZSE": "SZ", "BJSE": "BJ"}
    with use_source_instance(instance):
        provider = get_ts_pro()
        if provider is None:
            raise RuntimeError("Tushare provider unavailable")
        for market, code, day in targets:
            key = {"market": market, "code": code, "trade_date": day}
            if market not in suffix:
                residual.append({**key, "reason": "unsupported_market"})
                continue
            source_code = f"{code}.{suffix[market]}"
            request = {"ts_code": source_code, "start_date": day.replace("-", ""),
                       "end_date": day.replace("-", "")}
            try:
                daily = records(call_tushare_api("daily", provider.daily, **request))
                suspension = records(call_tushare_api("suspend_d", provider.suspend_d, **request))
            except Exception as exc:
                residual.append({**key, "reason": f"source_error:{type(exc).__name__}:{exc}"[:300]})
                continue
            raw.append({**key, "request": request, "daily": daily, "suspend_d": suspension})
            verified = [row for row in suspension if str(row.get("ts_code", "")) == source_code
                        and str(row.get("trade_date", "")) == day.replace("-", "")
                        and str(row.get("suspend_type", "")).upper() == "S"
                        and row.get("suspend_timing") in (None, "")]
            if not daily and len(verified) == 1:
                qualified.append({**key, "source_code": source_code, "source_record": verified[0]})
            else:
                residual.append({**key, "reason": "full_day_suspension_not_confirmed",
                                 "daily_rows": len(daily), "suspension_rows": len(verified)})
    if len(qualified) + len(residual) != len(targets):
        raise RuntimeError("source probe target accounting mismatch")
    return raw, qualified, residual


def apply_qualified(connection: psycopg.Connection, qualified: list[dict], data_version: str,
                    captured_at: str) -> tuple[int, int]:
    inserted = updated = 0
    with connection.transaction():
        with connection.cursor() as cursor:
            for row in qualified:
                market, code, day = row["market"], row["code"], row["trade_date"]
                cursor.execute(
                    """insert into fact.stock_suspension_history
                         (market, code, suspend_start_date, suspend_end_date, resume_date,
                          status, source, source_marker, captured_at_utc, data_version, loaded_at)
                       select %s, %s, %s::date, %s::date, null, 'suspended', %s, %s,
                              %s::timestamptz, %s, now()
                       where not exists (
                           select 1 from fact.stock_suspension_history existing
                           where existing.market=%s and existing.code=%s and existing.status='suspended'
                             and %s::date between existing.suspend_start_date and existing.suspend_end_date
                       )""",
                    (market, code, day, day, SOURCE, SOURCE_MARKER, captured_at, data_version,
                     market, code, day),
                )
                inserted += cursor.rowcount
                cursor.execute(
                    """update fact.stock_daily_1d daily
                       set is_suspended = true
                       where daily.market=%s and daily.code=%s and daily.trade_date=%s::date
                         and daily.volume=0 and daily.amount=0 and daily.is_suspended is false
                         and not exists (
                             select 1 from fact.stock_bar_1m minute
                             where minute.market=daily.market
                               and minute.code=daily.code
                               and minute.bar_time >= %s::date
                               and minute.bar_time < %s::date + interval '1 day'
                         )""",
                    (market, code, day, day, day),
                )
                updated += cursor.rowcount
            for row in qualified:
                cursor.execute(
                    """select daily.is_suspended and exists (
                           select 1 from fact.stock_suspension_history history
                           where history.market=daily.market and history.code=daily.code
                             and history.status='suspended'
                             and daily.trade_date between history.suspend_start_date and history.suspend_end_date)
                       from fact.stock_daily_1d daily
                       where daily.market=%s and daily.code=%s and daily.trade_date=%s::date
                         and daily.volume=0 and daily.amount=0""",
                    (row["market"], row["code"], row["trade_date"]),
                )
                result = cursor.fetchone()
                if result != (True,):
                    raise RuntimeError(f"post-write suspension verification failed: {row['market']}:{row['code']}:{row['trade_date']}")
    return inserted, updated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-date", default="", help="default: latest daily fact date")
    parser.add_argument("--audit-root", type=Path, default=Path("/data/markethub/audit/stock-intraday-suspension-reconcile"))
    args = parser.parse_args()
    service_environment()
    tasks = api_json(TASK_CENTER, "/api/tasks")
    if not isinstance(tasks, list) or any(item.get("task_id") == "markethub_stock_intraday_capture"
                                      and item.get("status") == "running" for item in tasks):
        print("deferred: stock intraday capture is running", file=sys.stderr)
        return 75
    before = api_json(API, "/api/health")
    if not isinstance(before, dict) or before.get("status") != "ok":
        raise RuntimeError("MarketHub health is not ok")
    args.audit_root.mkdir(parents=True, exist_ok=True)
    run_dir = args.audit_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir.mkdir()
    with db_connection() as connection:
        targets = candidates(connection, args.target_date)
        connection.commit()
        raw, qualified, residual = source_probe(targets) if targets else ([], [], [])
        after_probe = api_json(API, "/api/health")
        if not isinstance(after_probe, dict) or (after_probe.get("version"), after_probe.get("data_version")) != (before.get("version"), before.get("data_version")):
            raise RuntimeError("MarketHub release/data version changed during suspension source probe")
        captured_at = datetime.now(timezone.utc).isoformat()
        raw_path = run_dir / "source_raw.json"
        raw_path.write_text(json.dumps({"captured_at_utc": captured_at, "source": SOURCE,
                                        "targets": targets, "responses": raw}, ensure_ascii=False,
                                       indent=2, default=str) + "\n", encoding="utf-8")
        qualified_path = run_dir / "qualified.json"
        qualified_path.write_text(json.dumps(qualified, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        residual_path = run_dir / "residual.json"
        residual_path.write_text(json.dumps(residual, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        manifest = {"captured_at_utc": captured_at, "source": SOURCE, "source_marker": SOURCE_MARKER,
                    "release": before.get("version"), "data_version_before": before.get("data_version"),
                    "target_count": len(targets), "qualified_count": len(qualified),
                    "residual_count": len(residual), "residual": residual,
                    "raw_sha256": sha256(raw_path), "qualified_sha256": sha256(qualified_path),
                    "residual_sha256": sha256(residual_path), "script_sha256": sha256(Path(__file__))}
        manifest_path = run_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        inserted, updated = apply_qualified(connection, qualified, str(before.get("data_version", "")), captured_at)
        manifest["history_rows_inserted"] = inserted
        manifest["daily_flags_corrected"] = updated
        manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit = api_json(API, "/api/admin/capture-gaps/audit?window_count=90", post=True)
    (run_dir / "capture_gap_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if qualified:
        with db_connection() as connection, connection.cursor() as cursor:
            for row in qualified:
                cursor.execute(
                    """select status from market_data_capture_gaps
                       where capability_id='stocks.quotes.intraday'
                         and code=%s and trade_date=%s::date""",
                    (row["code"], row["trade_date"]),
                )
                gap = cursor.fetchone()
                if gap is not None and gap[0] != "ineligible_suspended":
                    raise RuntimeError(f"capture gap not classified as source-confirmed suspension: {row['code']} {row['trade_date']} status={gap[0]}")
    print(json.dumps({"artifact": str(run_dir), "targets": len(targets), "qualified": len(qualified),
                      "residual": len(residual), "history_rows_inserted": inserted,
                      "daily_flags_corrected": updated}, ensure_ascii=False), flush=True)
    return 2 if residual else 0


if __name__ == "__main__":
    raise SystemExit(main())
