# /api/stocks/{code}/corporate-actions/rights-issues

`GET` 返回单只股票的配股记录。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[RightsIssueItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `announce_date`（`str`）：公告日期。
- `rights_ratio`（`float | None`）：配股比例。
- `rights_price`（`float | None`）：配股价格。
- `record_date`（`str`）：股权登记日。
- `ex_date`（`str`）：除权除息日。
