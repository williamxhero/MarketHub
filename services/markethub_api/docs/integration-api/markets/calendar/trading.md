# /api/markets/calendar/trading

`GET` 返回交易日历列表。

## 查询参数

- `exchange`（`str`，默认 `SSE`）：交易所标识。
- `start_date`（`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（`str`）：结束日期，格式 `YYYY-MM-DD`。
- `is_open`（`bool | None`）：是否只返回开市日。

## 返回类型

顶层返回 `list[TradingCalendarItem]`。

## 返回字段

- `exchange`（`str`）：交易所标识，如 `SSE`、`SZSE`、`BSE`、`HKEX`。
- `trade_date`（`str`）：交易日期。
- `is_open`（`bool`）：是否为开市日。

## 补充说明

- 默认 provider 候选是 `static_core -> Tushare -> AKShare emergency`。
- 主路径先读取 QuoteMux Store 的 `markets.calendar.trading`，未命中时按 Capability Matrix 并发读取完整日历。
- Runtime Profile 会按 Capability Matrix 勾选的源并发读取完整日历，再按该 capability 的 `merge_strategy` 合并成一份结果。
- 最终的 `is_open` 过滤在合并完成后再执行，因此不会因为先过滤开市日而误判缺口。
- `AKShare emergency` 只用于应急日期覆盖，不视为正式 `trade_cal` 等价物。
