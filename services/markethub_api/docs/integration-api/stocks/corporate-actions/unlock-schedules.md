# /api/stocks/{code}/corporate-actions/unlock-schedules

`GET` 返回单只股票的限售解禁安排。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `unlock_date`（类型：`str`）：解禁日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[UnlockScheduleItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `unlock_date`（`str`）：解禁日期。
- `holder_type`（`str`）：持有人类型。
- `unlock_volume`（`float | None`）：解禁数量。
- `unlock_ratio`（`float | None`）：解禁比例，单位 %。
- `share_type`（`str`）：股份类型。
