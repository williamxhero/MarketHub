# system

这里收口 MarketHub 的运行、部署、验收和边界说明。

建议按顺序查看：

- `/docs/system/runbook`
- `/docs/system/deploy-process`
- `/docs/system/health`
- `/docs/system/minimal-online-scope`
- `/docs/system/minimal-regression`
- `/docs/system/provider-coverage`
- `/docs/system/capability-update-policy`
- `/docs/system/provider-unification-todo`
- `/docs/system/provider-gap-candidates`

## 当前收口结论

- MarketHub 当前承载 A 股股票市场基础数据，以及统一新闻事件只读查询。
- ETF、LOF、债券、可转债等内容不在当前范围内。
- `tick` 逐笔行情暂不接入 MarketHub。
- 因子能力不放在 MarketHub，已迁移到独立项目 `factors_proj`。
- 在上述边界下，当前没有仍需继续推进的 provider 接入 gap。
