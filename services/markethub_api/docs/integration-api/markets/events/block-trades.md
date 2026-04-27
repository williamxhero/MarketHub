# /api/markets/events/block-trades

`GET` 返回市场大宗交易明细。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。
- `code`（类型：`str`）：股票代码筛选。
- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。

## 返回类型

顶层返回 `list[BlockTradeItem]`。

## 返回字段

- `trade_date`（`str`）：交易日期。
- `code`（`str`）：股票代码。
- `name`（`str`）：证券简称。
- `price`（`float | None`）：价格。
- `volume`（`float | None`）：成交量。
- `amount`（`float | None`）：成交额。
- `buyer`（`str`）：买方营业部或席位名称。
- `seller`（`str`）：卖方营业部或席位名称。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
