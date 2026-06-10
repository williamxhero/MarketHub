# /api/stocks/quotes/daily-snapshot

`GET` 返回指定交易日的全市场股票日线快照。

## 查询参数

- `trade_date`（`str`）：交易日期，格式 `YYYY-MM-DD`。
- `fields`（`str`）：按逗号指定返回字段；不传则返回全部字段。
- `limit`（`int`，默认 `10000`，范围 `1-10000`）：返回记录上限。
- `offset`（`int`，默认 `0`，最小值 `0`）：结果偏移量。

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
- `pct_chg`（`float | None`）：涨跌幅，单位 `%`。
- `volume`（`float | None`）：成交量。
- `amount`（`float | None`）：成交额。
- `adjust`（`str`）：固定返回 `none`。

## 补充说明

- 这是面向“某日全市场日线统计”的正式入口，不需要传 `code` 或 `codes`。
- 默认 provider 候选是 `static_core -> Tushare -> efinance -> mootdx -> akshare`。
- 主路径先读取 QuoteMux Store 的 `stocks.quotes.daily_snapshot`，未命中时先读本地 `fact.stock_daily_1d`，仍有缺口时再按 Capability Matrix 补源。
- Runtime Profile 会按 Capability Matrix 勾选的源补齐本地缺口，再按该 capability 的 `merge_strategy` 合并成一份结果。
- `fact.stock_daily_1d` 已纳入 `BJSE` 正式日线口径，所以该快照接口会返回北交所股票。
- 该接口只支持单个交易日快照；全市场区间行情使用 `GET /api/stocks/quotes/daily-window`。
- 如需分页读取，可配合 `limit` 和 `offset` 使用。
- `open`、`high`、`low`、`close`、`volume`、`amount` 按 `fact.stock_daily_1d` 或补源 provider 的真实值返回；底层本身为空时，接口也会返回 `null`，不会额外脑补。
- `pre_close`、`change`、`pct_chg` 依赖上一条可用收盘价；如果上一条收盘价不存在或不可用，这几个字段会返回 `null`。
