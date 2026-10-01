"""Bounded, resumable repair of audited stock 1m capture gaps on yosef-server.

The scheduler runs this outside daily capture windows. A repair is counted as
successful only when the API reports every requested 240-bar day as covered.
The original gap rows and provider evidence remain in PostgreSQL.
"""

from __future__ import annotations

import argparse
import atexit
import fcntl
import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import psycopg


CAPABILITY = "stocks.quotes.intraday"
DATASET = "stock_bar_1m"
MAINTENANCE_LOCK = Path("/data/markethub/audit/stock-bar-maintenance.lock")


def acquire_maintenance_lock() -> bool:
    MAINTENANCE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = MAINTENANCE_LOCK.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    atexit.register(handle.close)
    return True


def request_json(base_url: str, path: str, *, payload: dict | None = None) -> object:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=body,
        method="POST" if body is not None else "GET",
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urlopen(request, timeout=300 if body is not None else 20) as response:
        return json.load(response)


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def record_event(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def task_center_busy(task_url: str) -> bool:
    tasks = request_json(task_url, "/api/tasks")
    if not isinstance(tasks, list):
        raise RuntimeError("Task Center returned a non-list task inventory")
    return any(
        item.get("group_name") == "MARKETHUB"
        and item.get("status") == "running"
        and not item.get("is_deleted", False)
        for item in tasks
    )


def capture_busy(api_url: str) -> bool:
    runs = request_json(api_url, "/api/admin/capture-runs?status=running&limit=500")
    if not isinstance(runs, list):
        raise RuntimeError("MarketHub returned a non-list capture run inventory")
    return any(item.get("capability_id") == CAPABILITY for item in runs)


def db_connection() -> psycopg.Connection:
    values = {
        "host": os.getenv("MARKETHUB_DB_HOST") or os.getenv("QUOTEMUX_CACHE_DB_HOST"),
        "port": os.getenv("MARKETHUB_DB_PORT") or os.getenv("QUOTEMUX_CACHE_DB_PORT"),
        "dbname": os.getenv("MARKETHUB_DB_NAME") or os.getenv("QUOTEMUX_CACHE_DB_NAME"),
        "user": os.getenv("MARKETHUB_DB_USER") or os.getenv("QUOTEMUX_CACHE_DB_USER"),
        "password": os.getenv("MARKETHUB_DB_PASSWORD") or os.getenv("QUOTEMUX_CACHE_DB_PASSWORD"),
    }
    if any(not value for value in values.values()):
        raise RuntimeError("production database configuration is incomplete")
    return psycopg.connect(**values, connect_timeout=15)


def unresolved_gaps() -> list[dict]:
    """Read the full durable queue; the API's 5000-row page is not exhaustive."""
    with db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("set transaction read only")
            cursor.execute(
                """select market, btrim(code), trade_date::text, status
                   from market_data_capture_gaps
                   where capability_id = %s and status not in ('resolved', 'ineligible_suspended')
                   order by trade_date desc, market, code""",
                (CAPABILITY,),
            )
            return [
                {"market": market, "code": code, "trade_date": trade_date, "status": status}
                for market, code, trade_date, status in cursor.fetchall()
            ]


def batch_statuses(trade_date: str, codes: list[str]) -> dict[str, str]:
    with db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("set transaction read only")
            cursor.execute(
                """select market, btrim(code), status
                   from market_data_capture_gaps
                   where capability_id = %s and trade_date = %s::date
                     and code = any(%s)""",
                (CAPABILITY, trade_date, codes),
            )
            return {f"{trade_date}|{market}|{code}": status for market, code, status in cursor.fetchall()}


def gap_key(gap: dict) -> str:
    return f"{gap['trade_date']}|{gap['market']}|{gap['code']}"


def select_batch(gaps: list[dict], attempts: dict[str, int], size: int) -> list[dict]:
    eligible = [
        gap for gap in gaps
        if gap.get("status") != "resolved"
        and attempts.get(gap_key(gap), 0) < 2
    ]
    if not eligible:
        return []
    target_date = str(eligible[0]["trade_date"])
    return [gap for gap in eligible if str(gap["trade_date"]) == target_date][:size]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8803")
    parser.add_argument("--task-url", default="http://127.0.0.1:8810")
    parser.add_argument("--audit-dir", type=Path, default=Path("/data/markethub/audit/stock-intraday-gap-backfill"))
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--max-batches", type=int, default=500)
    parser.add_argument("--stop-at-utc", default="15:40")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 120 or not 1 <= args.max_batches <= 10000:
        parser.error("batch-size must be 1..120 and max-batches 1..10000")
    if not acquire_maintenance_lock():
        print("deferred: stock-bar maintenance is already running", file=sys.stderr)
        return 75
    stop_time = time.fromisoformat(args.stop_at_utc)
    stop_at = datetime.combine(datetime.now(timezone.utc).date(), stop_time, timezone.utc)
    args.audit_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.audit_dir / "state.json"
    events_path = args.audit_dir / "events.jsonl"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
        "started_at": datetime.now(timezone.utc).isoformat(), "attempts": {}, "total_batches": 0
    }
    attempts = state.setdefault("attempts", {})  # lifetime audit, not a permanent retry ban
    run_attempts: dict[str, int] = {}
    start_health = request_json(args.api_url, "/api/health")
    if not isinstance(start_health, dict) or start_health.get("status") != "ok":
        raise RuntimeError("MarketHub health is not ok")
    release = str(start_health.get("version", ""))
    if not release:
        raise RuntimeError("MarketHub release identity is absent")
    state["release"] = release
    state["last_started_at"] = datetime.now(timezone.utc).isoformat()
    gaps = unresolved_gaps()
    state["unresolved_at_start"] = len(gaps)
    save_state(state_path, state)

    for _ in range(args.max_batches):
        # A request can consume its full 300-second bound. Stop before entering
        # the next scheduled global-update window, never in the middle of it.
        if datetime.now(timezone.utc) + timedelta(seconds=330) >= stop_at:
            state["last_stop_reason"] = "time_window"
            break
        current_health = request_json(args.api_url, "/api/health")
        if not isinstance(current_health, dict) or current_health.get("version") != release:
            raise RuntimeError("MarketHub release changed during historical backfill")
        if task_center_busy(args.task_url) or capture_busy(args.api_url):
            state["last_stop_reason"] = "daily_capture_or_task_running"
            break
        batch = select_batch(gaps, run_attempts, args.batch_size)
        if not batch:
            state["last_stop_reason"] = "all_resolved" if not gaps else "per_run_retry_limit"
            break
        trade_date = str(batch[0]["trade_date"])
        codes = [str(gap["code"]) for gap in batch]
        keys = [gap_key(gap) for gap in batch]
        event = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "release": release,
            "data_version_before": current_health.get("data_version"),
            "trade_date": trade_date,
            "codes": codes,
            "markets": [str(gap["market"]) for gap in batch],
        }
        scope = {
            "codes": codes, "freq": "1m", "adjust": "none",
            "start_time": f"{trade_date} 09:31:00",
            "end_time": f"{trade_date} 15:00:00",
        }
        try:
            result = request_json(args.api_url, "/api/admin/data-repairs", payload={"dataset_id": DATASET, "scope": scope})
            if not isinstance(result, dict):
                raise RuntimeError("MarketHub returned a non-object repair result")
            expected = 240 * len(batch)
            event.update({
                "repair_task_id": result.get("repair_task_id"),
                "status": result.get("status"),
                "row_count": result.get("row_count"),
                "coverage_count": result.get("coverage_count"),
                "expected_count": expected,
                "error_message": result.get("error_message", ""),
            })
            event["verified_by_result"] = (
                result.get("status") == "success"
                and result.get("row_count") == expected
                and result.get("coverage_count") == expected
            )
            statuses = batch_statuses(trade_date, codes)
            resolved = {key for key in keys if statuses.get(key) == "resolved"}
            event["resolved_code_days"] = len(resolved)
            event["verified_by_result"] = event["verified_by_result"] and len(resolved) == len(keys)
            gaps = [gap for gap in gaps if gap_key(gap) not in resolved]
            for key in keys:
                if key not in resolved:
                    run_attempts[key] = run_attempts.get(key, 0) + 1
                    attempts[key] = attempts.get(key, 0) + 1
        except Exception as exc:
            event.update({"status": "exception", "error": f"{type(exc).__name__}: {exc}"[:1000], "verified_by_result": False})
            for key in keys:
                run_attempts[key] = run_attempts.get(key, 0) + 1
                attempts[key] = attempts.get(key, 0) + 1
        state["total_batches"] = int(state.get("total_batches", 0)) + 1
        state["last_event"] = {key: event[key] for key in ("started_at", "trade_date", "status", "verified_by_result")}
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        record_event(events_path, event)
        save_state(state_path, state)
    else:
        state["last_stop_reason"] = "max_batches"
    state["last_finished_at"] = datetime.now(timezone.utc).isoformat()
    state["unresolved_after"] = len(unresolved_gaps())
    save_state(state_path, state)
    print(json.dumps({
        "release": release,
        "total_batches": state["total_batches"],
        "last_stop_reason": state["last_stop_reason"],
        "unresolved_at_start": state["unresolved_at_start"],
        "unresolved_after": state["unresolved_after"],
        "last_event": state.get("last_event"),
    }))
    return 0 if state["unresolved_after"] == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"stock intraday backfill failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
