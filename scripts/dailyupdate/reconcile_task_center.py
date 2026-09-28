from __future__ import annotations

import argparse
import json
import os
from urllib.request import Request, urlopen


TASK_ID = "markethub_global_data_update"
INTRADAY_TASK_ID = "markethub_stock_intraday_capture"


def desired_task() -> dict[str, object]:
    return {
        "task_id": TASK_ID,
        "name": "MarketHub stock daily close readiness 1605",
        "group_name": "MARKETHUB",
        "enabled": True,
        "schedule_type": "cron",
        "schedule_value": "5 16 * * *",
        "startup_delay_minutes": 0,
        "timezone": "Asia/Shanghai",
        "executor_type": "shell_file",
        "script_path": "/data/markethub/scripts/global-data-update-with-health.sh",
        "working_directory": "/data/markethub",
        "argument_text": "",
        "timeout_seconds": 21600,
        "description": (
            "交易日 16:05 启动 source-native 日线快照；等待源数据稳定后，只有目标日完整覆盖和健康门槛通过，"
            "才允许后续 AI 正式时点使用或发布。"
        ),
    }


def desired_intraday_task() -> dict[str, object]:
    return {
        "task_id": INTRADAY_TASK_ID,
        "name": "MarketHub stock 1m intraday capture",
        "group_name": "MARKETHUB",
        "enabled": True,
        "schedule_type": "cron",
        "schedule_value": "15 20 * * 1-5",
        "startup_delay_minutes": 0,
        "timezone": "Asia/Shanghai",
        "executor_type": "shell_file",
        "script_path": "/data/markethub/scripts/stock-intraday-capture-with-health.sh",
        "working_directory": "/data/markethub",
        "argument_text": "",
        "timeout_seconds": 21600,
        "description": (
            "每个交易日北京时间 20:15 串行触发 QuoteMux stocks.quotes.intraday；"
            "要求 1m 缺口采集完整成功，未完成则保留缺口并让任务失败。"
        ),
    }


def reconcile(base_url: str) -> dict[str, object]:
    return _reconcile_task(base_url, desired_task())


def reconcile_intraday(base_url: str) -> dict[str, object]:
    return _reconcile_task(base_url, desired_intraday_task())


def _reconcile_task(base_url: str, expected: dict[str, object]) -> dict[str, object]:
    payload = json.dumps(expected, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}/api/tasks/{expected['task_id']}",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="PUT",
    )
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise RuntimeError("Task Center returned a non-object response")
    for field in ("task_id", "schedule_type", "schedule_value", "timezone", "script_path"):
        if result.get(field) != expected[field]:
            raise RuntimeError(f"Task Center reconciliation mismatch: {field}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile MarketHub Task Center schedules")
    parser.add_argument("--base-url", default=os.getenv("MARKETHUB_TASK_CENTER_URL", "http://127.0.0.1:8810"))
    parser.add_argument("--task", choices=("readiness", "intraday"), default="readiness")
    parser.add_argument("--print", action="store_true", dest="print_only")
    args = parser.parse_args()
    if args.task == "intraday":
        result = desired_intraday_task() if args.print_only else reconcile_intraday(args.base_url)
    else:
        result = desired_task() if args.print_only else reconcile(args.base_url)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
