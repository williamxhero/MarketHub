# /api/boards/quotes

`GET` 返回单个或多个板块的行情序列。

## 查询参数

- `board_code`（`str`）：单个板块代码；与 `board_codes` 至少传一个。
- `board_codes`（`str`）：多个板块代码，逗号分隔；与 `board_code` 至少传一个。
- `freq`（`str`，默认 `1d`）：行情频率，可选 `1d`、`1w`、`1mo`。
- `trade_date`（`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（`str`）：结束日期，格式 `YYYY-MM-DD`。
- `count`（`int | None`）：每个板块返回的最近记录条数。
- `fields`（`str`）：按逗号指定返回字段，不传返回全部字段。
- `limit`（`int`，默认 `200`，范围 `1-5000`）：返回记录上限。

## 返回类型

顶层返回 `list[BoardQuoteItem]`。

## 返回字段

- `board_code`（`str`）：板块代码。
- `board_name`（`str`）：板块名称。
- `trade_time`（`str`）：交易日期。
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

## 补充说明

- `freq=1d` 且指定 `trade_date` 的单日查询直接读取本地 `fact.board_daily_1d`，并通过 `ref.board` 补齐 `board_name`。
- `pre_close`、`change`、`pct_chg` 会基于前一个已有交易日收盘价派生。
- 没有本地数据时快速返回空数组，不在请求线程内触发外源补齐。
