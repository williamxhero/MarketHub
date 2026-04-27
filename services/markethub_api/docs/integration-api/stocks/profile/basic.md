# /api/stocks/{code}/profile/basic

`GET` 返回单只股票的基础资料。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 返回类型

顶层返回 `StockBasicInfo`；查不到对应记录时返回空对象 `{}`。

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

- 查不到对应记录时返回空对象 `{}`。
