# 最小上线范围

当前最小可上线范围：

- `/api/health`
- `/api/openapi.json`
- `/api/stocks/catalog`
- `/api/stocks/{code}/profile/basic`
- `/api/stocks/quotes`
- `/api/boards/catalog`
- `/api/boards/quotes`
- `/api/boards/{board_code}/members`
- `/api/boards/{board_code}/indicators/money-flow`
- `/api/indexes/catalog`
- `/api/indexes/{index_code}/profile`
- `/api/indexes/quotes`
- `/api/indexes/{index_code}/members`
- `/api/markets/calendar/trading`
- `/api/markets/events/news`
- `/docs/*` 文档服务
- `/doc-view/*` 页面化文档服务

## 当前边界

- 这里只指 A 股股票市场基础数据和统一新闻事件只读查询。
- 不包含 ETF、LOF、债券、可转债。
- 不包含 `tick` 逐笔行情。
- 不包含因子接口。
