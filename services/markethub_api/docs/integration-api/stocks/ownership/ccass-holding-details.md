# /api/stocks/{code}/ownership/ccass-holding-details

`GET` 返回单只股票的中央结算持股明细。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[CcassHoldingDetailItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `trade_date`（`str`）：交易日期。
- `participant_id`（`str`）：参与者编号。
- `participant_name`（`str`）：参与者名称。
- `holding_volume`（`float | None`）：持有数量。
- `holding_ratio`（`float | None`）：持有占比，单位 %。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
