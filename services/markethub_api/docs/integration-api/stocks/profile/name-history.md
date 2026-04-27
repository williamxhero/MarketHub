# /api/stocks/{code}/profile/name-history

`GET` 返回单只股票的名称变更记录。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[NameHistoryItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `name`（`str`）：名称。
- `start_date`（`str`）：start日期。
- `end_date`（`str`）：END日期。
- `ann_date`（`str`）：公告日期。
