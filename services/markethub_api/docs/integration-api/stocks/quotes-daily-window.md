# /api/stocks/quotes/daily-window

`GET` 返回指定日期区间内的全市场股票日线。

## 查询参数

- `start_date`（`str`）：起始交易日期，格式 `YYYY-MM-DD`。
- `end_date`（`str`）：结束交易日期，格式 `YYYY-MM-DD`。
- `fields`（`str`）：按逗号指定返回字段。
- `limit`（`int`）：返回记录上限。
- `offset`（`int`）：结果偏移量。
- `skip_suspended`（`bool`）：过滤停牌行。
- `skip_st`（`bool`）：如果某只股票在请求窗口内任一行 `is_st=true`，则整只股票过滤。

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
- 该接口直接读取本地 `fact.stock_daily_1d`，不复用 `/api/stocks/quotes` 的逐股票缺口补源链路。
- 先按 `skip_st` 整只过滤 ST 股票，再按 `skip_suspended` 过滤停牌行。
- 分页在过滤后生效，排序固定为 `trade_time, code`。
- 如果本地日线事实表缺数据，应先修复日线表刷新。
