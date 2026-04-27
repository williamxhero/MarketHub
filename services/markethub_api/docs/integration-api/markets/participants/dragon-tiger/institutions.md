# /api/markets/participants/dragon-tiger/institutions

`GET` 返回龙虎榜机构席位明细。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。
- `code`（类型：`str`）：股票代码筛选。
- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。

## 返回类型

顶层返回 `list[DragonTigerInstitutionItem]`。

## 返回字段

- `trade_date`（`str`）：交易日期。
- `code`（`str`）：股票代码。
- `name`（`str`）：证券简称。
- `buy_amount`（`float | None`）：买入金额。
- `sell_amount`（`float | None`）：卖出金额。
- `net_amount`（`float | None`）：净买入金额。
- `institution_count`（`int | None`）：机构席位数量。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
