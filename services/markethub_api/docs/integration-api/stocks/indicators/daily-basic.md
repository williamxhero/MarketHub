# /api/stocks/indicators/daily-basic

`GET` 返回股票日频基础指标。

## 查询参数

- `code`（`str`）：单只股票代码。
- `codes`（`str`）：多只股票代码，逗号分隔。
- `trade_date`（`str`）：单日查询日期，格式 `YYYY-MM-DD`。
- `start_date`（`str`）：区间起始日期，格式 `YYYY-MM-DD`。
- `end_date`（`str`）：区间结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[StockDailyBasicItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `trade_date`（`str`）：交易日期。
- `turnover_rate`（`float | None`）：换手率，单位 `%`。
- `volume_ratio`（`float | None`）：量比。
- `pe`（`float | None`）：市盈率。
- `pb`（`float | None`）：市净率。
- `total_share`（`float | None`）：总股本。
- `float_share`（`float | None`）：流通股本。

## 补充说明

- `code` 和 `codes` 一次最多传 `200` 只股票；全市场取数请使用 `trade_date=单日` 且不要传 `code` 或 `codes`。
- 查询会优先用本地 `fact.stock_daily_1d` 返回已有行情覆盖的 `code/trade_date`，避免成员股链路因为指标 Store 空而直接中断。
- `turnover_rate`、`volume_ratio`、`pe`、`pb`、`total_share`、`float_share` 只有本地 Store 或外源已有对应字段时才返回具体数值；缺失时保持 `null`。
- 本地无覆盖时再进入 `stocks.indicators.daily_basic` 能力链路补源。
