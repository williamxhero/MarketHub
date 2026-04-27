# /api/stocks/{code}/indicators/chip-performance

`GET` 返回单只股票的筹码盈亏分布数据。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[ChipPerformanceItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `trade_date`（`str`）：交易日期。
- `profit_ratio`（`float | None`）：获利盘占比，单位 %。
- `avg_cost`（`float | None`）：平均成本。
- `cost_70`（`float | None`）：70% 成本位。
- `cost_90`（`float | None`）：90% 成本位。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
