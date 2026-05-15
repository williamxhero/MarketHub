# Capability 更新策略建议

盘点日期：2026-05-15

## 结论

当前策略是所有普通 API capability 均接入 Store。高复用、公共范围、全市场或适合预计算的 capability 打开定时更新；个股级、低频、重型数据默认只开缓存 TTL。派生能力也以自身 capability 写入 Store，默认 provider 统一为 `derived_core`，QuoteMux 只负责转发。

Task Center 已注册每小时调用一次 `/api/admin/capture/run-due-async`。打开定时更新后，capture 仍只会按 capability 自身的日、周、月、年到期规则运行；每小时任务只是负责检查是否到期。

## 规则

- `定时更新`：后台预采集。必须同时保持缓存开启，否则 capture 预检查会跳过。
- `TTL only`：不预采集，请求命中后写入缓存，后续请求在 TTL 内读缓存。
- `暂不配置`：当前默认表不再保留该状态；新增 capability 如普通 API 尚未接入 Store，才临时使用。
- TTL 建议里的 `-1` 表示永不过期。只适合历史稳定或静态定义类数据。
- 当前 Console 的定时选项只表达周期：无、每天、每周日、每月最后一天、每年最后一天；不要把它理解为精确盘后执行时间。

## 逐条建议

| Capability | API | 当前动作 | 定时选项 | TTL 建议 | 普通 API 读 Store | 理由 |
| --- | --- | --- | --- | --- | --- | --- |
| `boards.catalog` | `/api/boards/catalog` | 定时更新 | 每月最后一天 | 365 天 | 是 | 板块目录全量小，适合低频刷新。 |
| `boards.indicators.money_flow` | `/api/boards/{board_code}/indicators/money-flow` | 定时更新 | 每天 | 180 天 | 是 | 板块资金流已接入 Store，适合每天更新。 |
| `boards.indicators.money_flow.snapshot` | `/api/boards/indicators/money-flow` | 定时更新 | 每天 | 180 天 | 是 | 全市场板块资金流已接入 Store，适合每天更新。 |
| `boards.members` | `/api/boards/{board_code}/members` | 定时更新 | 每周日 | 365 天 | 是 | 板块成分低频变化，周更足够。 |
| `boards.members.history` | `/api/boards/{board_code}/members/history` | TTL only | 无 | 365 天 | 是 | 历史查询低频，按需缓存即可。 |
| `boards.profile` | `/api/boards/{board_code}/profile` | TTL only | 无 | 365 天 | 是 | 单板块画像低频且按需访问。 |
| `boards.quotes.daily` | `/api/boards/quotes` | 定时更新 | 每天 | 30 天 | 是 | 板块日线高复用，适合每日补最近窗口。 |
| `boards.reference.categories` | `/api/boards/reference/categories` | 定时更新 | 每月最后一天 | -1 | 是 | 分类定义近似静态，月更用于吸收调整。 |
| `indexes.catalog` | `/api/indexes/catalog` | 定时更新 | 每月最后一天 | 365 天 | 是 | 指数目录低频变化，月更即可。 |
| `indexes.members` | `/api/indexes/{index_code}/members` | 定时更新 | 每周日 | 365 天 | 是 | 指数成分有周期调整，周更比日更更合适。 |
| `indexes.profile` | `/api/indexes/{index_code}/profile` | TTL only | 无 | 365 天 | 是 | 单指数画像低频，按需缓存即可。 |
| `indexes.quotes.daily` | `/api/indexes/quotes` | 定时更新 | 每天 | 30 天 | 是 | 指数日线是核心行情。 |
| `markets.calendar.trading` | `/api/markets/calendar/trading` | 定时更新 | 每月最后一天 | -1 | 是 | 交易日历是基础依赖，月更未来窗口。 |
| `markets.calendar.trading.next` | `/api/markets/calendar/trading/next` | TTL only | 无 | 30 天 | 是 | 可由交易日历快速派生，没必要单独预采集。 |
| `markets.calendar.trading.previous` | `/api/markets/calendar/trading/previous` | TTL only | 无 | 365 天 | 是 | 历史结果稳定，可按需缓存。 |
| `markets.calendar.trading.yearly` | `/api/markets/calendar/trading/yearly` | TTL only | 无 | 3650 天 | 是 | 年度视图可由交易日历派生，首次请求缓存即可。 |
| `markets.connect.active_top10` | `/api/markets/connect/active-top10` | 定时更新 | 每天 | 180 天 | 是 | 互联互通活跃榜是日频市场事件。 |
| `markets.connect.capital_flow` | `/api/markets/connect/capital-flow` | 定时更新 | 每天 | 180 天 | 是 | 互联互通资金流是日频市场数据。 |
| `markets.connect.quotas` | `/api/markets/connect/quotas` | 定时更新 | 每天 | 180 天 | 是 | 额度数据随交易日更新。 |
| `markets.events.block_trades` | `/api/markets/events/block-trades` | 定时更新 | 每天 | 180 天 | 是 | 大宗交易是日频市场事件。 |
| `markets.events.news` | `/api/markets/events/news` | 定时更新 | 每天 | 30 天 | 是 | 新闻事件入口高复用，适合每日同步最近交易日。 |
| `markets.indicators.main_capital_flow` | `/api/markets/indicators/main-capital-flow` | 定时更新 | 每天 | 180 天 | 是 | 市场主力资金是日频数据。 |
| `markets.participants.dragon_tiger` | `/api/markets/participants/dragon-tiger` | 定时更新 | 每天 | 180 天 | 是 | 龙虎榜是日频市场事件。 |
| `markets.participants.dragon_tiger.institutions` | `/api/markets/participants/dragon-tiger/institutions` | 定时更新 | 每天 | 180 天 | 是 | 机构席位是日频市场事件。 |
| `markets.participants.hot_money` | `/api/markets/participants/hot-money` | 定时更新 | 每月最后一天 | 365 天 | 是 | 游资名录低频变化，月更即可。 |
| `markets.participants.hot_money.details` | `/api/markets/participants/hot-money/details` | 定时更新 | 每天 | 180 天 | 是 | 游资明细是日频市场事件。 |
| `markets.trading.open_auctions` | `/api/markets/trading/open-auctions` | 定时更新 | 每天 | 30 天 | 是 | 开盘竞价是日频交易数据，已接入 Store。 |
| `markets.trading.sessions` | `/api/markets/trading/sessions` | TTL only | 无 | -1 | 是 | 交易时段定义近似静态，首次请求缓存即可。 |
| `rankings.research.broker_monthly_picks` | `/api/rankings/research/broker-monthly-picks` | 定时更新 | 每周日 | 180 天 | 是 | 月度金股可能月内修订，周更比月末更稳。 |
| `rankings.research.reports` | `/api/rankings/research/reports` | 定时更新 | 每天 | 90 天 | 是 | 研报排行是公共日频列表。 |
| `stocks.catalog` | `/api/stocks/catalog` | 定时更新 | 每月最后一天 | 365 天 | 是 | 股票目录全量小，月更覆盖上市退市变化。 |
| `stocks.catalog.archive` | `/api/stocks/catalog/archive` | 定时更新 | 每月最后一天 | 365 天 | 是 | 股票目录归档已接入 Store，月更即可。 |
| `stocks.corporate_actions.dividends` | `/api/stocks/{code}/corporate-actions/dividends` | TTL only | 无 | 365 天 | 是 | 个股公司行为低频且按需查询，预跑全市场成本高。 |
| `stocks.corporate_actions.repurchases` | `/api/stocks/{code}/corporate-actions/repurchases` | TTL only | 无 | 365 天 | 是 | 个股回购低频，按需缓存即可。 |
| `stocks.corporate_actions.rights_issues` | `/api/stocks/{code}/corporate-actions/rights-issues` | TTL only | 无 | 365 天 | 是 | 配股事件低频，按需缓存即可。 |
| `stocks.corporate_actions.share_changes` | `/api/stocks/{code}/corporate-actions/share-changes` | TTL only | 无 | 365 天 | 是 | 股本变动低频，预跑全市场成本高。 |
| `stocks.corporate_actions.unlock_schedules` | `/api/stocks/{code}/corporate-actions/unlock-schedules` | TTL only | 无 | 365 天 | 是 | 解禁安排按个股查询，按需缓存即可。 |
| `stocks.factors.adj` | `/api/stocks/{code}/factors/adj` | TTL only | 无 | 365 天 | 是 | 复权因子按个股窗口使用，按需缓存即可。 |
| `stocks.factors.technical` | `/api/stocks/{code}/factors/technical` | 定时更新 | 每天 | 30 天 | 是 | 默认由 `derived_core` 基于日线行情派生，并以自身 capability 写入 Store。 |
| `stocks.finance.audits` | `/api/stocks/{code}/finance/audits` | TTL only | 无 | 365 天 | 是 | 审计意见低频，按需缓存即可。 |
| `stocks.finance.disclosure_dates` | `/api/stocks/{code}/finance/disclosure-dates` | TTL only | 无 | 180 天 | 是 | 披露日期有阶段性更新，按需缓存即可。 |
| `stocks.finance.express` | `/api/stocks/{code}/finance/express` | TTL only | 无 | 180 天 | 是 | 业绩快报按个股访问，预跑全市场成本高。 |
| `stocks.finance.forecasts` | `/api/stocks/{code}/finance/forecasts` | TTL only | 无 | 180 天 | 是 | 业绩预告按个股访问，按需缓存即可。 |
| `stocks.finance.indicators` | `/api/stocks/finance/indicators` | TTL only | 无 | 365 天 | 是 | 财务指标数据重，先按需缓存；后续如要建设财务宽表再周更。 |
| `stocks.finance.main_business` | `/api/stocks/{code}/finance/main-business` | TTL only | 无 | 365 天 | 是 | 主营构成低频，按需缓存即可。 |
| `stocks.finance.statements` | `/api/stocks/finance/statements` | TTL only | 无 | 365 天 | 是 | 三表数据体量大，当前不建议全市场预跑。 |
| `stocks.indicators.ah_comparisons` | `/api/stocks/indicators/ah-comparisons` | 定时更新 | 每天 | 180 天 | 是 | AH 比价是日频指标，已接入 Store。 |
| `stocks.indicators.chip_distribution` | `/api/stocks/{code}/indicators/chip-distribution` | 定时更新 | 每天 | 180 天 | 是 | 筹码分布已接入 Store，可按日更新。 |
| `stocks.indicators.chip_performance` | `/api/stocks/{code}/indicators/chip-performance` | 定时更新 | 每天 | 180 天 | 是 | 筹码表现已接入 Store，可按日更新。 |
| `stocks.indicators.daily_basic` | `/api/stocks/indicators/daily-basic` | 定时更新 | 每天 | 180 天 | 是 | 日频基础指标已接入 Store，适合每天更新。 |
| `stocks.indicators.daily_market_value` | `/api/stocks/indicators/daily-market-value` | 定时更新 | 每天 | 180 天 | 是 | 日频市值指标已接入 Store，适合每天更新。 |
| `stocks.indicators.daily_valuation` | `/api/stocks/indicators/daily-valuation` | 定时更新 | 每天 | 180 天 | 是 | 日频估值指标已接入 Store，适合每天更新。 |
| `stocks.indicators.money_flow` | `/api/stocks/{code}/indicators/money-flow` | 定时更新 | 每天 | 180 天 | 是 | 个股资金流已接入 Store，适合每天更新。 |
| `stocks.indicators.premarket` | `/api/stocks/{code}/indicators/premarket` | 定时更新 | 每天 | 30 天 | 是 | 盘前指标已接入 Store，适合每天更新。 |
| `stocks.indicators.risk_flags` | `/api/stocks/indicators/risk-flags` | 定时更新 | 每天 | 180 天 | 是 | 风险标记已接入 Store，适合每天更新。 |
| `stocks.ownership.ccass_holding_details` | `/api/stocks/{code}/ownership/ccass-holding-details` | TTL only | 无 | 180 天 | 是 | CCASS 明细按个股窗口访问，预跑全市场成本高。 |
| `stocks.ownership.ccass_holdings` | `/api/stocks/{code}/ownership/ccass-holdings` | TTL only | 无 | 180 天 | 是 | CCASS 汇总按个股窗口访问，按需缓存即可。 |
| `stocks.ownership.hk_connect_holdings` | `/api/stocks/{code}/ownership/hk-connect-holdings` | TTL only | 无 | 180 天 | 是 | 港股通持股按个股访问，按需缓存即可。 |
| `stocks.ownership.pledges.details` | `/api/stocks/{code}/ownership/pledges/details` | TTL only | 无 | 365 天 | 是 | 质押明细低频，按需缓存即可。 |
| `stocks.ownership.pledges.stats` | `/api/stocks/{code}/ownership/pledges/stats` | TTL only | 无 | 365 天 | 是 | 质押统计低频，按需缓存即可。 |
| `stocks.ownership.shareholders.changes` | `/api/stocks/{code}/ownership/shareholders/changes` | 定时更新 | 每周日 | 365 天 | 是 | 默认由 `derived_core` 基于 `shareholders.count` 派生，并以自身 capability 写入 Store。 |
| `stocks.ownership.shareholders.count` | `/api/stocks/{code}/ownership/shareholders/count` | TTL only | 无 | 365 天 | 是 | 股东户数低频，按需缓存即可。 |
| `stocks.ownership.shareholders.top10` | `/api/stocks/{code}/ownership/shareholders/top10` | TTL only | 无 | 365 天 | 是 | 前十大股东按报告期稳定，按需缓存即可。 |
| `stocks.ownership.shareholders.top10_float` | `/api/stocks/{code}/ownership/shareholders/top10-float` | TTL only | 无 | 365 天 | 是 | 前十大流通股东按报告期稳定，按需缓存即可。 |
| `stocks.profile.basic` | `/api/stocks/{code}/profile/basic` | TTL only | 无 | 365 天 | 是 | 单股基础资料按需访问；全量目录由 `stocks.catalog` 月更。 |
| `stocks.profile.company` | `/api/stocks/{code}/profile` | TTL only | 无 | 365 天 | 是 | 公司画像低频，按需缓存即可。 |
| `stocks.profile.management_rewards` | `/api/stocks/{code}/profile/management-rewards` | 定时更新 | 每月最后一天 | 365 天 | 是 | 管理层薪酬低频变化，已接入 Store，月更即可。 |
| `stocks.profile.managers` | `/api/stocks/{code}/profile/managers` | 定时更新 | 每月最后一天 | 365 天 | 是 | 管理层名单低频变化，已接入 Store，月更即可。 |
| `stocks.profile.name_history` | `/api/stocks/{code}/profile/name-history` | TTL only | 无 | 365 天 | 是 | 名称历史低频，按需缓存即可。 |
| `stocks.quotes.auctions` | `/api/stocks/{code}/quotes/auctions` | 定时更新 | 每天 | 30 天 | 是 | 个股竞价行情已接入 Store，适合每天更新。 |
| `stocks.quotes.daily` | `/api/stocks/quotes` | 定时更新 | 每天 | 30 天 | 是 | 股票日线是核心行情，适合每日滚动补最近交易日。 |
| `stocks.quotes.daily_snapshot` | `/api/stocks/quotes/daily-snapshot` | 定时更新 | 每天 | 30 天 | 是 | 单日全市场快照高复用。 |
| `stocks.quotes.intraday` | `/api/stocks/quotes` | TTL only | 无 | 1 天 | 是 | 分钟线体量大且盘中时效强，不适合当前每日 capture 预跑。 |
| `stocks.reference.bse_code_mappings` | `/api/stocks/reference/bse-code-mappings` | 定时更新 | 每月最后一天 | -1 | 是 | 北交所代码映射低频变化，月更即可。 |
| `stocks.reference.hk_connect_targets` | `/api/stocks/reference/hk-connect-targets` | 定时更新 | 每月最后一天 | 365 天 | 是 | 沪深港通标的范围低频调整，月更即可。 |
| `stocks.research.reports` | `/api/stocks/{code}/research/reports` | TTL only | 无 | 180 天 | 是 | 个股研报按需查询；公共排行由 `rankings.research.reports` 日更。 |
| `stocks.research.surveys` | `/api/stocks/{code}/research/surveys` | TTL only | 无 | 180 天 | 是 | 调研记录按个股访问，按需缓存即可。 |
| `stocks.signals.hl` | `/api/stocks/{code}/signals/hl` | 定时更新 | 每天 | 1 天 | 是 | 默认由 `derived_core` 基于分钟行情派生，并以自身 capability 写入 Store。 |
| `stocks.signals.nine_turn` | `/api/stocks/{code}/signals/nine-turn` | 定时更新 | 每天 | 30 天 | 是 | 九转信号已接入 Store，适合每天更新。 |

## 当前默认开启的定时更新清单

当前默认开启 45 条 capability：

- 每天：`stocks.quotes.daily`、`stocks.quotes.daily_snapshot`、`stocks.quotes.auctions`、`stocks.factors.technical`、`stocks.indicators.daily_basic`、`stocks.indicators.daily_valuation`、`stocks.indicators.daily_market_value`、`stocks.indicators.money_flow`、`stocks.indicators.risk_flags`、`stocks.indicators.ah_comparisons`、`stocks.indicators.chip_distribution`、`stocks.indicators.chip_performance`、`stocks.indicators.premarket`、`stocks.signals.hl`、`stocks.signals.nine_turn`、`boards.quotes.daily`、`boards.indicators.money_flow`、`boards.indicators.money_flow.snapshot`、`indexes.quotes.daily`、`markets.trading.open_auctions`、`markets.events.news`、`markets.indicators.main_capital_flow`、`markets.connect.capital_flow`、`markets.connect.quotas`、`markets.connect.active_top10`、`markets.events.block_trades`、`markets.participants.dragon_tiger`、`markets.participants.dragon_tiger.institutions`、`markets.participants.hot_money.details`、`rankings.research.reports`
- 每周日：`boards.members`、`indexes.members`、`rankings.research.broker_monthly_picks`、`stocks.ownership.shareholders.changes`
- 每月最后一天：`stocks.catalog`、`stocks.catalog.archive`、`stocks.profile.management_rewards`、`stocks.profile.managers`、`boards.catalog`、`boards.reference.categories`、`indexes.catalog`、`markets.calendar.trading`、`markets.participants.hot_money`、`stocks.reference.bse_code_mappings`、`stocks.reference.hk_connect_targets`

未列入定时更新的 `stocks.finance.*`、`stocks.corporate_actions.*`、`stocks.ownership.*` 等个股级重型数据，当前保持 TTL only；等明确需要全市场离线底座时，再单独评估批量窗口和 provider 成本。

## 已完成改造说明

- 所有普通 API capability 均已接入 Store；默认表不再保留 TTL=0 的能力。
- `stocks.factors.technical`、`stocks.signals.hl`、`stocks.ownership.shareholders.changes` 属于派生能力，默认 provider 为 `derived_core`。如果未来某个外部源原生支持其中某项能力，只需要该源 manifest 声明对应 handler，再把 capability 的 provider 切到该源。
- 运行环境应使用统一默认策略表同步 `capability_cache_policy` 与 `capability_capture_policy`。
