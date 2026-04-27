# /api/stocks/indicators/daily-market-value

`GET` 返回股票日频市值指标。

## 查询参数

- `code`（`str`）：单只股票代码。
- `codes`（`str`）：多只股票代码，逗号分隔。
- `trade_date`（`str`）：单日查询日期，格式 `YYYY-MM-DD`。
- `start_date`（`str`）：区间起始日期，格式 `YYYY-MM-DD`。
- `end_date`（`str`）：区间结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[StockDailyMarketValueItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `trade_date`（`str`）：交易日期。
- `total_mv`（`float | None`）：总市值。
- `float_mv`（`float | None`）：流通市值。
- `free_mv`（`float | None`）：自由流通市值。

## 补充说明

- 当前能力已改为 `Tushare only`，不再混接旧来源。
- `code` 和 `codes` 一次最多传 `200` 只股票；全市场取数请使用 `trade_date=单日` 且不要传 `code` 或 `codes`。
- 未传 `code` 和 `codes` 时，仅支持 `trade_date=单日` 的全市场查询；这时走 TS 单日全市场截面，避免拆成全市场代码批量请求。
- 未传 `code` 和 `codes` 时，不支持多日全市场区间查询。
