# /api/stocks/quotes/daily-snapshot

`GET` 返回指定交易日的全市场股票日线快照。

## 查询参数

- `trade_date`（`str`）：交易日期，格式 `YYYY-MM-DD`。
- `fields`（`str`）：按逗号指定返回字段。
- `limit`（`int`）：返回记录上限。
- `offset`（`int`）：结果偏移量。
- `skip_suspended`（`bool`）：过滤停牌行。
- `skip_st`（`bool`）：如果某只股票在快照内 `is_st=true`，则整只股票过滤。

## 返回类型

顶层返回 `list[StockQuoteItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `trade_time`（`str`）：交易日期。
- `freq`（`str`）：固定返回 `1d`。
- `open`（`float | None`）：开盘价。
- `high`（`float | None`）：最高价。
- `low`（`float | None`）：最低价。
- `close`（`float | None`）：收盘价。
- `pre_close`（`float | None`）：前收盘价。
- `change`（`float | None`）：涨跌额。
- `pct_chg`（`float | None`）：涨跌幅。
- `volume`（`float | None`）：成交量。
- `amount`（`float | None`）：成交额。
- `adjust`（`str`）：固定返回 `none`。
- `is_suspended`（`bool`）：是否停牌。
- `is_st`（`bool`）：是否 ST。

## 补充说明

- 不需要传 `code` 或 `codes`。
- 主路径先读 `stocks.quotes.daily_snapshot`，未命中时先读本地 `fact.stock_daily_1d`。
- `fact.stock_daily_1d` 已纳入 `BJSE` 正式日线口径。
- 运行时按 Capability Matrix 勾选的源补源。
