# /api/boards/quotes/daily-snapshot

`GET` 返回指定交易日的全市场板块日线快照。

## 查询参数

- `trade_date`（`str`）：交易日期，格式 `YYYY-MM-DD`。
- `fields`（`str`）：按逗号指定返回字段，不传返回全部字段。
- `limit`（`int`，默认 `10000`，范围 `1-10000`）：返回记录上限。
- `offset`（`int`，默认 `0`）：分页偏移量。

## 返回类型

顶层返回 `list[BoardQuoteItem]`。

## 返回字段

- `board_code`（`str`）：板块代码。
- `board_name`（`str`）：板块名称。
- `trade_time`（`str`）：交易日期。
- `freq`（`str`）：固定为 `1d`。
- `open`（`float | None`）：开盘价。
- `high`（`float | None`）：最高价。
- `low`（`float | None`）：最低价。
- `close`（`float | None`）：收盘价。
- `pre_close`（`float | None`）：前收盘价。
- `change`（`float | None`）：涨跌额。
- `pct_chg`（`float | None`）：涨跌幅，单位 `%`。
- `volume`（`float | None`）：成交量。
- `amount`（`float | None`）：成交额。

## 补充说明

- 该接口直接读取本地 `fact.board_daily_1d`，并通过 `ref.board` 补齐 `board_name`。
- 本地快照为空时，按活跃板块目录请求 `boards.quotes.daily`，由该 capability 的 provider package 补齐日线字段。
- 调用方不需要传 `board_code` 或 `board_codes`，适合复盘助手一次性获取全市场板块快照。
