# 全市场日线回测查询优化计划

目标：让 `FirstZT` 这类回测场景稳定、快速地读取最近半年全市场日线，避免把全市场窗口拆成大量 `codes + start_date/end_date` 的批量补源请求。

## 背景

当前 `FirstZT` 的日线预筛选会调用：

`GET /api/stocks/quotes?codes=...&freq=1d&start_date=...&end_date=...`

调用形态是每批约 12 只股票，并发约 3 批。健康检查和单股小查询正常，但多股日线批量查询极慢。即使窗口缩到 14 天、批次缩到 12 只股票，仍可能 20 秒不返回。

这个场景的业务输入其实是“某个日期窗口内的全市场日线”，不是“若干只股票的兼容行情序列”。因此继续调小批次、加大超时或提高并发不能解决根因，只会改变压力形状。

## 设计目标

- 全市场日线读取优先命中本地事实表或 QuoteMux Store。
- 外部 provider 只用于补洞，不进入回测主链路的常态热路径。
- `/api/stocks/quotes` 保持兼容接口定位，继续服务单股、小批量和既有调用方。
- 回测全市场日线优先使用 `/api/stocks/quotes/daily-snapshot`，长期提供更直接的区间接口。
- 缓存路径和实时补源路径返回同一套 `StockQuoteItem` 结构，调用方不感知内部是否命中缓存。

## 当前判断

1. `/api/stocks/quotes` 的 Store identity 包含完整 `codes` 列表、日期范围、频率等参数。全市场回测按批次拼 `codes` 会产生大量不同缓存键，复用率低。
2. Store 未命中或部分命中后，`freq=1d` 会进入 capability source order 对应的 provider fallback 和合并路径。外部源慢时，请求线程会被长时间占用。
3. `/api/stocks/quotes/daily-snapshot` 已经是“按单日读取全市场日线”的正式接口，接口形态更符合回测预筛选。
4. 盘中或源端暂时无数据时，空快照不能写成完整 coverage，否则会阻止后续刷新。该问题已修复，后续改造必须保留这个约束。

## 2026-06-10 实施记录

部署前线上基线：

- `GET /api/health`：`200`，约 `0.015s`。
- `GET /api/stocks/quotes?code=600519&freq=1d&count=1`：`200`，约 `3.166s`。
- `GET /api/stocks/quotes/daily-snapshot?trade_date=2026-06-09&...`：`30s` 超时。
- `GET /api/stocks/quotes?codes=12只&freq=1d&start_date=2026-05-27&end_date=2026-06-10&...`：`30s` 超时。

确认的直接原因：线上 `fact.stock_daily_1d` 不存在，`daily-snapshot` 和批量日线 Store 未命中后只能进入外部 provider 链路。

本轮采用的最小修法：

- `QuoteMux` 日线序列和 `daily-snapshot` 在 Store miss/partial hit 后先读本地 `fact.stock_daily_1d`，只有本地仍缺口时才进入外部 provider。
- `/api/stocks/quotes` 的 `freq=1d` 兼容路径改为先读本地 `fact.stock_daily_1d`。本地已覆盖，或只缺当天/未来日线时，直接返回本地结果，不再先等待 Store exact identity 或外部 provider。
- 新增 `GET /api/stocks/quotes/daily-window`，面向回测窗口读取，直接从本地日线表分页返回 `list[StockQuoteItem]`。
- 远端从 `fact.stock_bar_1m` 按交易日窗口聚合生成 `fact.stock_daily_1d`，并建立 `(trade_date, code)` 和 `(code, trade_date)` 索引。
- 修复 Runtime 配置 JSON 并发写入使用固定 `.tmp` 文件名的问题，避免多个请求同时初始化时互相抢占 `instances.json.tmp`、`draft_policies.json.tmp`。

部署后线上验收：

| 场景 | 部署前 | 部署后 |
| --- | ---: | ---: |
| `GET /api/health` | `200`，约 `0.015s` | `200`，约 `0.020s` |
| 单股日线 `code=600519&freq=1d&count=1` | `200`，约 `3.166s` | `200`，约 `0.039s` |
| `daily-snapshot` 查询 `2026-06-09` | `30s` 超时 | `200`，约 `0.354s` |
| `daily-window` 查询 `2026-05-27` 至 `2026-06-09` | 无专用接口 | `200`，约 `4.057s` |
| 原始 12 股批量日线，结束到 `2026-06-10` | `30s` 超时 | `200`，约 `0.055s` |
| 原始 12 股批量日线，结束到 `2026-06-09` | `20s` 超时 | `200`，约 `0.059s` |

补充验证：

- `GET /api/stocks/quotes/query` 对结束到 `2026-06-10` 的盘中请求返回 `120` 行，`meta.complete=false`，每只股票缺失 `2026-06-10`；这是当天尚无收盘日线时的预期状态。
- 同一请求结束到 `2026-06-09` 时返回 `120` 行，`meta.complete=true`。
- 8 个并发原始 12 股批量日线请求全部 `200`，耗时约 `0.19s` 到 `0.48s`。
- 重启后检查最近日志，未再出现 `/data/markethub/runtime/*.json.tmp` rename 失败。

## 改造清单

### 1. 恢复本地日线源优先级

- [x] 核对远端 Runtime Profile 中 `stocks.quotes.daily` 的 source order。
- [x] 核对远端 Runtime Profile 中 `stocks.quotes.daily_snapshot` 的 source order。
- [x] 确认 `static_core` 或等价本地日线 source 位于外部 provider 前面。
- [x] 确认本地 source 失败或缺数据时，才进入 `tushare`、`efinance`、`mootdx`、`akshare` 等外部 provider。

验收：Store 未命中的单日全市场快照优先从本地数据返回，不应先等待外部 provider。

### 2. 补稳本地日线事实表

- [x] 确认线上是否存在 `fact.stock_daily_1d`。
- [x] 若不存在，建立等价本地日线表，字段至少覆盖 `code`、`trade_date`、`open`、`high`、`low`、`close`、`pre_close`、`volume`、`amount`。
- [x] 为回测主查询建立索引，优先覆盖 `(trade_date, code)`，必要时补 `(code, trade_date)`。
- [x] 若从 `fact.stock_bar_1m` 聚合生成日线，只允许按交易日窗口增量聚合，禁止对 1m 巨表做无边界全量扫描。

验收：最近半年全市场日线可以完全由本地表或 Store 返回，外部 provider 不在常态请求路径中。

### 3. 稳定 `/api/stocks/quotes/daily-snapshot`

- [x] 读取顺序固定为 `stocks.quotes.daily_snapshot` Store -> 本地日线 source -> 外部 provider。
- [x] Store miss 时按 `trade_date` 读取全市场，不需要 `code` 或 `codes`。
- [x] `limit=10000` 默认足够覆盖 A 股全市场，保留 `offset` 分页能力。
- [x] 空结果不得写入完整 coverage。
- [x] 返回字段继续使用 `StockQuoteItem`，`freq` 固定为 `1d`，`adjust` 固定为 `none`。

验收：对最近已收盘交易日调用 `daily-snapshot`，应在可接受时间内返回约 5000 条记录，并写入或命中稳定 Store。

### 4. 新增全市场日线区间接口

长期建议新增接口：

`GET /api/stocks/quotes/daily-window?start_date=...&end_date=...&fields=...&limit=...&offset=...`

接口职责：直接从本地日线表或稳定 Store 读取日期窗口内的全市场日线。它不复用 `/api/stocks/quotes` 的 per-code fallback 逻辑，也不要求调用方传 `codes`。

- [x] 定义查询参数：`start_date`、`end_date`、`fields`、`limit`、`offset`。
- [x] 返回类型保持 `list[StockQuoteItem]`。
- [x] 排序固定为 `trade_time, code`。
- [x] 文档中明确该接口面向全市场回测、筛选、统计类窗口读取。
- [x] 若需要完整性信息，另行设计 query 版本，不在首版增加混合返回结构。

验收：半年全市场日线可以通过少量分页请求读取完成，避免按股票批次请求数百次。

### 5. 保留并收窄 `/api/stocks/quotes` 的职责

- [x] `/api/stocks/quotes` 继续作为兼容行情序列接口。
- [x] 文档继续提示：按 `trade_date` 读取全市场日线时使用 `/api/stocks/quotes/daily-snapshot`。
- [x] 对 `freq=1d + codes + start_date/end_date` 的大批量请求，优先从本地日线表按 `code in (...) and trade_date between ...` 读取。
- [x] Store exact identity 命中仍可保留，但不能作为全市场回测性能的唯一依赖。

验收：既有小批量调用不回退；大批量日线请求即使 Store exact identity 未命中，也能从本地事实表快速返回。

### 6. 治理 provider fallback 阻塞

- [x] 为外部 provider 调用设置明确超时。
- [x] 为外部 provider 并发设置上限，避免冷缓存批量请求耗尽 API 执行线程。
- [x] 在运行日志中记录 Store 状态、source order、provider 耗时和返回行数。
- [ ] 对大批量冷请求优先返回可解释的缺口信息或部分结果，避免无限等待。

验收：外部源慢或不可用时，API 不应长时间无响应，也不应影响健康检查和本地命中查询。

## 推荐执行顺序

1. 先核对并修复远端 Runtime Profile，让本地日线 source 排在外部 provider 前面。
2. 补齐或恢复 `fact.stock_daily_1d`，保证最近半年全市场日线有本地底座。
3. 加固 `/api/stocks/quotes/daily-snapshot`，让它成为 `FirstZT` 短期可用的正式快路径。
4. 修改 `FirstZT` 日线预筛选，按交易日调用 `daily-snapshot`，不再拼大批量 `codes`。
5. 新增 `/api/stocks/quotes/daily-window`，把半年窗口读取压缩为少量分页请求。
6. 最后再优化 `/api/stocks/quotes` 的本地表兜底和 provider timeout，降低兼容接口的异常慢风险。

## 回归验收

- `GET /api/health` 正常。
- 单股 `GET /api/stocks/quotes?code=600519&freq=1d&count=1` 正常。
- 最近已收盘交易日的 `GET /api/stocks/quotes/daily-snapshot?trade_date=YYYY-MM-DD&fields=code,trade_time,open,high,low,close,pre_close&limit=10000` 能返回全市场记录。
- 14 天窗口回测不再卡在日线阶段。
- 半年窗口回测的日线阶段不再产生数百个 provider fallback 请求。
- `stocks.quotes.daily_snapshot` 空结果不会写入完整 coverage。
