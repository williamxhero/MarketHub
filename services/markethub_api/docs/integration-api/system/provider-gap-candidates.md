# Provider 可用但 MarketHub 未接入

盘点时间：2026-04-06

## 文档目的

这份文档只回答一个问题：

- 在当前边界下，哪些 provider 能力已经存在，但 MarketHub 还没有对外暴露。

如果某项能力已经进入 `/api/*` 主路径，就不再写在本文，而应写入 `/docs/system/provider-coverage`。

## 当前边界

- MarketHub 当前承载 A 股股票市场基础数据，以及统一新闻事件只读查询。
- 不纳入 ETF、LOF、债券、可转债、期货、期权、外汇、宏观利率等品类。
- `tick` 逐笔行情当前不接入 MarketHub。
- 因子能力不放在 MarketHub。

## 当前结论

在上述边界下，当前没有仍需继续推进的候选功能组。

也就是说，当前这份 gap 文档的结论就是：

- 当前无新增 gap。
- 当前现网范围已经完成收口。
- 已经实现的内容全部维护在 `/docs/system/provider-coverage`。

## 当前不计入 gap 的内容

以下内容即使 provider 侧存在能力，也不算当前 MarketHub 的 gap：

- ETF / LOF
- 债券 / 可转债
- `tick` 逐笔行情
- 因子能力
- provider 原生行业 / 概念体系
- 统一证券主数据扩展
- 期货
- 期权
- 外汇
- T+D
- 宏观利率

原因：

- 这些内容不符合当前“MarketHub 只专注 A 股股票市场基础数据与统一新闻事件查询”的边界。
- 其中 `tick` 和因子是明确排除项，不应再反复作为 gap 讨论。

## SuperMind 当前判断

- 在当前边界下，SuperMind 只保留为可选参考来源，不进入 MarketHub 主路径。
- `get_all_securities('index')`
- `get_price`
- `get_index_stocks`
- 由于指数能力已经由 `datalake + A2/B3` 完成现网接入，所以 SuperMind 当前不构成 gap。
- `query_iwencai` / `get_iwencai` 仍然不建议并入 MarketHub 主路径。

## 重新打开 gap 的条件

只有在以下情况之一出现时，才需要重新扩写本文：

- MarketHub 的业务边界发生变化。
- 新增了当前边界内、但尚未进入 `/api/*` 的能力组。
- 现有 coverage 中的能力被回退或下线。
