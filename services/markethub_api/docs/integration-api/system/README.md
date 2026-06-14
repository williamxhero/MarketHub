# system

这里收口 MarketHub 的系统 API、系统设定、运行规则和验收边界。

建议按顺序查看：

- `/docs/system/runbook`
- `/docs/system/health`
- `/docs/system/minimal-online-scope`
- `/docs/system/minimal-regression`
- `/docs/system/provider-coverage`
- `/docs/system/capability-update-policy`
- `/docs/system/capability-store-metadata`

其中 `/docs/system/capability-update-policy` 同时约束两类后台更新入口：

- 定时到期检查：`POST /api/admin/capture/run-due-async`
- 手动大范围预热：`POST /api/admin/warmups`

## 当前收口结论

- MarketHub 当前承载 A 股股票市场基础数据，以及统一新闻事件只读查询。
- ETF、LOF、债券、可转债等内容不在当前范围内。
- `tick` 逐笔行情暂不接入 MarketHub。
- 因子能力不放在 MarketHub，已迁移到独立项目 `factors_proj`。
- 在上述边界下，当前没有仍需继续推进的 provider 接入 gap。
