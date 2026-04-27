# /api/stocks/{code}/indicators/money-flow

`GET` 返回单只股票的资金流指标。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。
- `view`（类型：`str`；默认：`summary`）：资金流视图，可选 `summary`、`trend`、`breakdown`。

## 返回类型

顶层返回 `list[StockMoneyFlowItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `trade_date`（`str`）：交易日期。
- `view`（`str`）：返回视图标识。
- `main_inflow`（`float | None`）：主力流入金额。
- `main_outflow`（`float | None`）：主力流出金额。
- `net_inflow`（`float | None`）：净流入金额。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
