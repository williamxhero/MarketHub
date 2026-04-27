# MarketHub API 程序对接入口

这里是给程序调用方使用的起始文档。建议从这里进入，再按分组文档继续展开。

## 当前范围

- MarketHub 当前承载 A 股股票市场基础数据接口，以及统一新闻事件只读查询接口。
- 不纳入 ETF、LOF、债券、可转债、期货、期权、外汇、宏观利率等品类。
- `tick` 逐笔行情当前不接入 MarketHub。
- 因子能力不放在 MarketHub，已迁移到独立项目 `factors_proj`。

## 对接原则

- 根路径 `/` 是 HTML 页面，主要给人浏览，不作为程序接口入口。
- 程序对接统一走 `/api/*`。
- 接口描述统一走 `/api/openapi.json`。
- 文档读取统一走 `/docs/*`。
- 页面化文档浏览统一走 `/doc-view/*`。

## 程序入口

- `/api/health`：健康检查接口，用于探活和部署验收。
- `/api/openapi.json`：OpenAPI 描述，用于代码生成、接口发现和联调。
- `/docs`：程序对接根文档，也是当前起始入口。
- `/docs/all`：全部已接入接口文档聚合页。
- `/docs/search?q=stocks`：文档搜索接口，返回 JSON。

## 基础接口总览

### 系统接口

- `GET /api/health`：返回服务健康状态，适合探活和可用性检查。
- `GET /api/openapi.json`：返回当前完整接口定义，适合程序自动发现接口。

### 股票基础数据

- `GET /api/stocks/catalog?limit=20`：返回股票目录列表，适合做代码到名称的初始加载。
- `GET /api/stocks/{code}/profile/basic`：返回单只股票的基础资料。
- `GET /api/stocks/quotes?code=600000&freq=1d&start_date=2026-03-24&end_date=2026-03-30`：返回股票行情序列。

### 板块基础数据

- `GET /api/boards/catalog?limit=20`：返回板块目录列表。
- `GET /api/boards/quotes?board_code=885338&freq=1d&start_date=2026-03-24&end_date=2026-03-30`：返回板块行情序列。
- `GET /api/boards/885338/members`：返回板块成员列表。

### 指数基础数据

- `GET /api/indexes/catalog?limit=20`：返回指数目录列表。
- `GET /api/indexes/quotes?index_code=000001&freq=1d&start_date=2026-03-24&end_date=2026-03-30`：返回指数行情序列。
- `GET /api/indexes/000300/members?trade_date=2026-04-03`：返回指数成分列表。

### 市场基础数据

- `GET /api/markets/calendar/trading?start_date=2026-03-24&end_date=2026-03-31`：返回交易日历。
- `GET /api/markets/indicators/main-capital-flow?start_date=2026-03-24&end_date=2026-03-31`：返回市场主力资金流指标。
- `GET /api/markets/events/news?trade_date=2026-04-12&crawl_date=2026-04-12&sort_by=crawl_time&stock_code=600519`：按爬取时间返回统一新闻事件流。

## 最小对接顺序

1. 先请求 `/api/health`，确认服务在线。
2. 再读取 `/api/openapi.json`，确认当前接口定义。
3. 再读取 `/docs/all`，快速总览当前已接入文档。
4. 最后从本页和分组文档里选择需要的 `/api/*` 路径。

## 文档接口

- `/docs`：根文档，程序对接起始入口。
- `/docs/all`：全部已接入接口文档聚合页。
- `/docs/search?q=boards`：按关键词搜索文档，返回 JSON。
- `/doc-view`：页面化文档首页。
- `/doc-view/all`：页面化的全部文档聚合页。
- `/doc-view/search`：页面化搜索入口。

## 文档分组入口

- `/docs/stocks`：股票相关接口入口。
- `/docs/boards`：板块相关接口入口。
- `/docs/indexes`：指数相关接口入口。
- `/docs/markets`：市场相关接口入口。
- `/docs/rankings`：排行相关接口入口。
- `/docs/system`：运行方式、边界、回归范围和来源口径说明。

## 边界说明

- MarketHub 当前承载 A 股股票市场基础数据接口，以及统一新闻事件只读查询接口。
- `tick` 逐笔行情当前不在 MarketHub 范围内。
- 因子能力不再提供 `/api/*` 出口，应转由独立项目 `factors_proj` 承担。
- 如果调用方需要最新路径定义，应优先读取 `/api/openapi.json`。
