# /api/markets/trading/open-auctions

`GET` 返回市场开盘竞价汇总。

## 查询参数

- `codes`（类型：`str`）：股票代码列表，逗号分隔。
- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `instrument_type`（类型：`str`；默认：`stock`）：标的类型，当前实现仅按股票口径返回。

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

- `instrument_type` 当前不会改变返回口径，接口始终按股票竞价数据返回。
