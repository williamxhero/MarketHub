# Store 优先与 provider 补洞统一查询计划

目标：所有公开数据 API 都遵守同一条查询规则：优先读取本地 Store 或本地事实表；如果请求时间段内 Store 数据不完整，只调用 provider 获取缺失部分；provider 结果写回 Store 后，再与已有 Store 数据合并并返回给调用者。

## 背景

MarketHub / QuoteMux 的早期约束是 provider 只用于补洞，不能成为常态查询主路径。当前代码中已经有 `time_field`、`key_fields`、`request_scope_fields`、`coverage_mode` 等 Store coverage 元数据，但执行逻辑分散在 `stocks.py`、`indexes.py`、`boards.py`、`markets.py` 等模块中，导致不同 API 对 Store、事实表和 provider 的顺序不一致。

本计划把“Store 优先 + 缺口补源 + 写回 + 合并返回”收敛为统一执行引擎。每个 API 只声明能力配置和参数映射，不再单独手写 Store miss、provider fallback、merge、writeback 流程。

## 统一规则

所有数据 API 固定按以下阶段执行：

1. 规范化请求参数，确定 `capability_id`、业务 scope、时间窗口、排序和裁剪参数。
2. 读取统一 Store，并按 capability 读取已接入的本地事实表或参考表。
3. 根据 coverage 计算请求时间段内的缺口。
4. 只针对缺口生成 provider fetch job。
5. 校验 provider 返回结果，写回 Store。
6. 按 capability 的 merge strategy 合并 Store 已有数据、本地事实表数据和 provider 新数据。
7. 最后应用 `fields`、`limit`、`offset` 等响应裁剪，并返回原 API 约定的结构。

provider 不允许收到原始完整请求后自行全量拉取；统一执行器必须只把缺口范围传给 provider。

## 现有元数据评估

当前每个 capability 已有以下 Store coverage 元数据，可作为统一引擎的基础：

- `time_field`：用于确定记录的时间轴字段，例如 `trade_time`、`trade_date`、`report_period`。
- `key_fields`：用于识别同一条业务记录，例如股票日线的 `code, trade_time, freq`。
- `request_scope_fields`：用于区分不同业务查询范围，例如 `code, freq, adjust`。
- `coverage_mode`：用于决定如何计算缺口，例如 `trading_day_range`、`minute_range`、`date_range`、`period_range`、`snapshot`。

结论：`time_field`、`key_fields`、`coverage_mode` 大体可用；`request_scope_fields` 需要系统性复核。

需要重点修正的 `request_scope_fields` 原则：

- `fields` 永远不进入 scope。
- `limit`、`offset` 通常不进入 coverage scope，只参与最终返回裁剪。
- `trade_date`、`start_date`、`end_date`、`start_time`、`end_time`、`report_period` 这类时间条件应进入时间窗口，不应混入业务 scope。
- `code`、`codes`、`freq`、`adjust`、`view`、`report_type`、`market`、`status` 等业务维度才进入 scope。
- 多代码请求应归一为可拆分 scope，避免完整 `codes` 列表成为不可复用的缓存键。

## Merge Strategy

Merge Strategy 只负责“多份数据如何合并”，不负责“是否跳过 Store 直接拉 provider”。统一执行器必须先完成 Store coverage 判断，再使用 merge strategy 合并结果。

现有策略含义：

- `append_dedupe`：追加并按 key 去重，适合行情、事件流、时间序列。
- `priority_fallback`：按 source order 优先级取结果，适合 profile、reference table 等不宜字段混拼的数据。
- `first_success`：第一个成功源直接返回。
- `freshest_wins`：同 key 冲突时选择更新时间或时间值更新的记录。
- `field_consensus`：同 key 多来源字段级合并，适合字段互补但需要冲突统计的能力。
- `raw_passthrough`：不做结构化合并。

统一引擎中 merge strategy 应用于两处：

- provider 之间返回同一缺口时如何合并。
- Store 已有数据、本地事实表数据和 provider 新补数据如何合并。

## Store Backend 设计

统一 Store 读取层需要支持多个 backend，但对执行器暴露同一结构：

- `CacheStoreBackend`：读取 `capability_cache_rows` 和 coverage。
- `FactTableStoreBackend`：读取本地事实表或参考表，例如 `fact.stock_daily_1d`、`fact.index_bar_1d`、`fact.board_daily_1d`、`fact.stock_bar_1m`、`ref.trade_calendar`、`ref.stock`。

执行器读取 Store 时不关心数据来自 cache row 还是事实表，只关心：

- 已有 records。
- 已覆盖的 scope。
- 已覆盖的时间窗口。
- 数据来源报告。

这样后续某个 capability 新增本地事实表时，只需要接入对应 backend，不需要重写 API 主流程。


## 当前本地 Backend 映射

| capability | local backend | 类型 |
| --- | --- | --- |
| `markets.calendar.trading` | `ref.trade_calendar` | reference table |
| `stocks.quotes.daily` | `fact.stock_daily_1d` | fact table |
| `stocks.quotes.intraday` | `fact.stock_bar_1m`, `fact.stock_bar_30m` | fact table |
| `stocks.quotes.daily_snapshot` | `fact.stock_daily_1d` | fact table |
| `indexes.quotes.daily` | `fact.index_bar_1d` | fact table |
| `boards.quotes.daily` | `fact.board_daily_1d` | fact table |
| `stocks.catalog`, `stocks.profile.basic`, `stocks.profile.company` | `ref.stock` | reference table / derived |
| `stocks.profile.name_history` | `ref.stock_name_history` | reference table |
| `boards.catalog`, `boards.profile` | `ref.board` | reference table |
| `boards.members`, `boards.members.history` | `ref.board_stock_membership` | reference table |
| `indexes.catalog`, `indexes.profile` | `fact.index_bar_1d` | derived |
| A2 类能力 | `capability_cache_rows` | cache only，待逐项决定是否建设事实表 |
## Coverage Mode 规则

统一引擎至少支持以下 coverage mode：

| coverage mode | 适用能力 | 缺口计算规则 |
| --- | --- | --- |
| `trading_day_range` | 股票日线、指数日线、板块日线 | 结合交易日历计算缺失交易日段 |
| `minute_range` | 股票分钟线 | 结合交易日和交易时段计算缺失 bar |
| `date_range` | 资金流、持仓、龙虎榜等日期序列 | 按日期段计算缺口 |
| `period_range` | 财务报表、财务指标 | 按报告期计算缺口 |
| `snapshot` | 快照、目录、分类 | 按单个快照 scope 判断完整性 |
| `single_record` | profile、basic 等单记录 | 无记录时补源 |
| `event_range` | 新闻、研报、公告类事件流 | 按事件时间窗口和游标规则判断缺口 |

空结果写 coverage 的规则必须统一：默认不允许空结果写完整 coverage。只有 capability 明确声明空结果代表完整，并且来源是权威源时，才允许写入完整空覆盖。盘中当天、未来日期、provider 超时、provider 异常返回都不得写完整 coverage。

## 修复优先级

### 1. 交易日历

优先治理 `markets.calendar.trading`，因为它影响所有基于交易日的缺口判断。统一引擎应优先读取 `ref.trade_calendar`，只有本地日历缺口才调用 provider。

验收：交易日历请求在 Store 或 `ref.trade_calendar` 覆盖时不调用 provider；其他行情能力计算缺口时复用同一日历结果。

### 2. 行情主链路

优先治理以下能力：

- `stocks.quotes.daily`
- `stocks.quotes.intraday`
- `indexes.quotes.daily`
- `boards.quotes.daily`
- `stocks.quotes.daily_snapshot`

原因：这些能力直接影响回测、筛选、扫描和完整性判断。

本地事实表优先级：

- 股票日线：`fact.stock_daily_1d`
- 股票分钟线：`fact.stock_bar_1m`、`fact.stock_bar_30m`
- 指数日线：`fact.index_bar_1d`
- 板块日线：`fact.board_daily_1d`

验收：请求时间段内事实表或 Store 已覆盖时 provider 不调用；有 fact/ref 的能力只缺部分交易日或分钟 bar 时，只拉缺口并 upsert fact/ref；没有 fact/ref 的能力才写回 Store。

### 3. Reference 表

治理股票、板块、指数 reference 表，减少 provider 依赖和启动冷查询波动。

优先能力：

- `stocks.catalog`
- `stocks.profile.basic`
- `stocks.profile.name_history`
- `boards.catalog`
- `boards.profile`
- `boards.members`
- `boards.members.history`
- `indexes.catalog`
- `indexes.profile`

本地参考表优先级：

- 股票：`ref.stock`、`ref.stock_name_history`
- 板块：`ref.board`、`ref.board_stock_membership`
- 指数：`ref.index`；`fact.index_bar_1d` 只保存指数行情事实

验收：本地 reference 表存在记录时不调用 provider；缺失单个 scope 时只补该 scope，并 upsert 对应 reference 表。

### 4. A2 类能力

最后治理财务、股东、公司行动、研报、龙虎榜等 A2 类能力。

原则：先决定哪些能力需要建设本地事实表。没有本地事实表前，只能统一走 Cache Store + provider 补缺；不为单个 API 手写特殊分支。

优先评估：

- `stocks.finance.*`
- `stocks.ownership.*`
- `stocks.corporate_actions.*`
- `stocks.research.*`
- `markets.connect.*`
- `markets.participants.*`
- `rankings.research.*`

验收：已有 Store coverage 时不调用 provider；Store 缺口只按缺口补源；如果新建事实表，只接入 backend，不改 API 主流程。

## 实施阶段

### 阶段 1：配置复核

- [x] 导出所有 capability 的 `time_field`、`key_fields`、`request_scope_fields`、`coverage_mode`、`default_merge_strategy`。
- [x] 复核 `request_scope_fields`，移除不应影响 coverage 的 `limit`、`offset`、分页和响应裁剪参数。
- [x] 标记多 capability API 的解析规则，例如 `/api/stocks/quotes` 按 `freq` 选择 daily 或 intraday。
- [x] 为每个 capability 标记本地 backend：cache only、fact table、reference table、derived。

### 阶段 2：统一执行器

- [x] 实现 `CapabilityQueryEngine`，固定执行 Store read、coverage plan、provider fetch、Store write、merge、project。
- [x] 实现 `CoveragePlanner`，按 coverage mode 计算缺口。
- [x] 实现统一 `StoreBackend` 接口，先接 Cache Store 和已存在事实表/参考表。
- [x] 实现统一 `ProviderRunner`，只接受缺口 fetch job。
- [x] 实现统一空结果 coverage 规则。

### 阶段 3：主链路迁移

- [x] 迁移 `markets.calendar.trading`。
- [x] 迁移股票日线、股票分钟线、指数日线、板块日线。
- [x] 迁移股票、板块、指数 reference 表。
- [x] 迁移 A2 类能力。

### 阶段 4：删除分散逻辑

- [x] 移除各业务模块中重复的 Store miss、provider fallback、merge、writeback 代码。
- [x] API 方法只保留参数规范化和响应裁剪。
- [x] 保留兼容接口返回结构，不让调用方感知内部是否命中 Store 或 provider。

## 统一验收模板

每个 capability 都必须通过以下测试：

- [x] Store 或本地事实表完整覆盖时，provider 不被调用。
- [x] Store 只有部分 coverage 时，provider 只收到缺口范围。
- [x] provider 返回后写回 Store。
- [x] 第二次同请求只读 Store，不调用 provider。
- [x] `limit`、`offset`、`fields` 不影响 coverage。
- [x] 空结果默认不写完整 coverage。
- [x] Store 路径和 provider 路径返回结构一致。
- [x] 多来源同 key 冲突按 merge strategy 合并，并记录冲突或降级状态。


## 实测结果

部署前线上基线（`deploy_20260610_072943_mh22a52aa_qmdd347d4_pkg2f66417`）：
- `GET /api/health`：`200`，`0.021s`
- `stocks.quotes.daily` 12 只、2026-05-27 到 2026-06-10：`200`，`0.064s`
- `markets.calendar.trading`：`200`，`0.042s`
- 单只日线 count=1：`200`，`0.025s`

第一次统一引擎部署后发现的问题：API 方法在确认本地覆盖前提前执行 `build_steps()`，触发 provider package 初始化，导致本地本应命中的 calendar/index/catalog/daily 批量接口重新出现 20s 超时。

修复后线上结果（`deploy_20260610_175835`）：
- `GET /api/health`：`200`，`0.0068s`
- `stocks.quotes.daily` 12 只、2026-05-27 到 2026-06-10：`200`，`0.217s`
- 单只日线 count=1：`200`，`0.051s`
- `markets.calendar.trading`：`200`，`0.131s`
- `stocks.quotes.intraday` 1m 小窗口：`200`，`0.130s`
- `indexes.quotes.daily`：`200`，`0.202s`
- `stocks.catalog`：`200`，`0.099s`

结论：统一规则部署后主链路均在亚秒级返回；外部 provider 已改为懒构建，只在统一缺口判断确认需要补源时才初始化。
