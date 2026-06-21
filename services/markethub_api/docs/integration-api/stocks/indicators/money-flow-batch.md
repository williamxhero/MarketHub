# /api/stocks/indicators/money-flow/batch

`GET` 返回多只股票在指定交易日的资金流指标。

## 查询参数

- `codes`（`str`）：股票代码列表，多个代码用逗号分隔。
- `trade_date`（`str`）：交易日期，格式 `YYYY-MM-DD`。
- `view`（`str`，默认 `main`）：资金流视图，当前固定使用 `main`。

## 返回类型

顶层返回 `list[StockMoneyFlowItem]`。

## Capability

- `stocks.indicators.money_flow`

## 返回字段

- `code`（`str`）：股票代码。
- `trade_date`（`str`）：交易日期。
- `view`（`str`）：返回视图标识。
- `main_inflow`（`float | None`）：主力流入金额。
- `main_outflow`（`float | None`）：主力流出金额。
- `net_inflow`（`float | None`）：净流入金额。

## 补充说明

- 该接口直接从本地 `fact.stock_daily_1d` 批量读取资金流字段，返回结构与单票资金流接口一致。
- 当日线存在但资金流明细字段缺失时，`net_inflow` 返回 `0.0`，避免调用方把已有行情覆盖误判成接口断链。
- 缺少指定交易日行情数据时快速返回空数组，不按股票代码串行触发外源补齐。
