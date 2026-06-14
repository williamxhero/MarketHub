# /api/stocks/quotes

`GET` 返回单只或多只股票的行情序列。该接口保留列表返回形式，适合已有调用方兼容使用；需要完整性元信息时，请使用 `/api/stocks/quotes/query`。

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
- `fields`（`str`）：按逗号指定返回字段；不传则返回全部字段。
- `limit`（`int | None`）：调用方主动裁剪总返回条数；不传则返回完整结果。
- `skip_suspended`（`bool`，默认 `true`）：仅对 `1d/1w/1mo` 生效；强制过滤停牌行。
- `skip_st`（`bool`，默认 `false`）：仅对 `1d/1w/1mo` 生效；如果某只股票在请求窗口内任一行 `is_st=true`，则该股票所有返回行都会被过滤。
- `fill_missing`（`bool`，默认 `false`）：控制是否返回日线缺口补洞产生的停牌占位行；只有 `fill_missing=true&skip_suspended=false` 时才返回 `is_suspended=true` 行。

## 返回类型

顶层返回 `list[StockQuoteItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `trade_time`（`str`）：时间点；日频返回交易日，分钟频返回具体时间。
- `freq`（`str`）：数据频率。
- `open`（`float | None`）：开盘价。
- `high`（`float | None`）：最高价。
- `low`（`float | None`）：最低价。
- `close`（`float | None`）：收盘价。
- `pre_close`（`float | None`）：前收盘价。
- `change`（`float | None`）：涨跌额。
- `pct_chg`（`float | None`）：涨跌幅，单位 `%`。
- `volume`（`float | None`）：成交量。
- `amount`（`float | None`）：成交额。
- `adjust`（`str`）：复权方式。
- `is_suspended`（`bool`）：该行是否停牌。
- `is_st`（`bool`）：该行是否 ST。

## 补充说明

- `freq=1d/1w/1mo` 先读本地 `fact.stock_daily_1d` 和 `stocks.quotes.daily` 能力链路，发现历史交易日缺口时进入 provider 补缺链路。
- Runtime Profile 会按 Capability Matrix 勾选的源补齐本地缺口。
- `fact.stock_daily_1d` 已纳入 `BJSE` 正式日线口径，所以 `1d` 日线查询会正常返回北交所股票。
- 如果 provider 补缺后仍缺少历史交易日，且该股票能找到前一个交易日，系统会用前一交易日收盘价写入一条 `is_suspended=true` 的停牌占位日线。
- 停牌占位日线的 `open/high/low/close` 等于前一交易日 `close`，`volume=0`，`amount=0`，`is_st` 沿用前一交易日。
- 停牌占位写入 `fact.stock_daily_1d` 后，后续查询不再把该交易日识别为历史缺口。
- `fill_missing=false` 默认不返回停牌占位行；`skip_suspended=true` 会强制过滤所有停牌行。
- `skip_suspended` 只过滤停牌行，不会过滤 ST 股票。
- `skip_st=true` 会按请求窗口整只过滤 ST 股票；`skip_st=false` 时 ST 股票正常返回。
- `1w`、`1mo` 在日线结果上聚合，并沿用同一套 `fill_missing`、`skip_suspended`、`skip_st` 规则。
- 分钟线不应用停牌补洞和 ST 整只过滤规则。
