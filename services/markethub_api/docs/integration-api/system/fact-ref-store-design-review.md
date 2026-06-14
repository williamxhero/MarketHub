# Fact/Ref Store 设计复核与改进建议

## 目标

复核 MarketHub 当前事实表、参考表和 Cache Store 的职责边界，明确哪些表应该保留，哪些表需要补齐，哪些能力暂时只走 Cache Store + provider 补洞即可。

统一查询规则保持不变：

```text
本地已有数据 = Cache Store + 已接入的 fact/ref 表
缺口 = 请求范围 - 本地已有数据
provider 只补缺口
如果 capability 有 fact/ref，provider 结果 upsert fact/ref
如果 capability 没有 fact/ref，provider 结果写回 Cache Store
返回 = 本地已有数据 + provider 新数据
```

fact/ref 表不是这条规则之外的第二套逻辑，而是“本地已有数据”的更稳定来源。

## 实现状态

本轮修复已把文档中的核心规则落到代码和部署脚本中：

- `execute_capability_query()` 支持 fact/ref writer。有 fact/ref writer 的 capability 不再把 fact/ref 完整命中结果写入 Cache Store。
- provider 补洞后，主链路 capability 的新增或修正结果会 upsert 对应 fact/ref 表；没有 fact/ref writer 的能力仍走 Cache Store。
- 已补部署 schema：`fact.stock_daily_1d`、`fact.index_bar_1d`、`fact.board_daily_1d`、`ref.stock`、`ref.stock_name_history`、`ref.board`、`ref.board_stock_membership`、`ref.index`。
- 已补 diagnostics：`GET /api/diagnostics/fact-ref` 和 admin runtime health 中的 `fact_ref_availability`，检查主链路表、关键索引和覆盖范围。
- 已统一交易日历读取的 `SSE/SHSE/SZSE/BJSE` 映射；部署脚本继续用权威交易日历源刷新 `ref.trade_calendar`。
- 指数 catalog/profile 的本地来源已从 `fact.index_bar_1d` 派生改为 `ref.index`。

仍需单独治理的数据工程任务：

- 为新建的 `ref.stock`、`ref.index`、`ref.board`、`ref.board_stock_membership`、`fact.index_bar_1d`、`fact.board_daily_1d` 做历史全量导入和增量任务。
- 扩展 `fact.stock_daily_1d` 的历史覆盖，支持一年或多年回测。
- 继续评估 `fact.stock_bar_1m` / `fact.stock_bar_30m` 的分区、压缩和刷新策略。
- A2 类能力仍保持 Cache Store + provider 补洞，只有达到高频/批量/回测依赖条件后再建事实表。

## 当前远端真实状态

以下状态来自 `yosef-server` 上 `datalake_dev` 的实际表检查。

### 已存在且有数据

| 表 | 当前行数 | 覆盖范围 | 判定 |
| --- | ---: | --- | --- |
| `fact.stock_bar_1m` | 约 1,272,652,500 | `2022-01-04 09:31:00` 到 `2026-06-09 15:00:00` | 保留，分钟线核心底座 |
| `fact.stock_bar_30m` | 约 54,915,282 | `2022-01-04 09:30:00` 到 `2026-06-09 15:00:00` | 保留，但定义为 1m 派生加速表 |
| `fact.stock_daily_1d` | 约 684,633 | `2025-12-01` 到 `2026-06-09` | 保留，回测和日线主链路底座 |
| `ref.trade_calendar` | 8,797 | `1990-12-19` 到 `2026-12-31` | 保留，但需要改成权威日历源 |

### 本轮修复前：代码已接入但远端未落表

| 表 | 当前问题 | 判定 |
| --- | --- | --- |
| `fact.index_bar_1d` | 代码已作为指数日线本地 backend 使用，但远端表不存在 | 应建设 |
| `fact.board_daily_1d` | 代码已作为板块日线本地 backend 使用，但远端表不存在 | 应建设 |
| `ref.stock` | 代码已作为股票 catalog/basic/profile 本地 backend 使用，但远端表不存在 | 必须建设 |
| `ref.stock_name_history` | 代码已作为 name history 本地 backend 使用，但远端表不存在 | 建议建设 |
| `ref.board` | 代码已作为板块 catalog/profile 本地 backend 使用，但远端表不存在 | 应建设 |
| `ref.board_stock_membership` | 代码已作为板块 members/history 本地 backend 使用，但远端表不存在 | 应建设 |
| `ref.index` | 当前没有，指数 catalog/profile 临时从 `fact.index_bar_1d` 派生 | 应新增 |

### 空表

| 表 | 当前状态 | 判定 |
| --- | --- | --- |
| `fact.news_event` 及相关表 | 存在但为空 | 保留为新闻事件能力预留；不纳入行情主链路事实表验收 |

## 事实表是否应该取消

不建议取消事实表。

Cache Store 和 fact/ref 表职责不同：

| 层 | 职责 | 适用场景 |
| --- | --- | --- |
| `capability_cache_rows` | API 结果缓存，保存 provider 补洞后的 API 形态数据 | 长尾能力、provider 补洞沉淀、没有事实表的 A2 能力 |
| `fact.*` | 标准化事实数据底座 | 高频行情、全市场扫描、回测、批量查询、覆盖率治理 |
| `ref.*` | 标准化参考/维表 | 日历、股票目录、指数目录、板块目录、成分关系 |

只用 Cache Store 会带来几个问题：

- 冷启动差：没有请求过的 scope 不会有 cache。
- 全市场/长窗口复用差：同一份数据会因为不同 `codes`、窗口、分页方式被重复缓存。
- coverage 风险更高：Cache 更依赖 coverage 元数据；fact/ref 可以按真实行和交易日重新算缺口。
- 数据治理弱：批量导入、索引、去重、质量检查、覆盖统计都更适合 fact/ref。
- 回测不稳：回测需要稳定的全市场底座，不适合把 API cache 当主数据仓库。

因此建议继续保持分工：

```text
fact/ref 表：核心本地数据底座
Cache Store：API 结果缓存 + provider 补洞沉淀
provider：只补本地缺口
```

## 逐表判定

### `fact.stock_bar_1m`

保留。

这是股票分钟线底座，数据量大、查询高频、provider 不适合作为常态路径。后续优化重点不是取消，而是治理物理结构：分区或 Timescale hypertable、压缩、索引、按日期导入和覆盖检查。

### `fact.stock_bar_30m`

保留，但明确为派生加速表。

30m 可由 1m 聚合，但在线查询时从 1m 长窗口聚合成本太高。`fact.stock_bar_30m` 应定义为由 1m 刷新的派生事实表，不作为独立权威源。

### `fact.stock_daily_1d`

保留。

股票日线是回测、预筛选、daily snapshot、daily-window 的核心表。当前覆盖只有 `2025-12-01` 到 `2026-06-09`，够当前半年回测，但不足以支撑一年或多年回测。后续应扩展历史覆盖。

### `ref.trade_calendar`

保留，但需要调整来源和 exchange 语义。

交易日历影响所有基于交易日的缺口判断。当前部署脚本有从分钟线推导日历的逻辑，这不应作为长期权威来源。应使用权威交易日历源维护，并覆盖未来交易日和休市日。

同时当前读取函数硬编码 `SHSE`，API 层使用 `SSE`。需要统一 `SSE/SHSE/SZSE/BJSE` 映射，避免请求参数和底层表语义不一致。

### `fact.index_bar_1d`

应该建设。

指数日线是行情主链路，不能长期依赖 Cache/provider。当前代码已接入该表，但远端表不存在，需要补 migration、导入、增量更新和覆盖检查。

### `ref.index`

应该新增。

当前指数 catalog/profile 从 `fact.index_bar_1d` 派生，这是临时方案。指数名称、发布方、分类、市场、状态等 metadata 应放在 `ref.index`，`fact.index_bar_1d` 只存行情事实。

### `ref.stock`

必须建设。

股票 catalog、basic、profile、活跃股票列表都依赖它。没有 `ref.stock`，reference 请求会冷启动走 provider，回测的股票 universe 也缺少稳定依据。

### `ref.stock_name_history`

建议建设。

如果 name history API 要满足本地优先，则需要该表。优先级低于 `ref.stock`，但不应在未落表时把它标成已部署 backend。

### `ref.board` 与 `ref.board_stock_membership`

应该建设。

板块 catalog/profile/members/history 和板块日线都依赖稳定的板块 universe 与成分关系。没有这两张表，板块 reference 会依赖 provider，板块日线也缺少稳定关联基础。

### `fact.board_daily_1d`

应该建设。

板块日线是行情主链路。当前代码已接入但远端没有表。建设前应先明确 `ref.board` 和板块代码体系，再导入或采集板块日线。

## 需要调整的设计点

### 1. 区分“已部署表”和“目标表”

状态：已在文档和 diagnostics 中区分；部署脚本已补目标表 schema。历史数据导入仍需独立执行。

`unified-store-provider-query-plan.md` 中的 backend 映射需要拆成两列：

- 当前已部署 backend
- 目标 backend

不能把 `fact.index_bar_1d`、`fact.board_daily_1d`、`ref.stock` 等未落表对象写成当前已可用能力。

### 2. 加本地 backend availability 检查

状态：已实现 `GET /api/diagnostics/fact-ref`，并接入 admin runtime health。

主链路 fact/ref 表缺失不应该被静默吞掉。

当前 `query_dataframe()` 遇到表不存在会打印错误并返回空 DataFrame，业务层会误以为“本地无数据”，然后走 provider。这会掩盖 schema 漂移。

建议增加启动或 health diagnostics：

- 检查主链路表是否存在。
- 检查关键索引是否存在。
- 检查最近覆盖日期。
- 对缺失表返回显式 warning，而不是只在请求时退化到 provider。

### 3. 目标写入策略：有 fact/ref 的能力不再重复写 Cache

状态：已实现。统一查询引擎以 `fact_ref_writer` 判定写入目标；有 writer 时完整命中直接返回，provider 补洞只 upsert fact/ref，不写 Cache Store。

目标策略按 capability 是否有 fact/ref backend 区分。

有 fact/ref 且 fact/ref 完整覆盖：

```text
fact/ref 完整覆盖 -> 直接返回 -> 不写 Cache Store
```

有 fact/ref 但仍有缺口：

```text
fact/ref 已有部分 -> provider 只补缺口 -> provider 结果 upsert fact/ref -> 合并返回
```

没有 fact/ref 的能力：

```text
Cache Store 命中 -> 直接返回
Cache Store 缺口 -> provider 只补缺口 -> provider 结果写 Cache Store -> 合并返回
```

这意味着：主链路 capability 一旦建设了事实表或参考表，provider 补洞结果的长期落点应该是对应 fact/ref 表，而不是只写 API Cache。Cache Store 继续服务没有事实表的长尾能力，以及必要的 provider 补洞缓存。

适合 upsert fact/ref 的能力：

- `stocks.quotes.daily`
- `stocks.quotes.intraday`
- `indexes.quotes.daily`
- `boards.quotes.daily`
- `markets.calendar.trading`
- `stocks.catalog`
- `boards.catalog`
- `boards.members`
- `indexes.catalog`

这样事实表缺口才会真正愈合，而不是只在 API cache 中愈合。

### 4. fact/ref 完整命中时不必大量写 Cache

状态：已实现。有 fact/ref writer 的 capability 在完整命中时不会调用 `store_result()`。

如果请求完全由 fact/ref 满足，通常不需要再把同一批 API 结果写入 `capability_cache_rows`。否则会造成重复存储。

建议：

- 有 fact/ref 且完整命中时只返回，不写 Cache。
- 有 fact/ref 但存在缺口时，provider 补洞结果 upsert fact/ref。
- 没有 fact/ref 的 A2 能力继续写 Cache。
- 除非某个 capability 明确需要 API 形态缓存，否则不要把 fact/ref 完整命中的结果复制到 Cache Store。

### 5. A2 类能力不要急着全建事实表

财务、股东、公告、研报、龙虎榜等 A2 能力先继续走：

```text
Cache Store -> provider 补洞 -> 写 Cache Store -> 合并返回
```

只有当某个 A2 能力满足以下条件时，再建设事实表：

- 高频查询。
- 批量扫描。
- 回测或策略依赖。
- 数据量大且 provider 慢或不稳定。
- 需要独立质量检查和覆盖率治理。

## 建议优先级

1. [x] 修正文档，把“当前已部署 backend”和“目标 backend”拆开。
2. [x] 增加 fact/ref availability diagnostics，主链路缺表必须显式告警。
3. [x] 建设 `ref.stock` schema 和 provider upsert writer；历史全量导入另做。
4. [x] 把 `ref.trade_calendar` 改成权威日历源，并统一 exchange 映射。
5. [x] 建设 `fact.index_bar_1d` 和 `ref.index` schema 和 provider upsert writer；历史全量导入另做。
6. [x] 建设 `ref.board`、`ref.board_stock_membership`、`fact.board_daily_1d` schema 和 provider upsert writer；历史全量导入另做。
7. [ ] 优化 `fact.stock_bar_1m` 和 `fact.stock_bar_30m` 的物理结构、刷新策略和覆盖检查。
8. [ ] 再逐项评估 A2 类能力是否需要事实表。

部署验收状态：`yosef-server` 当前所有目标表和关键索引已存在，`GET /api/diagnostics/fact-ref` 返回 `status=ok`。新建的 `ref.stock`、`ref.index`、`ref.board`、`ref.board_stock_membership`、`fact.index_bar_1d`、`fact.board_daily_1d` 仍为空表，后续需要独立导入任务补历史底座。

## 当前结论

事实表方向合理，不应该取消。

当前真正合理且已落地的是股票分钟线、30m 派生线、股票日线、交易日历这几类。需要调整的是：代码和文档已经把指数、板块、股票/板块 reference 表当作本地 backend，但远端并没有这些表。下一步应补齐主链路 fact/ref 表，或在补齐前把文档和 diagnostics 明确标为目标状态而非当前状态。


## ??????

- `fact.stock_daily_1d` ?? `is_suspended boolean not null default false`?
- `fact.stock_daily_1d` ?? `is_st boolean not null default false`?
- ?????????????????? provider ??????????????
- `skip_suspended` ? `skip_st` ?????????? API???????????????

### 股票日线停牌占位

`fact.stock_daily_1d.is_suspended=true` 表示该行是停牌日线，来源可以是 provider 明确返回的停牌状态，也可以是 QuoteMux 在历史交易日补洞时基于前一个交易日生成的停牌占位。占位行只用于补齐时间轴：`open/high/low/close` 等于前一个交易日 `close`，`volume=0`，`amount=0`，`is_st` 沿用前一个交易日。
