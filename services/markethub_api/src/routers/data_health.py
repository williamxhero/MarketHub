from __future__ import annotations

from fastapi import APIRouter

from services.data_health import get_data_health


router = APIRouter()


@router.get(
    "/api/data-health",
    summary="返回 MarketHub 数据健康状态",
    description="""`GET` 返回 capability 规则、交易日历、本地 fact/ref 表组合后的数据健康状态。

## 返回类型

顶层返回 `DataHealthPayload`。

## 检查范围

- 每个 capability 是否登记逻辑健康规则。
- 逻辑规则依赖的本地 fact/ref 表是否存在。
- 需要日期判断的 capability 是否可读取 `ref.trade_calendar`。
- 不触发外部 provider 拉取数据。""",
)
async def api_data_health() -> dict[str, object]:
    return get_data_health()
