# /api/stocks/indicators/risk-flags

`GET` 返回股票风险标识记录。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。
- `flag_type`（类型：`str`）：风险标识类型筛选。
- `status`（类型：`str`）：状态筛选。
- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。
- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。

## 返回类型

顶层返回 `list[StockRiskFlagItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `name`（`str`）：名称。
- `flag_type`（`str`）：风险标识类型。
- `start_date`（`str`）：start日期。
- `end_date`（`str`）：END日期。
- `status`（`str`）：风险标识状态。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
