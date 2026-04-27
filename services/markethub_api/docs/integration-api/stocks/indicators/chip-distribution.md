# /api/stocks/{code}/indicators/chip-distribution

`GET` 返回单只股票的筹码分布数据。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[ChipDistributionItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `trade_date`（`str`）：交易日期。
- `price`（`float | None`）：价格。
- `chip_ratio`（`float | None`）：筹码占比，单位 %。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
