# /api/stocks/{code}/quotes/auctions

`GET` 返回单只股票的竞价行情数据。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `session`（类型：`str`；默认：`open`）：竞价时段。
- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。

## 返回类型

顶层返回 `list[AuctionItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `trade_date`（`str`）：交易日期。
- `auction_time`（`str`）：竞价时间。
- `price`（`float | None`）：价格。
- `volume`（`float | None`）：成交量。
- `amount`（`float | None`）：成交额。
- `session`（`str`）：竞价时段，如开盘竞价。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
