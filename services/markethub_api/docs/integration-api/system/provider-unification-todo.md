# Provider 统一规则改造 TODO

目标：所有对外数据 API 统一执行 `Store 缓存 -> SourceInstanceExecutor 按 capability source_order 调 source package handler -> 写回 Store`。接口实现不直接写死 provider，不假定某个 provider 一定存在，也不把本地事实表伪装成默认 source。

## 当前规则

1. 预处理：构造稳定的 `store_identity`，读取 QuoteMux Store。
2. 核心执行：Store 未命中或部分命中时，通过 `SourceInstanceExecutor` 和 `run_fallback_chain_with_report` 调用 Runtime Profile 配置出的 source instance。
3. 后处理：按 capability 主键合并、排序、分页、写回 Store，并返回稳定模型。
4. handler 选择只来自 source package manifest；QuoteMux 主逻辑不得写 `{"tushare": ...}[instance.package_id]` 这类 provider map。
5. `datalake` source 已废弃，不再作为 source package、默认候选源或隐藏 fallback。

## 已完成

- [x] `/api/stocks/catalog`：已恢复为普通 capability 链路，不再特殊优先本地 `ref.stock`；Store miss 后通过 `stocks.catalog` 的 source instance 获取。
- [x] 股票、指数、板块、市场、排行主 API：已移除 QuoteMux 主逻辑中的 provider map，统一通过 source package registry handler 调用。
- [x] `/api/stocks/quotes` 和 `/api/stocks/quotes/query`：已移除 `datalake` 默认候选源和 30m 本地事实表写回后处理；交易日历缺口判断改为走 `markets.calendar.trading` 主 capability。
- [x] `/api/stocks/quotes/daily-snapshot`：已移除本地 `datalake_ref.get_stock_active_codes` 补全逻辑；全市场快照只由支持 `stocks.quotes.daily_snapshot` 的 source package 提供。
- [x] `/api/stocks/{code}/indicators/money-flow`：已移除 `datalake` 默认候选源，按 `stocks.indicators.money_flow` 的 source instance 获取。
- [x] `/api/indexes/quotes`：已移除 `datalake` 默认候选源；缺口交易日历改为走 `markets.calendar.trading`。
- [x] `/api/indexes/{index_code}/members`：已移除本地股票名称补全后处理，provider 返回即为最终成分数据。
- [x] `/api/markets/events/news`：按既定设计保留为 news store 读取，但已移除对 `quotemux.sources.datalake.news` 的依赖。
- [x] `QuoteMux_Packages`：已移除 `quotemux_packages.datalake` 注册。
- [x] `QuoteMux/src/quotemux/sources/datalake`：已删除废弃 source 目录。

## 设计内例外

- `/api/markets/calendar/trading/previous`：这是 `/api/markets/calendar/trading` 的窗口函数，不需要独立 Store 或 provider。
- `/api/markets/calendar/trading/next`：这是 `/api/markets/calendar/trading` 的窗口函数，不需要独立 Store 或 provider。
- `/api/markets/calendar/trading/yearly`：这是 `/api/markets/calendar/trading` 的窗口函数，不需要独立 Store 或 provider。
- `/api/markets/events/news`：当前设计就是只从 news store 获取，不走外部 provider 链。

## 待改造清单

- [x] `QuoteMuxDatasets.fetch_*` 数据集辅助入口：已删除未使用的直接 Tushare / OpenTDX 辅助入口，保留入口不再在 datasets 层硬编码 provider client。
- [x] `MarketHub` / `QuoteMux` 配置中的 `DATALAKE_ROOT`、`DL_DB_*`、`datalake_db` 命名：已迁移为 `MARKETHUB_DATA_ROOT`、`MARKETHUB_DB_*`、`store_db`，并同步诊断与文档命名。
- [x] `test_smoke.py` 中旧 provider monkeypatch 测试：已删除绑定 `qm_stocks._tushare_provider`、`qm_boards._datalake` 等旧内部变量的白盒用例，保留服务接口黑盒测试。

## 后续改造原则

- 新增或修复 API 时，先看 `capabilities/inventory.py` 是否已有 capability 定义和默认 source order。
- 如果 provider 需要支持某 capability，只改 source package manifest 和 handler，不在 QuoteMux 主逻辑新增 provider 分支。
- 如果能力只能从本地 Store 获取，要明确写成 Store-only 设计例外；不要包装成不存在的 provider。
