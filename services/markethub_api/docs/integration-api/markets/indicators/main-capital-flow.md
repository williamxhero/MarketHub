# /api/markets/indicators/main-capital-flow

`GET` 返回市场主力资金流指标。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[MarketCapitalFlowItem]`。

## 返回字段

- `trade_date`（`str`）：交易日期。
- `market`（`str`）：市场范围标识，如沪市、深市或全市场口径。
- `main_inflow`（`float | None`）：主力流入金额。
- `main_outflow`（`float | None`）：主力流出金额。
- `net_inflow`（`float | None`）：净流入金额。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
