from __future__ import annotations

import anyio
from fastapi import APIRouter

from services.data_health import get_data_health, run_data_health_check


router = APIRouter()


@router.get(
    "/api/data-health",
    summary="返回 MarketHub 数据健康状态",
    description="""`GET` 返回最近一次数据健康检查结果，不执行全量检查。

## 返回类型

顶层返回 `DataHealthPayload`。

## 数据来源

- 读取 `data-health/latest.json`。
- 页面刷新只读快照，不扫描数据库。
- 定时任务或手动按钮通过 `POST /api/data-health/run` 生成新快照。""",
)
async def api_data_health() -> dict[str, object]:
    return get_data_health()


@router.post(
    "/api/data-health/run",
    summary="执行 MarketHub 数据健康全量检查",
    description="""`POST` 执行 capability 规则、交易日历、本地 fact/ref 表组合后的全量数据健康检查，并写入最近检查结果。

## 返回类型

顶层返回 `DataHealthPayload`。

## 检查范围

- 每个 capability 是否登记逻辑健康规则。
- 逻辑规则依赖的本地 fact/ref 表是否存在。
- 需要日期判断的 capability 是否可读取 `ref.trade_calendar`。
- 不触发外部 provider 拉取数据。""",
)
async def api_data_health_run() -> dict[str, object]:
    return await anyio.to_thread.run_sync(run_data_health_check)
