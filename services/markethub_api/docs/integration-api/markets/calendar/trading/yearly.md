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

- 该接口是显式登记在 `DERIVED_CAPABILITY_BASE_IDS` 的派生视图；不独立配置 TTL、缓存策略、采集策略或更新频率。
- 执行时读取主 capability `markets.calendar.trading` 的 QuoteMux Store 和配置，再按 `start_year` / `end_year` 生成年度日期范围。
- 默认 provider 候选源继承主交易日历：`static_core -> Tushare -> AKShare emergency`。
