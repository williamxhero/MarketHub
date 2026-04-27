# /api/boards/{board_code}/members

`GET` 返回单个板块在指定交易日的成分列表。

## 路径参数

- `board_code`（类型：`str`）：板块代码。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[BoardMemberItem]`。

## 返回字段

- `board_code`（`str`）：板块代码。
- `code`（`str`）：股票代码。
- `name`（`str`）：成分股名称。
- `weight`（`float | None`）：权重。
- `join_date`（`str`）：纳入日期。
