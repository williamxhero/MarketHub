# /api/stocks/quotes

`GET` 返回单只或多只股票的行情序列。

## 查询参数

- `code`（`str`）：单个股票代码；与 `codes` 至少传一个。
- `codes`（`str`）：多个股票代码，逗号分隔；与 `code` 至少传一个。
- `freq`（`str`，默认 `1d`）：行情频率，可选 `tick`、`1m`、`5m`、`15m`、`30m`、`60m`、`1d`、`1w`、`1mo`。
- `trade_date`（`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（`str`）：结束日期，格式 `YYYY-MM-DD`。
- `start_time`（`str`）：起始时间；分钟行情可传完整时间字符串。
- `end_time`（`str`）：结束时间；分钟行情可传完整时间字符串。
- `count`（`int | None`）：每个代码返回的最近记录条数。
- `adjust`（`str`，默认 `none`）：复权方式。
- `fields`（`str`）：按逗号指定返回字段；不传则返回全部字段。
- `limit`（`int`，默认 `200`，范围 `1-5000`）：返回记录上限。
- `skip_suspended`（`bool`，默认 `true`）：兼容保留参数；当前服务层不再单独走旧停牌跳过逻辑。
- `fill_missing`（`bool`，默认 `false`）：兼容保留参数；当前缺口补源已自动开启，不需要显式传 `true`。

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

## 补充说明

- `code` 和 `codes` 至少需要传一个；`count` 按每个股票分别生效。
- `trade_date` 适合单日查询；`start_date` 和 `end_date` 用于区间查询。
- 传入 `fields` 后，每条记录只保留所选字段。
- `freq=1d/1w/1mo` 先读取 QuoteMux Store 的 `stocks.quotes.daily`；Store 未命中时按 Capability Matrix 并发取数，其中 `1w`、`1mo` 在日线基础上聚合。
- `fact.stock_daily_1d` 已纳入 `BJSE` 正式日线口径，所以 `1d` 日线查询会正常返回北交所股票。
- `freq=1m/5m/15m/30m/60m` 先读取 QuoteMux Store 的 `stocks.quotes.intraday`，未命中时走 `OpenTDX -> efinance -> mootdx -> akshare`。
- `freq=1d/1w/1mo` 的默认 provider 候选是 `static_core -> Tushare -> efinance -> mootdx -> akshare`。
- Runtime Profile 会按 Capability Matrix 勾选的源并发取数，再按该 capability 的 `merge_strategy` 合并成一份结果。
- `fill_missing` 当前只做兼容保留，不再控制是否补源。
- 如果需求是“按 `trade_date` 直接拿全市场日线快照”，请使用 `GET /api/stocks/quotes/daily-snapshot`，不要再拼大批量 `codes`。
- `open`、`high`、`low`、`close`、`volume`、`amount` 按底层真实值返回；底层该行为空时，接口返回 `null`，不会额外脑补。
- `pre_close`、`change`、`pct_chg` 由服务层根据上一条可用收盘价计算；上一条收盘价不存在或不可用时，这几个字段会返回 `null`。
