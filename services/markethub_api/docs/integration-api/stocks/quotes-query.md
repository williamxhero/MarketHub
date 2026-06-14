# /api/stocks/quotes/query

`GET` 返回股票行情数据和完整性元信息，适合批量查询、本地扫描和需要确认每只股票覆盖情况的调用场景。

## 查询参数

- `code`（`str`）：单个股票代码；与 `codes` 至少传一个。
- `codes`（`str`）：多个股票代码，逗号分隔；与 `code` 至少传一个。
- `freq`（`str`，默认 `1d`）：行情频率，可选 `tick`、`1m`、`5m`、`15m`、`30m`、`60m`、`1d`、`1w`、`1mo`。
- `trade_date`（`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（`str`）：结束日期，格式 `YYYY-MM-DD`。
- `start_time`（`str`）：起始时间；分钟行情可传完整时间字符串。
- `end_time`（`str`）：结束时间；分钟行情可传完整时间字符串。
- `count`（`int | None`）：每只股票返回最近若干条记录。
- `adjust`（`str`，默认 `none`）：复权方式。
- `fields`（`str`）：按逗号指定 `items` 的返回字段；不影响 `meta`。
- `limit`（`int | None`）：调用方主动裁剪总返回条数；不传则返回完整结果。
- `skip_suspended`（`bool`，默认 `true`）：仅对 `1d/1w/1mo` 生效；强制过滤停牌行。
- `skip_st`（`bool`，默认 `false`）：仅对 `1d/1w/1mo` 生效；如果某只股票在请求窗口内任一行 `is_st=true`，则该股票所有返回行都会被过滤。
- `fill_missing`（`bool`，默认 `false`）：控制是否返回日线缺口补洞产生的停牌占位行；只有 `fill_missing=true&skip_suspended=false` 时才返回 `is_suspended=true` 行。

## 返回类型

顶层返回 `StockQuotesQueryResult`。

## 返回字段

- `items`（`list[StockQuoteItem]`）：行情记录列表。
- `meta`（`StockQuotesMeta`）：本次查询的完整性元信息。
- `items.code`（`str`）：股票代码。
- `items.trade_time`（`str`）：时间点。
- `items.freq`（`str`）：数据频率。
- `items.open`（`float | None`）：开盘价。
- `items.high`（`float | None`）：最高价。
- `items.low`（`float | None`）：最低价。
- `items.close`（`float | None`）：收盘价。
- `items.pre_close`（`float | None`）：前收盘价。
- `items.change`（`float | None`）：涨跌额。
- `items.pct_chg`（`float | None`）：涨跌幅。
- `items.volume`（`float | None`）：成交量。
- `items.amount`（`float | None`）：成交额。
- `items.adjust`（`str`）：复权方式。
- `items.is_suspended`（`bool`）：是否停牌。
- `items.is_st`（`bool`）：是否 ST。
- `meta.total_rows`（`int`）：过滤后可返回集合的总行数。
- `meta.returned_rows`（`int`）：实际返回行数。
- `meta.complete`（`bool`）：整体结果是否完整。
- `meta.truncated`（`bool`）：是否被 `limit` 裁剪。
- `meta.codes`（`list[StockQuoteCodeSummary]`）：每只股票的完整性统计。
- `meta.codes.code`（`str`）：股票代码。
- `meta.codes.row_count`（`int`）：该股票实际返回行数。
- `meta.codes.expected_bar_count`（`int`）：预期 bar 数。
- `meta.codes.actual_bar_count`（`int`）：实际覆盖 bar 数。
- `meta.codes.first_trade_time`（`str`）：首条返回时间。
- `meta.codes.last_trade_time`（`str`）：末条返回时间。
- `meta.codes.complete`（`bool`）：该股票是否完整。
- `meta.codes.truncated`（`bool`）：该股票是否被裁剪。
- `meta.codes.missing_trade_dates`（`list[str]`）：缺失交易日。
- `meta.codes.missing_trade_times`（`list[str]`）：缺失 bar 时间。

## 补充说明

- `fields` 只裁剪 `items`，不裁剪 `meta`。
- `freq=1d/1w/1mo` 先读本地 `fact.stock_daily_1d`，发现历史交易日缺口时进入 provider 补缺链路。
- 如果 provider 补缺后仍缺少历史交易日，且该股票能找到前一个交易日，系统会用前一交易日收盘价写入一条 `is_suspended=true` 的停牌占位日线。
- 已写入 `fact.stock_daily_1d` 的停牌占位日线会参与完整性覆盖计算，因此不会再计入 `missing_trade_dates`。
- `fill_missing=false` 默认不返回停牌占位行；`skip_suspended=true` 会强制过滤所有停牌行。
- `skip_st=true` 时，被过滤掉的股票仍会在 `meta.codes` 中保留一条 summary，但 `row_count=0`、`complete=false`。
