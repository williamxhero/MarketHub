# /api/markets/connect/capital-flow

`GET` 返回沪深港通资金流向数据。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[ConnectCapitalFlowItem]`。

## 返回字段

- `trade_date`（`str`）：交易日期。
- `market`（`str`）：互联互通市场方向，如沪股通、深股通或港股通。
- `buy_amount`（`float | None`）：买入金额。
- `sell_amount`（`float | None`）：卖出金额。
- `net_amount`（`float | None`）：净买入金额。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
