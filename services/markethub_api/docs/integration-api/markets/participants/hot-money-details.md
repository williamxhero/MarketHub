# /api/markets/participants/hot-money/details

`GET` 返回游资营业部交易明细。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。
- `name`（类型：`str`）：游资或营业部名称筛选。
- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。
- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。

## 返回类型

顶层返回 `list[HotMoneyDetailItem]`。

## 返回字段

- `trade_date`（`str`）：交易日期。
- `name`（`str`）：游资或营业部名称。
- `code`（`str`）：股票代码。
- `stock_name`（`str`）：股票名称。
- `buy_amount`（`float | None`）：买入金额。
- `sell_amount`（`float | None`）：卖出金额。
- `net_amount`（`float | None`）：净买入金额。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
