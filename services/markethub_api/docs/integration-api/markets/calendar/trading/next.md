# /api/markets/calendar/trading/next

`GET` 返回给定日期之后的最近若干个交易日。

## 查询参数

- `exchange`（类型：`str`；默认：`SSE`）：交易所标识。
- `trade_date`（类型：`str`）：参考交易日，返回该日期之后的开市日，格式 `YYYY-MM-DD`。
- `n`（类型：`int`；默认：`1`；范围：`1-5000`）：返回记录数量。

## 返回类型

顶层返回 `list[TradingCalendarItem]`。

## 返回字段

- `exchange`（`str`）：交易所标识，如 `SSE`、`SZSE`、`BSE`、`HKEX`。
- `trade_date`（`str`）：交易日日期。
- `is_open`（`bool`）：是否为开市日。

## 补充说明

- 结果按日期升序返回，最多返回 `n` 个开市日。
- 该接口是显式登记在 `DERIVED_CAPABILITY_BASE_IDS` 的派生视图；不独立配置 TTL、缓存策略、采集策略或更新频率。
- 执行时读取主 capability `markets.calendar.trading` 的 QuoteMux Store 和配置，再从交易日历中截取 `trade_date` 之后最多 `n` 个开市日。
