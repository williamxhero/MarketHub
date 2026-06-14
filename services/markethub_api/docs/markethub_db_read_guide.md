# MarketHub 数据库读取说明

本文对应当前 `PostgreSQL + TimescaleDB` 落库方案，目标是让 MarketHub 直接读取基础数据正式库，不再依赖旧 parquet 主路径。

## 1. 连接方式

统一使用环境变量：

- `DL_DB_HOST`
- `DL_DB_PORT`
- `DL_DB_NAME`
- `DL_DB_USER`
- `DL_DB_PASSWORD`

## 2. MarketHub 当前主要读取表

元数据：

- `ref.stock`
- `ref.stock_name_history`
- `ref.board`
- `ref.board_stock_membership`
- `ref.trade_calendar`

时序数据：

- `fact.stock_bar_1m`
- `fact.stock_daily_1d`
- `fact.index_bar_1d`
- `fact.board_daily_1d`
- `fact.news_event_agent_view`
- `fact.news_event_source`

更新状态：

- `etl.update_watermark`

## 3. 当前推荐读取口径

股票基础信息：

- `ref.stock`
- `ref.stock_name_history`

股票行情：

- `fact.stock_bar_1m`
- `fact.stock_daily_1d`

板块目录与成分：

- `ref.board`
- `ref.board_stock_membership`

板块行情：

- `fact.board_daily_1d`

交易日历：

- `ref.trade_calendar`

统一新闻事件：

- `fact.news_event_agent_view`
- `fact.news_event_source`

## 4. 边界约束

MarketHub 当前不再承载 `factor.*` 的对外接口职责。

统一新闻事件对外只读查询必须优先走：

- `fact.news_event_agent_view`

需要来源回链时，再补查：

- `fact.news_event_source`

不允许对用户直接暴露 raw 新闻来源表。

统一新闻事件对外契约里，公告/披露类数据必须显式区分两套时间：

- `announcement_time`：源站给出的公告日期或公告发布时间，属于业务时间。
- `crawl_time`：本系统显式记录的首次抓取时间，属于采集时间。

不允许继续把 `published_at`、`created_at`、`processed_at` 这类单独字段直接暴露给调用方，让调用方自行猜测语义。

如果需要因子结果：

- 已迁移到独立项目 `factors_proj`
- `stock_platform` 不再承担因子生产、存储和 HTTP 出口
- `stock_platform` 不再承载 MarketHub 源码

## 5. 迁移约束

迁移完成后，MarketHub 不应再依赖以下旧路径作为主读取入口：

- `type=metadata/*`
- `type=timeseries/*`

这些路径最多只保留为迁移期缓存或参考源。


## ????????

`fact.stock_daily_1d` ?????????????

- `is_suspended`??????????
- `is_st`????????? ST ???

???????????

- `/api/stocks/quotes?freq=1d`
- `/api/stocks/quotes/query?freq=1d`
- `/api/stocks/quotes/daily-snapshot`
- `/api/stocks/quotes/daily-local-window`

?????

- `skip_suspended=true`???????
- `skip_st=true`??????????? ST ??????

## 股票日线停牌占位

`fact.stock_daily_1d.is_suspended=true` 可能来自 provider 的明确停牌字段，也可能来自 QuoteMux 历史缺口补洞。补洞占位使用前一个交易日 `close` 填充 `open/high/low/close`，并写入 `volume=0`、`amount=0`，用于避免同一停牌交易日反复触发 provider 补缺。API 默认不返回停牌占位行；需要查看时传 `fill_missing=true&skip_suspended=false`。
