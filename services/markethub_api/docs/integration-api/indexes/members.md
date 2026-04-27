# /api/indexes/{index_code}/members

`GET` 返回单个指数的成分列表。

## 路径参数

- `index_code`（类型：`str`）：指数代码。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[IndexMemberItem]`。

## 返回字段

- `index_code`（`str`）：指数代码。
- `code`（`str`）：股票代码。
- `name`（`str`）：成分股名称。
- `weight`（`float | None`）：权重。
- `trade_date`（`str`）：成分权重对应的交易日。
