# 主键组合增量补源策略

MarketHub 的范围型数据查询遵循同一个执行策略：先读取本地已有数据，再按稳定主键组合计算缺失部分，只把缺失主键组合映射成外源请求。

## 核心规则

- 本地已有记录直接进入结果集。
- 本地缺失记录按主键组合做差集，不按整段请求重拉。
- 外源请求只负责补缺，不覆盖本地已有记录。
- 合并结果按 capability 的稳定主键去重。
- 缓存路径和实时路径返回同一类强类型结果。

## 主键组合

不同 capability 使用自己的稳定主键组合：

- 股票行情：`code, trade_time, freq`。
- 指数行情：`index_code, trade_time, freq`。
- 板块行情：`board_code, trade_time, freq`。
- 资金流：`code, trade_date, view` 或 `board_code, trade_date, scope`。
- 事件流：`event_id`。

## 股票 30m 行情

股票 30m 行情的预期主键组合是：

```text
code + 30m bar time + freq
```

查询时先读 `fact.stock_bar_30m`。如果本地 30m 表缺少某些 bar，则继续用本地 1m 表聚合补本地缺口。仍然缺失的 bar 所在日期才会进入外源补缺。

## 调用方契约

调用方不需要猜测本次结果是否完整。强契约接口会返回：

```text
expected_bar_count
actual_bar_count
missing_trade_dates
missing_trade_times
complete
```

扫描类任务只应使用 `complete=true` 的 code。
