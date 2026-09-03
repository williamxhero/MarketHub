# 中国股票当前交易周期 Bar API

MarketHub 将股票当前交易周期 Bar 合并在既有历史行情资源中，不另设“实时价格”接口：

`GET /api/stocks/quotes?code=600519&freq=1m&datetime=now&count=1`

这里返回的是当前 1 分钟或 30 分钟周期内持续变化的 OHLCVA（开、高、低、收、成交量、成交额），不是某一时点的 last-price quote。`close` 表示本次观测时该周期内的最新价格。

## 请求约束

`datetime=now` 当前采用以下固定约束：

- `freq` 只支持 `1m` 和 `30m`。
- `count` 只支持 `1`；省略时按 `1` 处理。
- `adjust` 只支持 `none`。
- 不能同时传 `trade_date`、`start_date`、`end_date`、`start_time` 或 `end_time`。
- `code` 与 `codes` 至少传一个；当前部署还会应用实时股票 allowlist。

历史查询保持原有参数语义。例如，查询已经完成的某天或某段时间时不要传 `datetime=now`，MarketHub 会从 QuoteMux 的历史事实数据集中读取。

## 1 分钟示例

请求：

```http
GET /api/stocks/quotes?code=600519&freq=1m&datetime=now&count=1
```

响应结构：

```json
{
  "items": [
    {
      "code": "600519",
      "trade_time": "2026-09-03T13:30:00+08:00",
      "freq": "1m",
      "open": 1400.0,
      "high": 1401.0,
      "low": 1399.0,
      "close": 1400.5,
      "volume": 1200.0,
      "amount": 1680600.0,
      "adjust": "none",
      "interval_start": "2026-09-03T13:30:00+08:00",
      "interval_end": "2026-09-03T13:31:00+08:00",
      "is_final": false,
      "observed_at": "2026-09-03T13:30:08+08:00",
      "last_trade_at": "2026-09-03T13:30:07+08:00",
      "provider": "mootdx",
      "source_semantics": "native",
      "observation_version": "42",
      "freshness_ms": 3000,
      "degraded": false,
      "market_status": "trading"
    }
  ],
  "meta": {
    "effective_now": "2026-09-03T13:30:11+08:00",
    "historical_dataset_version": ""
  },
  "errors": [],
  "diagnostics": []
}
```

价格和成交字段均描述 `[interval_start, interval_end)` 交易周期。周期未结束时值会变化，且 `is_final=false`；调用方不得把该版本当成已经完成的 K 线。

## 新鲜度

盘中 1m Bar 的 `observed_at` 不得早于请求时间 `meta.effective_now` 5 分钟以上。`freshness_ms` 是两者的毫秒差：

- 正常刷新：`degraded=false`。
- provider 刷新失败，但缓存观测仍不超过 5 分钟：可以返回，`degraded=true`。
- 没有符合当前周期和 5 分钟门槛的数据：返回 HTTP 503，不会把上一周期 Bar 冒充当前 Bar。

`last_trade_at` 表示 provider 能确认的最近成交时间，与 `observed_at` 的含义不同。前者回答“最后一笔成交何时发生”，后者回答“这一版本行情何时被观测”。

## 30 分钟语义

`GET /api/stocks/quotes?code=600519&freq=30m&datetime=now&count=1`

MarketHub 按以下顺序返回当前 30m 周期：

1. 优先使用 provider 原生 30m Bar，`source_semantics=native`。
2. 原生条不可用时，只允许由当前 30m 周期起点至当前时刻所有已过分钟的完整 1m 前缀聚合，`source_semantics=derived`，provider 为 `derived_core`。
3. 任一已过分钟缺失时返回 HTTP 503 `LIVE_BAR_DATA_INCOMPLETE`。

系统不会用不完整分钟拼接 30m，也不会制造无成交 OHLCVA。当前周期的派生 Bar 仍为 `is_final=false`。

## 从当前 Bar 到历史事实

成功返回的当前 Bar 已进入 QuoteMux 的持久化实时层，但不等于尚未完成的 Bar 已写入历史 fact：

1. provider 尝试写入 `live.stock_bar_provider_attempt`。
2. 每次观测写入 `live.stock_bar_observation`。
3. 当前选中版本写入 `live.stock_bar_selected`，状态为 staged。
4. 周期完成后，finalizer 将最终版本写入 `fact.stock_bar_1m` 或 `fact.stock_bar_30m`，并把 live 记录标记为 finalized。
5. 后续普通历史查询通过同一个 `/api/stocks/quotes` 资源读取该事实数据。provider 事后修正会更新对应 fact，并留下审计/迁移记录。

因此，正确表述是：“实时请求成功后数据已经持久化到 QuoteMux；完成周期沉淀到 fact 后，才成为历史 K 线。”最终事实仍允许 provider 事后修正，但修正过程有审计记录。

## 字段判读

- `interval_start` / `interval_end`：Bar 的左闭右开交易周期。
- `is_final`：是否已完成并固化为历史事实。
- `provider`：实际选中数据源。
- `source_semantics`：`native` 为原生周期，`derived` 为完整 1m 前缀派生。
- `observation_version`：观测版本；当前周期内容变化时版本会变化。
- `market_status`：查询锚点对应的 `trading`、`recess`、`preopen` 或 `closed`。
- `errors`：按股票记录的局部错误；整体不可用时接口返回 503。
- `diagnostics`：validator 或 fallback 诊断，不改变主结果本身的 OHLCVA 语义。

## 健康检查

`GET /api/health` 的 live-bar readiness 会给出：

- 支持频率 `1m`、`30m`。
- worker deadline。
- primary、fallback、validator provider。
- 每个频率的 staged、failed 和最近选中时间。
- finalizer 是否 ready，以及是否存在 overdue staged Bar。

当前交易周期尚未结束而处于 staged 是正常状态；只有周期结束后仍 overdue，或出现 failed，才表示 finalizer 异常。

## 常见错误

- HTTP 422：`datetime=now` 与不支持的频率、复权、count 或时间范围组合。
- HTTP 503 `LIVE_INGEST_UNAVAILABLE`：实时 provider/worker 不可用，或没有满足当前周期与新鲜度要求的数据。
- HTTP 503 `LIVE_BAR_DATA_INCOMPLETE`：30m 派生所需的完整 1m 前缀缺失。
- HTTP 503 `LIVE_CLOCK_UNHEALTHY`：服务时钟不满足实时行情安全门槛。

非连续竞价时段不应把实时 provider 的 503 单独判定为盘中行情故障；应结合 `market_status`、交易日历和 `/api/health` 判断。
