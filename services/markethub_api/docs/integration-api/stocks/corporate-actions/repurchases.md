# /api/stocks/{code}/corporate-actions/repurchases

`GET` 返回单只股票的回购记录。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[RepurchaseItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `announce_date`（`str`）：公告日期。
- `progress`（`str`）：进度状态。
- `repurchase_volume`（`float | None`）：回购数量。
- `repurchase_amount`（`float | None`）：回购金额。
- `highest_price`（`float | None`）：最高回购价。
- `lowest_price`（`float | None`）：最低回购价。
