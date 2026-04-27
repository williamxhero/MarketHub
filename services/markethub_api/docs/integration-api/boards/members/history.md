# /api/boards/{board_code}/members/history

`GET` 返回单个板块的成分变动历史。

## 路径参数

- `board_code`（类型：`str`）：板块代码。

## 查询参数

- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[BoardMemberHistoryItem]`。

## 返回字段

- `board_code`（`str`）：板块代码。
- `code`（`str`）：股票代码。
- `name`（`str`）：成分股名称。
- `effective_date`（`str`）：生效日期。
- `action`（`str`）：变动动作。
