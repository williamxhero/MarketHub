# /api/stocks/quotes/query

`GET` 返回股票行情数据和完整性元信息，适合大批量、本地扫描和需要确认每只股票实际起止时间的调用场景。

## 查询参数

- `code`（`str`）：单个股票代码；与 `codes` 至少传一个。
- `codes`（`str`）：多个股票代码，逗号分隔；与 `code` 至少传一个。
- `freq`（`str`，默认 `1d`）：行情频率，可选 `tick`、`1m`、`5m`、`15m`、`30m`、`60m`、`1d`、`1w`、`1mo`。
- `trade_date`（`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（`str`）：结束日期，格式 `YYYY-MM-DD`。
- `start_time`（`str`）：起始时间；分钟行情可传完整时间字符串。
- `end_time`（`str`）：结束时间；分钟行情可传完整时间字符串。
- `count`（`int | None`）：每个股票返回的最近记录条数。
- `adjust`（`str`，默认 `none`）：复权方式。
- `fields`（`str`）：按逗号指定 `items` 中每条行情记录的返回字段；不影响 `meta`。
- `limit`（`int | None`）：调用方主动裁剪返回条数；不传时返回完整结果。
- `skip_suspended`（`bool`，默认 `true`）：兼容保留参数；当前服务层不再单独走旧停牌跳过逻辑。
- `fill_missing`（`bool`，默认 `false`）：兼容保留参数；当前缺口补源已自动开启，不需要显式传 `true`。

## 返回类型

顶层返回 `StockQuotesQueryResult`。

## 返回字段

- `items`（`list[StockQuoteItem]`）：行情记录列表。
- `meta`（`StockQuotesMeta`）：本次查询的完整性元信息。
- `items.code`（`str`）：股票代码。
- `items.trade_time`（`str`）：时间点；日频返回交易日，分钟频返回具体时间。
- `items.freq`（`str`）：数据频率。
- `items.open`（`float | None`）：开盘价。
- `items.high`（`float | None`）：最高价。
- `items.low`（`float | None`）：最低价。
- `items.close`（`float | None`）：收盘价。
- `items.pre_close`（`float | None`）：前收盘价。
- `items.change`（`float | None`）：涨跌额。
- `items.pct_chg`（`float | None`）：涨跌幅，单位 `%`。
- `items.volume`（`float | None`）：成交量。
- `items.amount`（`float | None`）：成交额。
- `items.adjust`（`str`）：复权方式。
- `meta.total_rows`（`int`）：服务端查询到的总行数。
- `meta.returned_rows`（`int`）：本次实际返回的行数。
- `meta.complete`（`bool`）：整体结果是否完整。
- `meta.truncated`（`bool`）：本次响应是否因 `limit` 被调用方主动裁剪。
- `meta.codes`（`list[StockQuoteCodeSummary]`）：每只股票的完整性统计。
- `meta.codes.code`（`str`）：股票代码。
- `meta.codes.row_count`（`int`）：该股票本次实际返回行数。
- `meta.codes.expected_bar_count`（`int`）：该股票在请求窗口内按当前频率预期应有的 bar 数；当前对 `30m` 提供 bar 级统计。
- `meta.codes.actual_bar_count`（`int`）：该股票在请求窗口内实际命中的预期 bar 数；当前对 `30m` 提供 bar 级统计。
- `meta.codes.first_trade_time`（`str`）：该股票本次实际返回的第一条时间。
- `meta.codes.last_trade_time`（`str`）：该股票本次实际返回的最后一条时间。
- `meta.codes.complete`（`bool`）：该股票结果是否完整。
- `meta.codes.truncated`（`bool`）：该股票结果是否因 `limit` 被裁剪。
- `meta.codes.missing_trade_dates`（`list[str]`）：该股票在请求交易日范围内缺失的交易日。
- `meta.codes.missing_trade_times`（`list[str]`）：该股票在请求窗口内缺失的具体 bar 时间；当前对 `30m` 提供 bar 级统计。

## 补充说明

- 不传 `limit` 时，服务端返回完整结果，并在 `meta` 中说明每只股票的起止时间和完整性。
- 传入 `limit` 时，`limit` 表示调用方主动裁剪总返回条数；如果发生裁剪，`meta.truncated=true` 且 `meta.complete=false`。
- `fields` 只裁剪 `items` 内的行情字段，不裁剪 `meta`。
- `meta.codes.complete=false` 时，调用方不应把该股票用于需要完整窗口的扫描。
- `30m` 分钟线完整性按 bar 级判断；缺失一根 30m K 线时，`meta.codes.complete=false` 并返回具体 `missing_trade_times`。
- 本地已有的行情会直接用于结果；只有本地缺失的日期或 bar 所在日期会进入外源补缺。
- ChannelN 这类需要预热 K 线的扫描应优先使用本接口，并检查 `meta.complete` 和每只股票的 `complete`。
