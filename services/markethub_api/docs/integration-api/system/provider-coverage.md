# MarketHub 已接入能力映射表

盘点时间：2026-04-23

## 文档目的

这份文档只回答三件事：

- 当前 MarketHub 已经通过哪些 `/api/*` 路径对外提供 A 股市场数据。
- 每条路径对应哪个 `capability_id`，以及 Capability Matrix 中哪些 source package 能提供该能力。
- 哪些路径属于盘中分钟口径，哪些只是日频或离线数据。

## 当前边界

- MarketHub 当前承载 A 股股票市场基础数据，以及统一新闻事件只读查询。
- 不纳入 ETF、LOF、债券、可转债、期货、期权、外汇、宏观利率等品类。
- `tick` 暂不接入 MarketHub。
- 因子能力不放在 MarketHub，已迁移到独立项目 `factors_proj`。

## 盘中口径说明

- 本文里的“实时”不表示 `current/current_n` 这类严格实时快照。
- 截至 2026-04-22，MarketHub 旧行情主链已完全移除。
- 当前最接近盘中的只有 `/api/stocks/quotes`：
  - 当 `freq=1m~60m` 的 QuoteMux Store 未命中时，会继续走 `OpenTDX` 补分钟 bar。
  - 若 `OpenTDX` 仍有缺口，才退到 `B3 = efinance -> mootdx -> akshare`。
- 这属于“盘中分钟 bar”，不等同于逐笔或实时快照。

## 使用说明

- `本地能力 / Store` 列写 QuoteMux Store 或本地 source package 承担的能力。
- `OpenTDX` 列写现网直接调用的 OpenTDX 能力。
- `Tushare` 列写现网直接调用的 Tushare API。
- `B3 后备` 列写现网补洞时允许调用的 `efinance -> mootdx -> akshare`。
- 某列留空，表示该接口主链没有接这一层来源。
- “正式源 / 常规后备 / 昂贵兜底” 的含义：
  - 正式源：`static_core`、`news_store`、`derived_core`、`OpenTDX`、`Tushare` 里承担正式主链的来源。
  - 常规后备：允许自动补目标 capability 的 `B3`。
  - 昂贵兜底：当前未放进默认链路、需要人工确认才启用的高成本路径，例如 `Tushare mins`。

## 股票接口

| MarketHub API | 本地能力 / Store | OpenTDX | Tushare | B3 后备 | 实时 | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| `/api/stocks/quotes` | `stocks.quotes.intraday` / `stocks.quotes.daily` Store + `static_core` | `stock_kline` | `pro_bar` | `efinance` `mootdx` `akshare` | 条件盘中 | `freq=1m~60m` 默认候选源是 `OpenTDX -> efinance -> mootdx -> akshare`；`freq=1d/1w/1mo` 默认候选源是 `static_core -> Tushare -> efinance -> mootdx -> akshare`。Capability Matrix 勾选的源会并发参与，最终由该 capability 的 `merge_strategy` 合并成一份结果。`1w/1mo` 统一由日线聚合，不混接外部周/月原始数据。 |
| `/api/stocks/quotes/daily-snapshot` | `stocks.quotes.daily_snapshot` Store + `static_core` |  | `daily` | `efinance` `mootdx` `akshare` | 否 | 单日全市场日线快照。默认候选源是 `static_core -> Tushare -> efinance -> mootdx -> akshare`，Capability Matrix 勾选的源会并发参与并按 `merge_strategy` 合并。 |
| `/api/stocks/catalog` | `ref.stock` |  |  | 否 | 股票目录。 |
| `/api/stocks/catalog/archive` |  |  | `bak_basic` | 否 | 指定交易日归档快照。 |
| `/api/stocks/{code}/profile/basic` | `ref.stock` |  |  | 否 | 股票基础资料。 |
| `/api/stocks/{code}/profile` |  |  | `stock_company` | 否 | 公司画像。 |
| `/api/stocks/{code}/profile/name-history` | `ref.stock_name_history` |  |  | 否 | 证券简称历史。 |
| `/api/stocks/{code}/profile/managers` |  |  | `stk_managers` | 否 | 高管名单。 |
| `/api/stocks/{code}/profile/management-rewards` |  |  | `stk_rewards` | 否 | 高管薪酬与持股。 |
| `/api/stocks/{code}/signals/hl` | `fact.stock_daily_1d` |  |  | 否 | 新高新低信号，服务层派生。 |
| `/api/stocks/{code}/signals/nine-turn` |  |  | `stk_nineturn` | 否 | 神奇九转。 |
| `/api/stocks/{code}/factors/adj` | `fact.stock_daily_1d` |  |  | 否 | 直接读取 `adj_factor`。 |
| `/api/stocks/{code}/factors/technical` | `derived_core` |  |  | 否 | 服务层基于标准日线派生技术指标。 |
| `/api/stocks/{code}/indicators/money-flow` | `static_core` |  | `moneyflow` | 否 | 默认候选源是 `static_core -> Tushare`，并发后按 `merge_strategy` 合并。 |
| `/api/stocks/indicators/ah-comparisons` |  |  | `stk_ah_comparison` | 否 | A/H 比价。 |
| `/api/stocks/indicators/daily-basic` |  |  | `daily_basic` | 否 | 已改为 `Tushare only`。 |
| `/api/stocks/indicators/daily-valuation` |  |  | `daily_basic` | 否 | 已改为 `Tushare only`。 |
| `/api/stocks/indicators/daily-market-value` |  |  | `daily_basic` | 否 | 已改为 `Tushare only`。 |
| `/api/stocks/indicators/risk-flags` |  |  | `stock_st` | 否 | 风险标识。 |
| `/api/stocks/{code}/indicators/premarket` |  |  | `stk_premarket` | 否 | 盘前指标。 |
| `/api/stocks/{code}/indicators/chip-distribution` |  |  | `cyq_chips` | 否 | 筹码分布。 |
| `/api/stocks/{code}/indicators/chip-performance` |  |  | `cyq_perf` | 否 | 筹码盈亏。 |
| `/api/stocks/finance/statements` |  |  | `income` `balancesheet` `cashflow` | 否 | 已改为 `Tushare only`。 |
| `/api/stocks/finance/indicators` |  |  | `fina_indicator` | 否 | 财务指标。 |
| `/api/stocks/{code}/finance/audits` |  |  | `fina_audit` | 否 | 审计意见。 |
| `/api/stocks/{code}/finance/disclosure-dates` |  |  | `disclosure_date` | 否 | 披露日期。 |
| `/api/stocks/{code}/finance/express` |  |  | `express` | 否 | 业绩快报。 |
| `/api/stocks/{code}/finance/forecasts` |  |  | `forecast` | 否 | 业绩预告。 |
| `/api/stocks/{code}/finance/main-business` |  |  | `fina_mainbz` | 否 | 主营业务构成。 |
| `/api/stocks/{code}/corporate-actions/dividends` |  |  | `dividend` | 否 | 分红送转。 |
| `/api/stocks/{code}/corporate-actions/repurchases` |  |  | `repurchase` | 否 | 回购。 |
| `/api/stocks/{code}/corporate-actions/rights-issues` |  |  | `rights_issue` `stk_ration` | 否 | 配股。 |
| `/api/stocks/{code}/corporate-actions/share-changes` |  |  | `share_change` `stk_share_change` `daily_basic` | 否 | 优先走股本变动表；缺失时退化为 `daily_basic` 差分推断。 |
| `/api/stocks/{code}/corporate-actions/unlock-schedules` |  |  | `share_float` | 否 | 解禁安排。 |
| `/api/stocks/{code}/ownership/ccass-holdings` |  |  | `ccass_hold` | 否 | CCASS 汇总。 |
| `/api/stocks/{code}/ownership/ccass-holding-details` |  |  | `ccass_hold_detail` | 否 | CCASS 明细。 |
| `/api/stocks/{code}/ownership/hk-connect-holdings` |  |  | `hk_hold` | 否 | 港股通持股。 |
| `/api/stocks/{code}/ownership/pledges/stats` |  |  | `pledge_stat` | 否 | 股权质押统计。 |
| `/api/stocks/{code}/ownership/pledges/details` |  |  | `pledge_detail` | 否 | 股权质押明细。 |
| `/api/stocks/{code}/ownership/shareholders/count` |  |  | `stk_holdernumber` | 否 | 股东户数。 |
| `/api/stocks/{code}/ownership/shareholders/changes` |  |  | `stk_holdernumber` | 否 | 基于股东户数序列在服务层派生。 |
| `/api/stocks/{code}/ownership/shareholders/top10` |  |  | `top10_holders` | 否 | 前十大股东。 |
| `/api/stocks/{code}/ownership/shareholders/top10-float` |  |  | `top10_floatholders` | 否 | 前十大流通股东。 |
| `/api/stocks/{code}/research/reports` |  |  | `report_rc` | 否 | 个股研报。 |
| `/api/stocks/{code}/research/surveys` |  |  | `stk_surv` | 否 | 调研记录。 |
| `/api/stocks/reference/bse-code-mappings` |  |  | `bse_mapping` | 否 | 北交所代码映射。 |
| `/api/stocks/reference/hk-connect-targets` |  |  | `stock_hsgt` | 否 | 沪深港通标的范围。 |
| `/api/stocks/{code}/quotes/auctions` |  |  | `stk_auction_o` `stk_auction_c` | 否 | 个股竞价。 |

## 板块接口

| MarketHub API | 本地能力 / Store | OpenTDX | Tushare | B3 后备 | 实时 | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| `/api/boards/quotes` | `fact.board_daily_1d` |  |  | 否 | 仅支持日线及更高周期；盘中频率当前不返回数据。 |
| `/api/boards/catalog` | `ref.board` |  |  | 否 | 板块目录。 |
| `/api/boards/{board_code}/profile` | `ref.board` |  |  | 否 | 板块画像。 |
| `/api/boards/{board_code}/members` | `ref.board_stock_membership` `ref.stock` |  |  | 否 | 板块成分。 |
| `/api/boards/{board_code}/members/history` | `ref.board_stock_membership` `ref.stock` |  |  | 否 | 板块成分历史。 |
| `/api/boards/{board_code}/indicators/money-flow` | `static_core` |  | `moneyflow_cnt_ths` `moneyflow_ind_ths` | 否 | 默认候选源是 `static_core -> Tushare`；`scope=board` 时走 `moneyflow_cnt_ths`，`scope=industry` 时走 `moneyflow_ind_ths`。 |
| `/api/boards/indicators/money-flow` | `static_core` |  |  | 否 | 单日全市场板块资金流快照，默认由 `static_core` 提供。 |
| `/api/boards/reference/categories` | `static_core` |  |  | 否 | 本地静态枚举，不依赖外部 provider。 |

## 指数接口

| MarketHub API | 本地能力 / Store | OpenTDX | Tushare | B3 后备 | 实时 | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| `/api/indexes/catalog` | `static_core` |  | `index_basic` |  | 否 | 当前执行顺序是 `static_core -> Tushare`。本地能力侧通过指数日线汇总目录与首末交易日。 |
| `/api/indexes/{index_code}/profile` | `fact.index_bar_1d` |  | `index_basic` |  | 否 | 与目录同链路。 |
| `/api/indexes/quotes` | `indexes.quotes.daily` Store + `static_core` | `index_daily` | `index_daily` | `efinance` `mootdx` `akshare` | 否 | 默认候选源是 `static_core -> Tushare -> efinance -> mootdx -> akshare`，Capability Matrix 勾选的源会并发参与并按 `merge_strategy` 合并。指数代码会先做名称/代码标准化，避免 `efinance` 把指数代码识别成股票。`1w/1mo` 同样统一由日线聚合。 |
| `/api/indexes/{index_code}/members` | `static_core` |  | `index_weight` | `efinance` `mootdx` `akshare` | 否 | 这是“降级后备”，不是 `index_weight` 等价替代。默认先走 `Tushare index_weight`；只有主源空结果时，才退到 `B3` 提供成分名单，并继续只用 `static_core` 补股票名称。 |

## 市场接口

| MarketHub API | 本地能力 / Store | OpenTDX | Tushare | B3 后备 | 实时 | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| `/api/markets/calendar/trading` | `markets.calendar.trading` Store + `static_core` |  | `trade_cal` | `akshare` | 否 | 默认候选源是 `static_core -> Tushare -> AKShare emergency`，Capability Matrix 勾选的源会并发参与并按 `merge_strategy` 合并。`AKShare` 只作为应急日历，不视为正式 `trade_cal` 等价物。 |
| `/api/markets/calendar/trading/previous` | `ref.trade_calendar` |  | `trade_cal` | `akshare` | 否 | 先补齐参考日前的交易日历缺口，再截取最近 `n` 个结果。 |
| `/api/markets/calendar/trading/next` | `ref.trade_calendar` |  | `trade_cal` | `akshare` | 否 | 先补齐参考日后的交易日历缺口，再截取最近 `n` 个结果。 |
| `/api/markets/calendar/trading/yearly` | `markets.calendar.trading.yearly` Store + `static_core` |  | `trade_cal` | `akshare` | 否 | 复用交易日历能力的默认候选源 `static_core -> Tushare -> AKShare emergency`，按 `merge_strategy` 合并。 |
| `/api/markets/events/news` | `news_store` |  |  | 否 | 统一新闻事件流正式主入口。默认通过 `news_store` 读取 `fact.news_event_agent_view`，仅在 `include_sources=true` 时再补查 `fact.news_event_source`。 |
| `/api/markets/indicators/main-capital-flow` |  |  | `moneyflow_mkt_dc` | 否 | 市场主力资金。 |
| `/api/markets/connect/capital-flow` |  |  | `moneyflow_hsgt` | 否 | 互联互通资金流。 |
| `/api/markets/connect/quotas` |  |  | `moneyflow_hsgt` | 否 | 服务层基于 `moneyflow_hsgt` 派生额度余额。 |
| `/api/markets/connect/active-top10` |  |  | `hsgt_top10` `ggt_top10` | 否 | 互联互通活跃成交前十。 |
| `/api/markets/events/block-trades` |  |  | `block_trade` | 否 | 大宗交易。 |
| `/api/markets/participants/dragon-tiger` |  |  | `top_list` | 否 | 龙虎榜。 |
| `/api/markets/participants/dragon-tiger/institutions` |  |  | `top_inst` | 否 | 龙虎榜机构席位。 |
| `/api/markets/participants/hot-money` |  |  | `hm_list` | 否 | 游资名单。 |
| `/api/markets/participants/hot-money/details` |  |  | `hm_detail` | 否 | 游资明细。 |
| `/api/markets/trading/open-auctions` |  |  | `stk_auction_o` | 否 | 复用个股竞价读取，只取开盘竞价。 |
| `/api/markets/trading/sessions` | `static_core` |  |  | 否 | 本地静态交易时段定义，不依赖外部 provider。 |

## 排行接口

| MarketHub API | 本地能力 / Store | OpenTDX | Tushare | B3 后备 | 实时 | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| `/api/rankings/research/reports` |  |  | `report_rc` | 否 | 研报热度排行。 |
| `/api/rankings/research/broker-monthly-picks` |  |  | `broker_recommend` | 否 | 券商月度金股。 |

## 最终结论

- 当前 `provider-coverage` 已按 capability 更新为 `Store / static_core / news_store / derived_core / OpenTDX / Tushare / B3` 映射。
- 截至 2026-04-22，MarketHub 旧行情主链已完全移除。
- 当前唯一需要单独注意的盘中口径仍是 `/api/stocks/quotes`：
  - 仅在分钟频率下可能读到盘中分钟 bar。
  - 它不是 `current` 式实时快照。
