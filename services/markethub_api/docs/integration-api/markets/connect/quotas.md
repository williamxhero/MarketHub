# /api/markets/connect/quotas

`GET` 返回沪深港通额度使用情况。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。
- `type`（类型：`str`）：互联互通额度类型筛选，如沪股通、深股通或港股通。

## 返回类型

顶层返回 `list[ConnectQuotaItem]`。

## 返回字段

- `trade_date`（`str`）：交易日期。
- `market`（`str`）：互联互通市场方向，如沪股通、深股通或港股通。
- `quota_total`（`float | None`）：总额度。
- `quota_balance`（`float | None`）：剩余额度。
- `quota_used`（`float | None`）：已使用额度。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
