# /api/stocks/indicators/ah-comparisons

`GET` 返回 AH 股比价数据。

## 查询参数

- `code`（类型：`str`）：股票代码。
- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。
- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。
- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。

## 返回类型

顶层返回 `list[StockAHComparisonItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `name`（`str`）：A 股简称。
- `h_code`（`str`）：对应 H 股代码。
- `trade_date`（`str`）：交易日期。
- `a_close`（`float | None`）：A 股收盘价。
- `h_close`（`float | None`）：H 股收盘价。
- `premium_ratio`（`float | None`）：A/H 溢价率，单位 %。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
