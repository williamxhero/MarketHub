# /api/stocks/{code}/ownership/pledges/stats

`GET` 返回单只股票的股权质押统计。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[PledgeStatItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `trade_date`（`str`）：交易日期。
- `pledge_volume`（`float | None`）：质押数量。
- `pledge_ratio`（`float | None`）：质押比例，单位 %。
- `unrestricted_pledge_volume`（`float | None`）：无限售股质押数量。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
