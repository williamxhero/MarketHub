# /api/stocks/{code}/profile/managers

`GET` 返回单只股票的管理层名单。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 返回类型

顶层返回 `list[StockManagerItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `name`（`str`）：名称。
- `title`（`str`）：职务。
- `gender`（`str`）：性别。
- `education`（`str`）：学历。
- `begin_date`（`str`）：开始日期。
- `end_date`（`str`）：END日期。
