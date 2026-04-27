# /api/markets/calendar/trading/yearly

`GET` 返回指定年份区间的交易日历汇总。

## 查询参数

- `exchange`（类型：`str`；默认：`SSE`）：交易所标识。
- `start_year`（类型：`int`；默认：`2024`；范围：`1990-2100`）：起始年份。
- `end_year`（类型：`int`；默认：`2026`；范围：`1990-2100`）：结束年份。

## 返回类型

顶层返回 `list[TradingCalendarItem]`。

## 返回字段

- `exchange`（`str`）：交易所标识，如 `SSE`、`SZSE`、`BSE`、`HKEX`。
- `trade_date`（`str`）：交易日日期。
- `is_open`（`bool`）：是否为开市日。

## 补充说明

- 默认 provider 候选是 `static_core -> Tushare -> AKShare emergency`。
- 主路径先读取 QuoteMux Store 的 `markets.calendar.trading.yearly`，未命中时按 Capability Matrix 并发读取年度区间日历。
- Runtime Profile 会按 Capability Matrix 勾选的源并发读取年度区间日历，再按该 capability 的 `merge_strategy` 合并后返回。
