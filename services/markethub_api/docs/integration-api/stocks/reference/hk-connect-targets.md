# /api/stocks/reference/hk-connect-targets

`GET` 返回沪深港通标的范围。

## 查询参数

- `direction`（类型：`str`）：互联互通方向筛选，如 `north` 或 `south`。
- `status`（类型：`str`）：标的状态筛选。
- `effective_date`（类型：`str`）：生效日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[HKConnectTargetItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `name`（`str`）：名称。
- `direction`（`str`）：互联互通方向，如 `north` 或 `south`。
- `status`（`str`）：标的状态，如调入、调出或有效状态。
- `effective_date`（`str`）：生效日期。
