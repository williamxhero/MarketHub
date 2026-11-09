"""Confirm zero-turnover daily rows against source-native full-day suspensions.

Only source-native full-day suspension evidence can exclude a code-day from the
240-minute capture obligation. Tushare suspend_d=S remains preferred; when it
is unavailable, AKShare's Eastmoney suspension interval is used. Ambiguous
rows remain visible and fail the job.
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


Candidate = tuple[str, str, str, int, str]


TUSHARE_SOURCE = "Tushare.suspend_d"
TUSHARE_SOURCE_MARKER = "suspend_type_S_full_day_no_daily"
EASTMONEY_SOURCE = "Eastmoney.RPT_CUSTOM_SUSPEND_DATA_INTERFACE"
EASTMONEY_SOURCE_MARKER = "suspend_tfp_em_active_on_trade_date"
# Keep the historical names for callers that imported this maintenance script.
SOURCE = TUSHARE_SOURCE
SOURCE_MARKER = TUSHARE_SOURCE_MARKER
API = "http://127.0.0.1:8803"
TASK_CENTER = "http://127.0.0.1:8810"
# Some historical daily imports contain the smallest positive float instead of
# numeric zero for an absent turnover value.  Only source-confirmed full-day
# suspension can turn one of these candidates into a non-trading day.


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


def require_stable_daily_snapshot(before: object, after: object) -> None:
    """Allow unrelated minute writes while guarding the daily facts being reconciled."""
    if not isinstance(before, dict) or not isinstance(after, dict) or after.get("status") != "ok":
        raise RuntimeError("MarketHub health changed during suspension source probe")
    before_datasets, after_datasets = before.get("dataset_versions"), after.get("dataset_versions")
    if not isinstance(before_datasets, dict) or not isinstance(after_datasets, dict):
        raise RuntimeError("MarketHub daily dataset version unavailable during suspension source probe")
    before_daily = before_datasets.get("stock_daily_1d")
    after_daily = after_datasets.get("stock_daily_1d")
    if not before_daily or not after_daily or (before.get("version"), before_daily) != (after.get("version"), after_daily):
        raise RuntimeError("MarketHub release/stock daily dataset version changed during suspension source probe")


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


def _snapshot_shortfall_codes_tushare(target_date: str) -> tuple[str, ...]:
    from quotemux.infra.db.reference_reads import load_stock_active_codes_frame
    from quotemux.settings import QuoteMuxSettings
    from quotemux.source_packages.instance_context import use_source_instance
    from quotemux.source_packages.registry import get_default_source_package_registry
    from quotemux.stocks import _snapshot_complete_codes

    active_frame = load_stock_active_codes_frame(target_date)
    if active_frame.empty:
        return ()
    instance = next((item for item in QuoteMuxSettings().get_contract_source_instances(
        "stocks.quotes.daily_snapshot", ("tushare",)) if item.package_id == "tushare"), None)
    if instance is None:
        raise RuntimeError("Tushare snapshot source instance unavailable")
    with use_source_instance(instance):
        items = get_default_source_package_registry().get_handler(
            "tushare", "get_stock_daily_snapshot_full"
        )(target_date)
    active_codes, complete_codes = _snapshot_complete_codes(active_frame, items, target_date)
    return tuple(code for code in dict.fromkeys(active_codes) if code not in complete_codes)


def _date_text(value: object) -> str:
    """Normalize provider date values without inventing a timestamp."""
    if value is None:
        return ""
    text = str(value).strip().replace("/", "-")
    return text[:10] if len(text) >= 10 else text


def _eastmoney_active_codes(records: list[dict[str, object]], target_date: str) -> tuple[str, ...]:
    """Return codes covered by an Eastmoney full-day suspension interval."""
    active: set[str] = set()
    for row in records:
        code = str(row.get("代码", "")).strip().zfill(6)
        start = _date_text(row.get("停牌时间"))
        end = _date_text(row.get("停牌截止时间"))
        if code and start and start <= target_date and (not end or target_date <= end):
            active.add(code)
    return tuple(sorted(active))


def _eastmoney_identity_matches(row: dict[str, object], market: str, code: str) -> bool:
    expected_suffix = {"SHSE": "SH", "SZSE": "SZ", "BJSE": "BJ"}.get(market, "")
    if not expected_suffix or str(row.get("代码", "")).strip().zfill(6) != code:
        return False
    secucode = str(row.get("SECUCODE", "")).strip().upper()
    if secucode in {"", "NONE", "NAN", "NAT"}:
        secucode = ""
    if secucode:
        return secucode == f"{code}.{expected_suffix}"
    market_text = str(row.get("所属市场", ""))
    if "上交所" in market_text:
        provider_suffix = "SH"
    elif "深交所" in market_text:
        provider_suffix = "SZ"
    elif "北交所" in market_text or "北京" in market_text:
        provider_suffix = "BJ"
    else:
        provider_suffix = ""
    return provider_suffix == expected_suffix


def _snapshot_shortfall_codes_eastmoney(target_date: str) -> tuple[str, ...]:
    from quotemux.settings import QuoteMuxSettings
    from quotemux.source_packages.instance_context import use_source_instance
    from quotemux_packages.akshare import source as ak_source

    instance = next((item for item in QuoteMuxSettings().get_contract_source_instances(
        "stocks.quotes.daily_snapshot", ("akshare",)
    ) if item.package_id == "akshare"), None)
    if instance is None:
        raise RuntimeError("AKShare snapshot source instance unavailable")
    date_arg = target_date.replace("-", "")
    with use_source_instance(instance):
        frame = ak_source._call_ak("stock_tfp_em", ak_source.ak.stock_tfp_em, date=date_arg)
    return _eastmoney_active_codes(records(frame), target_date)


def _snapshot_shortfall_codes(target_date: str) -> tuple[str, ...]:
    try:
        return _snapshot_shortfall_codes_tushare(target_date)
    except Exception as tushare_error:
        try:
            return _snapshot_shortfall_codes_eastmoney(target_date)
        except Exception as eastmoney_error:
            raise RuntimeError(
                "Tushare snapshot unavailable and Eastmoney suspension fallback failed: "
                f"tushare={type(tushare_error).__name__}:{tushare_error}; "
                f"eastmoney={type(eastmoney_error).__name__}:{eastmoney_error}"
            ) from eastmoney_error


def candidates(
    connection: psycopg.Connection,
    target_date: str,
    *,
    include_snapshot_shortfall: bool = False,
) -> list[Candidate]:
    if include_snapshot_shortfall and not target_date:
        raise ValueError("--target-date is required with --include-snapshot-shortfall")
    with connection.cursor() as cursor:
        cursor.execute(
            """select daily.market, btrim(daily.code), daily.trade_date::text,
                      coalesce(coverage.row_count, 0)
               from fact.stock_daily_1d daily
               left join readmodel.stock_bar_1m_daily_coverage coverage
                 on coverage.market=daily.market and coverage.code=daily.code
                and coverage.trade_date=daily.trade_date
               where daily.trade_date = coalesce(%s::date, (select max(trade_date) from fact.stock_daily_1d))
                 and daily.volume = 0
                 and daily.amount is not null and abs(daily.amount) <= 1e-12
                 and daily.is_suspended is false
               order by daily.market, daily.code""",
            (target_date or None,),
        )
        result = [
            (str(market), str(code), str(day), int(bars), "daily_row")
            for market, code, day, bars in cursor.fetchall()
        ]
        if include_snapshot_shortfall:
            shortfall_codes = _snapshot_shortfall_codes(target_date)
            if shortfall_codes:
                cursor.execute(
                    """select stock.market, btrim(stock.code), %s::date
                       from ref.stock stock
                       where stock.code = any(%s::text[])
                         and stock.listed_date <= %s::date
                         and (stock.delisted_date is null or stock.delisted_date > %s::date)
                         and stock.identity_status = 'authoritative'
                         and not (stock.market = 'SHSE' and left(stock.code, 3) = '900')
                         and not (stock.market = 'SZSE' and left(stock.code, 3) = '200')
                         and not exists (
                             select 1 from fact.stock_daily_1d daily
                             where daily.market=stock.market and daily.code=stock.code
                               and daily.trade_date=%s::date
                         )
                       order by stock.market, stock.code""",
                    (target_date, list(shortfall_codes), target_date, target_date, target_date),
                )
                result.extend(
                    (str(market), str(code), str(day), 0, "snapshot_shortfall_no_daily")
                    for market, code, day in cursor.fetchall()
                )
    if len(result) > 100:
        raise RuntimeError(f"suspension candidate count {len(result)} exceeds safety bound 100")
    return result


def _source_probe_tushare(targets: list[Candidate]) -> tuple[list[dict], list[dict], list[dict]]:
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
        for market, code, day, existing_bars, candidate_kind in targets:
            key = {"market": market, "code": code, "trade_date": day, "candidate_kind": candidate_kind}
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
            if not daily and len(verified) == 1 and existing_bars == 0:
                qualified.append({**key, "source": TUSHARE_SOURCE, "source_marker": TUSHARE_SOURCE_MARKER,
                                  "source_code": source_code, "source_record": verified[0]})
            elif not daily and len(verified) == 1 and existing_bars > 0:
                residual.append({**key, "reason": "source_confirmed_suspension_conflicts_with_existing_1m",
                                 "existing_1m_bars": existing_bars})
            else:
                residual.append({**key, "reason": "full_day_suspension_not_confirmed",
                                 "daily_rows": len(daily), "suspension_rows": len(verified)})
    if len(qualified) + len(residual) != len(targets):
        raise RuntimeError("source probe target accounting mismatch")
    return raw, qualified, residual


def _source_probe_eastmoney(targets: list[Candidate]) -> tuple[list[dict], list[dict], list[dict]]:
    from quotemux.settings import QuoteMuxSettings
    from quotemux.source_packages.instance_context import use_source_instance
    from quotemux_packages.akshare import source as ak_source

    instance = next((item for item in QuoteMuxSettings().get_contract_source_instances(
        "stocks.quotes.daily_snapshot", ("akshare",)
    ) if item.package_id == "akshare"), None)
    if instance is None:
        raise RuntimeError("AKShare snapshot source instance unavailable")
    dates = sorted({str(row[2]) for row in targets})
    raw: list[dict] = []
    qualified: list[dict] = []
    residual: list[dict] = []
    with use_source_instance(instance):
        for target_date in dates:
            date_arg = target_date.replace("-", "")
            frame = ak_source._call_ak("stock_tfp_em", ak_source.ak.stock_tfp_em, date=date_arg)
            source_rows = records(frame)
            raw.append({"source": EASTMONEY_SOURCE, "target_date": target_date,
                        "request": {"date": date_arg}, "rows": source_rows})
            active_by_code: dict[str, list[dict[str, object]]] = {}
            for source_row in source_rows:
                for code in _eastmoney_active_codes([source_row], target_date):
                    active_by_code.setdefault(code, []).append(source_row)
            for market, code, day, existing_bars, candidate_kind in targets:
                if day != target_date:
                    continue
                key = {"market": market, "code": code, "trade_date": day,
                       "candidate_kind": candidate_kind}
                verified = [row for row in active_by_code.get(code, [])
                            if _eastmoney_identity_matches(row, market, code)]
                if existing_bars == 0 and len(verified) == 1:
                    qualified.append({**key, "source": EASTMONEY_SOURCE,
                                      "source_marker": EASTMONEY_SOURCE_MARKER,
                                      "source_code": verified[0].get("SECUCODE", code),
                                      "source_record": verified[0]})
                else:
                    residual.append({**key, "reason": "eastmoney_full_day_suspension_not_confirmed",
                                     "suspension_rows": len(verified),
                                     "existing_1m_bars": existing_bars})
    if len(qualified) + len(residual) != len(targets):
        raise RuntimeError("Eastmoney source probe target accounting mismatch")
    return raw, qualified, residual


def source_probe(targets: list[Candidate]) -> tuple[list[dict], list[dict], list[dict]]:
    try:
        tushare_raw, tushare_qualified, tushare_residual = _source_probe_tushare(targets)
    except Exception as tushare_error:
        raw, qualified, residual = _source_probe_eastmoney(targets)
        raw.insert(0, {"source": TUSHARE_SOURCE, "error": f"{type(tushare_error).__name__}: {tushare_error}",
                       "fallback": EASTMONEY_SOURCE})
        return raw, qualified, residual
    source_errors = [row for row in tushare_residual
                     if str(row.get("reason", "")).startswith("source_error:")]
    if not source_errors:
        return tushare_raw, tushare_qualified, tushare_residual
    eastmoney_raw, eastmoney_qualified, eastmoney_residual = _source_probe_eastmoney(targets)
    fallback_record = {
        "source": TUSHARE_SOURCE,
        "fallback": EASTMONEY_SOURCE,
        "errors": source_errors,
    }
    return [fallback_record, *tushare_raw, *eastmoney_raw], eastmoney_qualified, eastmoney_residual


def apply_qualified(connection: psycopg.Connection, qualified: list[dict], data_version: str,
                    captured_at: str) -> tuple[int, int, int]:
    inserted = updated = ineligible = 0
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_xact_lock(hashtext('markethub:stock-fact-publication'))"
            )
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
                    (market, code, day, day, row.get("source", SOURCE), row.get("source_marker", SOURCE_MARKER), captured_at, data_version,
                     market, code, day),
                )
                inserted += cursor.rowcount
                if row["candidate_kind"] == "daily_row":
                    cursor.execute(
                        """update fact.stock_daily_1d daily
                           set is_suspended = true, amount = 0
                           where daily.market=%s and daily.code=%s and daily.trade_date=%s::date
                             and daily.volume=0
                             and daily.amount is not null and abs(daily.amount) <= 1e-12
                             and daily.is_suspended is false
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
                    cursor.execute(
                        """update market_data_capture_gaps gaps
                           set status='ineligible_suspended', last_seen_at=now(), resolved_at=null,
                               last_error='source-confirmed full-day suspension; 1m bars not expected'
                           from fact.stock_daily_1d daily
                           where gaps.capability_id='stocks.quotes.intraday'
                             and gaps.code=%s and gaps.trade_date=%s::date
                             and gaps.status not in ('resolved', 'ineligible_suspended')
                             and daily.market=%s and daily.code=gaps.code
                             and daily.trade_date=gaps.trade_date
                             and daily.is_suspended is true
                             and daily.volume=0
                             and daily.amount = 0
                             and exists (
                                 select 1 from fact.stock_suspension_history history
                                 where history.market=daily.market and history.code=daily.code
                                   and history.status='suspended'
                                   and daily.trade_date between history.suspend_start_date and history.suspend_end_date
                             )""",
                        (code, day, market),
                    )
                    ineligible += cursor.rowcount
            for row in qualified:
                if row["candidate_kind"] != "daily_row":
                    continue
                cursor.execute(
                    """select daily.is_suspended and exists (
                           select 1 from fact.stock_suspension_history history
                           where history.market=daily.market and history.code=daily.code
                             and history.status='suspended'
                             and daily.trade_date between history.suspend_start_date and history.suspend_end_date)
                       from fact.stock_daily_1d daily
                       where daily.market=%s and daily.code=%s and daily.trade_date=%s::date
                         and daily.volume=0
                         and daily.amount = 0""",
                    (row["market"], row["code"], row["trade_date"]),
                )
                result = cursor.fetchone()
                if result != (True,):
                    raise RuntimeError(f"post-write suspension verification failed: {row['market']}:{row['code']}:{row['trade_date']}")
    return inserted, updated, ineligible


def apply_if_requested(connection: psycopg.Connection, qualified: list[dict], data_version: str,
                       captured_at: str, *, dry_run: bool) -> tuple[int, int, int]:
    if dry_run:
        return 0, 0, 0
    return apply_qualified(connection, qualified, data_version, captured_at)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-date", default="", help="default: latest daily fact date")
    parser.add_argument(
        "--include-snapshot-shortfall",
        action="store_true",
        help="include active codes absent from the daily fact and QuoteMux snapshot",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="probe providers and write evidence artifacts without database writes",
    )
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
        targets = candidates(
            connection,
            args.target_date,
            include_snapshot_shortfall=args.include_snapshot_shortfall,
        )
        connection.commit()
        raw, qualified, residual = source_probe(targets) if targets else ([], [], [])
        after_probe = api_json(API, "/api/health")
        require_stable_daily_snapshot(before, after_probe)
        captured_at = datetime.now(timezone.utc).isoformat()
        raw_path = run_dir / "source_raw.json"
        source_names = sorted({str(item["source"]) for item in [*raw, *qualified] if item.get("source")})
        source_markers = sorted({str(item["source_marker"]) for item in qualified if item.get("source_marker")})
        raw_path.write_text(json.dumps({"captured_at_utc": captured_at, "sources": source_names,
                                        "targets": targets, "responses": raw}, ensure_ascii=False,
                                       indent=2, default=str) + "\n", encoding="utf-8")
        qualified_path = run_dir / "qualified.json"
        qualified_path.write_text(json.dumps(qualified, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        residual_path = run_dir / "residual.json"
        residual_path.write_text(json.dumps(residual, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        qualified_by_kind = {
            kind: sum(1 for row in qualified if row.get("candidate_kind") == kind)
            for kind in ("daily_row", "snapshot_shortfall_no_daily")
        }
        manifest = {"captured_at_utc": captured_at, "dry_run": args.dry_run,
                    "source": source_names[0] if len(source_names) == 1 else "mixed",
                    "source_marker": source_markers[0] if len(source_markers) == 1 else "mixed",
                    "sources": source_names, "source_markers": source_markers,
                    "release": before.get("version"), "data_version_before": before.get("data_version"),
                    "data_version_after_probe": after_probe.get("data_version"),
                    "stock_daily_dataset_version": before["dataset_versions"]["stock_daily_1d"],
                    "include_snapshot_shortfall": args.include_snapshot_shortfall,
                    "target_count": len(targets), "qualified_count": len(qualified),
                    "qualified_by_kind": qualified_by_kind,
                    "residual_count": len(residual), "residual": residual,
                    "raw_sha256": sha256(raw_path), "qualified_sha256": sha256(qualified_path),
                    "residual_sha256": sha256(residual_path), "script_sha256": sha256(Path(__file__))}
        manifest_path = run_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        inserted, updated, ineligible = apply_if_requested(
            connection, qualified, str(after_probe.get("data_version", "")), captured_at,
            dry_run=args.dry_run,
        )
        manifest["history_rows_inserted"] = inserted
        manifest["daily_flags_corrected"] = updated
        manifest["capture_gaps_ineligible"] = ineligible
        manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not args.dry_run:
        audit = api_json(API, "/api/admin/capture-gaps/audit?window_count=90", post=True)
        (run_dir / "capture_gap_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if qualified and not args.dry_run:
        with db_connection() as connection, connection.cursor() as cursor:
            for row in qualified:
                if row["candidate_kind"] != "daily_row":
                    continue
                cursor.execute(
                    """select status from market_data_capture_gaps
                       where capability_id='stocks.quotes.intraday'
                         and code=%s and trade_date=%s::date""",
                    (row["code"], row["trade_date"]),
                )
                gap = cursor.fetchone()
                if gap is not None and gap[0] != "ineligible_suspended":
                    raise RuntimeError(f"capture gap not classified as source-confirmed suspension: {row['code']} {row['trade_date']} status={gap[0]}")
    print(json.dumps({"artifact": str(run_dir), "dry_run": args.dry_run,
                      "targets": len(targets), "qualified": len(qualified),
                      "qualified_by_kind": {kind: sum(1 for row in qualified if row.get("candidate_kind") == kind)
                                            for kind in ("daily_row", "snapshot_shortfall_no_daily")},
                      "residual": len(residual), "history_rows_inserted": inserted,
                      "daily_flags_corrected": updated,
                      "capture_gaps_ineligible": ineligible}, ensure_ascii=False), flush=True)
    return 2 if residual else 0


if __name__ == "__main__":
    raise SystemExit(main())
