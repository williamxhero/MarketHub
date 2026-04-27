# /api/stocks/{code}/corporate-actions/dividends

`GET` 返回单只股票的分红送转记录。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[DividendItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `announce_date`（`str`）：公告日期。
- `record_date`（`str`）：股权登记日。
- `ex_date`（`str`）：除权除息日。
- `pay_date`（`str`）：派息日期。
- `cash_dividend_per_share`（`float | None`）：每股现金分红。
- `stock_dividend_per_share`（`float | None`）：每股送股。
- `capital_reserve_per_share`（`float | None`）：每股转增资本公积。
