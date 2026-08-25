from __future__ import annotations

from datetime import datetime
import os
import threading
import uuid

import pandas as pd
from psycopg.types.json import Jsonb
from quotemux.infra.common import format_date_value
from quotemux.infra.db.market_reads import list_stock_codes_with_daily_data
from quotemux.local_daily import get_stock_codes_missing_adj_factors
from quotemux.store.cache_db import execute_many, execute_sql, query_dataframe
from services import stocks


TASK_QUEUED = "queued"
TASK_RUNNING = "running"
TASK_SUCCESS = "success"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
ITEM_QUEUED = "queued"
ITEM_RUNNING = "running"
ITEM_SUCCESS = "success"
ITEM_FAILED = "failed"
ITEM_CANCELLED = "cancelled"
FINISHED_ITEM_STATUSES = (ITEM_SUCCESS, ITEM_FAILED, ITEM_CANCELLED)
ACTIVE_TASK_STATUSES = (TASK_QUEUED, TASK_RUNNING)

_SCHEMA_SQL = (
    "create table if not exists admin_adj_factor_warmup_tasks (task_id text primary key, status text not null, start_date date not null, end_date date not null, base_date date not null, created_at timestamp without time zone not null default now(), started_at timestamp without time zone, finished_at timestamp without time zone, error_message text not null default '')",
    "create table if not exists admin_adj_factor_warmup_items (task_id text not null references admin_adj_factor_warmup_tasks(task_id) on delete cascade, position integer not null, code text not null, status text not null, factor_count integer not null default 0, started_at timestamp without time zone, finished_at timestamp without time zone, error_message text not null default '', detail_json jsonb not null default '{}'::jsonb, primary key (task_id, position))",
    "create index if not exists idx_admin_adj_factor_warmup_tasks_created_at on admin_adj_factor_warmup_tasks (created_at desc)",
    "create index if not exists idx_admin_adj_factor_warmup_tasks_status on admin_adj_factor_warmup_tasks (status, created_at desc)",
    "create index if not exists idx_admin_adj_factor_warmup_items_task_position on admin_adj_factor_warmup_items (task_id, position)",
)

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False
_STOP_REQUESTED = threading.Event()
_WORKERS: dict[str, threading.Thread] = {}
_WORKERS_LOCK = threading.Lock()


def _fetch_complete_factor_window(code: str, start_date: str, end_date: str, base_date: str) -> list[object]:
    items = stocks.get_adj_factors(code, start_date, end_date, base_date)
    if code in get_stock_codes_missing_adj_factors([code], start_date, end_date):
        raise RuntimeError("权威源返回后窗口内仍有正常交易日缺少复权因子")
    return items


def _serialize_time(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _ensure_schema() -> bool:
    global _SCHEMA_READY
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return True
        for statement in _SCHEMA_SQL:
            if not execute_sql(statement):
                return False
        _SCHEMA_READY = True
        return True


def _task_rows(limit: int) -> list[dict[str, object]]:
    if not _ensure_schema():
        return []
    frame = query_dataframe(
        "select task_id, status, start_date, end_date, base_date, created_at, started_at, finished_at, error_message from admin_adj_factor_warmup_tasks order by created_at desc limit %s",
        (max(1, min(limit, 200)),),
    )
    return [] if frame.empty else [dict(row) for row in frame.to_dict("records")]


def _item_rows(task_id: str) -> list[dict[str, object]]:
    frame = query_dataframe(
        "select task_id, position, code, status, factor_count, started_at, finished_at, error_message, detail_json from admin_adj_factor_warmup_items where task_id = %s order by position",
        (task_id,),
    )
    return [] if frame.empty else [dict(row) for row in frame.to_dict("records")]


def _task_payload(task_row: dict[str, object], item_rows: list[dict[str, object]]) -> dict[str, object]:
    finished_count = sum(str(row["status"]) in FINISHED_ITEM_STATUSES for row in item_rows)
    current_code = next((str(row["code"]) for row in item_rows if str(row["status"]) == ITEM_RUNNING), "")
    if current_code == "":
        current_code = next((str(row["code"]) for row in item_rows if str(row["status"]) == ITEM_QUEUED), "")
    return {
        "task_id": str(task_row["task_id"]),
        "status": str(task_row["status"]),
        "start_date": format_date_value(task_row["start_date"]),
        "end_date": format_date_value(task_row["end_date"]),
        "base_date": format_date_value(task_row["base_date"]),
        "created_at": _serialize_time(task_row.get("created_at")),
        "started_at": _serialize_time(task_row.get("started_at")),
        "finished_at": _serialize_time(task_row.get("finished_at")),
        "total_count": len(item_rows),
        "finished_count": finished_count,
        "success_count": sum(str(row["status"]) == ITEM_SUCCESS for row in item_rows),
        "failed_count": sum(str(row["status"]) == ITEM_FAILED for row in item_rows),
        "cancelled_count": sum(str(row["status"]) == ITEM_CANCELLED for row in item_rows),
        "current_code": current_code,
        "error_message": str(task_row.get("error_message", "")),
    }


def _item_payload(row: dict[str, object]) -> dict[str, object]:
    detail_json = row.get("detail_json") if isinstance(row.get("detail_json"), dict) else {}
    return {
        "position": int(row["position"]),
        "code": str(row["code"]),
        "status": str(row["status"]),
        "factor_count": int(row["factor_count"]),
        "started_at": _serialize_time(row.get("started_at")),
        "finished_at": _serialize_time(row.get("finished_at")),
        "error_message": str(row.get("error_message", "")),
        "detail_json": detail_json,
    }


def list_tasks(limit: int = 50) -> list[dict[str, object]]:
    return [_task_payload(row, _item_rows(str(row["task_id"]))) for row in _task_rows(limit)]


def get_task(task_id: str) -> dict[str, object]:
    rows = [row for row in _task_rows(200) if str(row["task_id"]) == task_id]
    if rows == []:
        raise KeyError(f"未知复权因子预热任务: {task_id}")
    payload = _task_payload(rows[0], _item_rows(task_id))
    payload["items"] = [_item_payload(row) for row in _item_rows(task_id)]
    return payload


def create_task(start_date: str, end_date: str, base_date: str) -> dict[str, object]:
    actual_start = format_date_value(start_date)
    actual_end = format_date_value(end_date)
    actual_base = format_date_value(base_date)
    if actual_start == "" or actual_end == "" or actual_base == "" or actual_start > actual_end:
        raise ValueError("预热日期范围或冻结基准日不合法")
    configured_base = format_date_value(os.getenv("QUOTEMUX_ADJUSTMENT_BASE_DATE", ""))
    if configured_base == "" or configured_base != actual_base:
        raise ValueError("冻结基准日必须等于 QUOTEMUX_ADJUSTMENT_BASE_DATE")
    if not _ensure_schema():
        raise RuntimeError("复权因子预热表初始化失败")
    active_rows = query_dataframe(
        "select task_id from admin_adj_factor_warmup_tasks where status in (%s, %s) limit 1",
        ACTIVE_TASK_STATUSES,
    )
    if not active_rows.empty:
        raise RuntimeError(f"已有复权因子预热任务正在执行: {active_rows.iloc[0]['task_id']}")
    codes = list_stock_codes_with_daily_data(actual_start, actual_end)
    if codes == []:
        raise RuntimeError("指定窗口没有可预热的股票日线")
    task_id = str(uuid.uuid4())
    if not execute_sql(
        "insert into admin_adj_factor_warmup_tasks (task_id, status, start_date, end_date, base_date) values (%s, %s, %s::date, %s::date, %s::date)",
        (task_id, TASK_QUEUED, actual_start, actual_end, actual_base),
    ):
        raise RuntimeError("复权因子预热任务创建失败")
    if not execute_many(
        "insert into admin_adj_factor_warmup_items (task_id, position, code, status) values (%s, %s, %s, %s)",
        [(task_id, index + 1, code, ITEM_QUEUED) for index, code in enumerate(codes)],
    ):
        raise RuntimeError("复权因子预热任务明细创建失败")
    start_task(task_id)
    return get_task(task_id)


def _is_cancelled(task_id: str) -> bool:
    frame = query_dataframe("select status from admin_adj_factor_warmup_tasks where task_id = %s", (task_id,))
    return not frame.empty and str(frame.iloc[0]["status"]) == TASK_CANCELLED


def _finish_task(task_id: str, failed: bool) -> None:
    if _STOP_REQUESTED.is_set() or _is_cancelled(task_id):
        return
    status = TASK_FAILED if failed else TASK_SUCCESS
    error_message = "存在未补齐的复权因子" if failed else ""
    execute_sql(
        "update admin_adj_factor_warmup_tasks set status = %s, finished_at = now(), error_message = %s where task_id = %s",
        (status, error_message, task_id),
    )


def _run_task(task_id: str) -> None:
    failed = False
    try:
        if _STOP_REQUESTED.is_set():
            return
        execute_sql(
            "update admin_adj_factor_warmup_tasks set status = %s, started_at = coalesce(started_at, now()), error_message = '' where task_id = %s and status = %s",
            (TASK_RUNNING, task_id, TASK_QUEUED),
        )
        for row in _item_rows(task_id):
            if _STOP_REQUESTED.is_set() or _is_cancelled(task_id):
                return
            if str(row["status"]) in FINISHED_ITEM_STATUSES:
                continue
            position = int(row["position"])
            code = str(row["code"])
            execute_sql(
                "update admin_adj_factor_warmup_items set status = %s, started_at = coalesce(started_at, now()), error_message = '' where task_id = %s and position = %s",
                (ITEM_RUNNING, task_id, position),
            )
            try:
                task = get_task(task_id)
                items = _fetch_complete_factor_window(
                    code,
                    str(task["start_date"]),
                    str(task["end_date"]),
                    str(task["base_date"]),
                )
                if _STOP_REQUESTED.is_set():
                    return
                if _is_cancelled(task_id):
                    execute_sql(
                        "update admin_adj_factor_warmup_items set status = %s, finished_at = now() where task_id = %s and position = %s",
                        (ITEM_CANCELLED, task_id, position),
                    )
                    return
                if items == []:
                    failed = True
                    execute_sql(
                        "update admin_adj_factor_warmup_items set status = %s, factor_count = %s, finished_at = now(), error_message = %s, detail_json = %s where task_id = %s and position = %s",
                        (ITEM_FAILED, 0, "未返回复权因子", Jsonb({"base_date": task["base_date"]}), task_id, position),
                    )
                    continue
                execute_sql(
                    "update admin_adj_factor_warmup_items set status = %s, factor_count = %s, finished_at = now(), detail_json = %s where task_id = %s and position = %s",
                    (ITEM_SUCCESS, len(items), Jsonb({"base_date": task["base_date"]}), task_id, position),
                )
            except Exception as exc:
                failed = True
                execute_sql(
                    "update admin_adj_factor_warmup_items set status = %s, finished_at = now(), error_message = %s, detail_json = %s where task_id = %s and position = %s",
                    (ITEM_FAILED, str(exc), Jsonb({"error_type": type(exc).__name__}), task_id, position),
                )
        _finish_task(task_id, failed)
    finally:
        with _WORKERS_LOCK:
            _WORKERS.pop(task_id, None)


def start_task(task_id: str) -> None:
    with _WORKERS_LOCK:
        worker = _WORKERS.get(task_id)
        if worker is not None and worker.is_alive():
            return
        worker = threading.Thread(target=_run_task, args=(task_id,), name=f"adj-factor-warmup-{task_id}", daemon=True)
        _WORKERS[task_id] = worker
        worker.start()


def cancel_task(task_id: str) -> dict[str, object]:
    if get_task(task_id)["status"] not in ACTIVE_TASK_STATUSES:
        raise ValueError("只能取消排队或运行中的复权因子预热任务")
    execute_sql(
        "update admin_adj_factor_warmup_tasks set status = %s, finished_at = now(), error_message = %s where task_id = %s",
        (TASK_CANCELLED, "用户取消", task_id),
    )
    execute_sql(
        "update admin_adj_factor_warmup_items set status = %s, finished_at = now(), error_message = %s where task_id = %s and status in (%s, %s)",
        (ITEM_CANCELLED, "用户取消", task_id, ITEM_QUEUED, ITEM_RUNNING),
    )
    return get_task(task_id)


def resume_tasks() -> None:
    if not _ensure_schema():
        return
    _STOP_REQUESTED.clear()
    execute_sql(
        "update admin_adj_factor_warmup_items set status = %s, started_at = null where status = %s",
        (ITEM_QUEUED, ITEM_RUNNING),
    )
    execute_sql(
        "update admin_adj_factor_warmup_tasks set status = %s, started_at = null where status = %s",
        (TASK_QUEUED, TASK_RUNNING),
    )
    for row in _task_rows(200):
        if str(row["status"]) == TASK_QUEUED:
            start_task(str(row["task_id"]))


def stop_tasks() -> None:
    _STOP_REQUESTED.set()
    if not _ensure_schema():
        return
    execute_sql(
        "update admin_adj_factor_warmup_items set status = %s, started_at = null where status = %s",
        (ITEM_QUEUED, ITEM_RUNNING),
    )
    execute_sql(
        "update admin_adj_factor_warmup_tasks set status = %s, started_at = null where status = %s",
        (TASK_QUEUED, TASK_RUNNING),
    )
