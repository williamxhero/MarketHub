# /api/indexes/quotes

`GET` 返回单个或多个指数的行情序列。

## 查询参数

- `index_code`（类型：`str`）：单个指数代码；与 `index_codes` 至少传一个。
- `index_codes`（类型：`str`）：多个指数代码，逗号分隔；与 `index_code` 至少传一个。
- `freq`（类型：`str`；默认：`1d`）：行情频率，可选 `1d`、`1w`、`1mo`。
- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。
- `count`（类型：`int | None`；允许空值；最小值：`1`）：每个代码返回的最近记录条数。
- `fields`（类型：`str`）：按逗号指定返回字段，不传返回全部字段。
- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。

## 返回类型

顶层返回 `list[IndexQuoteItem]`。

## 返回字段

- `index_code`（`str`）：指数代码。
- `trade_time`（`str`）：时间点；日频返回交易日，分钟级返回具体时间。
- `freq`（`str`）：数据频率。
- `open`（`float | None`）：开盘价。
- `high`（`float | None`）：最高价。
- `low`（`float | None`）：最低价。
- `close`（`float | None`）：收盘价。
- `pre_close`（`float | None`）：前收盘价。
- `change`（`float | None`）：涨跌额。
- `pct_chg`（`float | None`）：涨跌幅，单位 %。
- `volume`（`float | None`）：成交量。
- `amount`（`float | None`）：成交额。

## 补充说明

- `index_code` 与 `index_codes` 至少需要传一个，`count` 按每个指数分别生效。
- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
- 传入 `fields` 后，响应中的每条记录只保留所选字段。
