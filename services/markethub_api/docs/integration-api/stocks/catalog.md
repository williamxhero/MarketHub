# /api/stocks/catalog

`GET` 返回股票基础清单。

## 查询参数

- `codes`（类型：`str`）：多个股票代码，逗号分隔。
- `name`（类型：`str`）：股票简称关键字。
- `market`（类型：`str`）：兼容参数，当前实现保留该入参但不参与筛选。
- `exchange`（类型：`str`）：交易所标识。
- `list_status`（类型：`str`）：上市状态筛选。
- `is_hs`（类型：`str`）：兼容参数，当前实现保留该入参但不参与筛选。
- `include_delisted`（类型：`bool`；默认：`false`）：是否包含已退市标的。
- `limit`（类型：`int`；默认：`5000`；范围：`1-5000`）：返回记录上限。
- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。

## 返回类型

顶层返回 `list[StockBasicInfo]`。

## 返回字段

- `code`（`str`）：股票代码。
- `name`（`str`）：名称。
- `exchange`（`str`）：交易所。
- `market`（`str`）：所属市场板块，如主板、创业板或北交所等口径值。
- `list_status`（`str`）：上市状态。
- `list_date`（`str`）：上市日期。
- `delist_date`（`str`）：退市日期；未退市时为空字符串。
- `industry`（`str`）：所属行业。
- `area`（`str`）：所属地域。

## 补充说明

- `market` 和 `is_hs` 当前只是兼容入参，不参与实际筛选。
