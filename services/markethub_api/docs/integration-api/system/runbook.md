# MarketHub 运行说明

当前正式口径：
- MarketHub 承载 A 股市场基础数据 API，以及统一新闻事件只读查询 API。
- 对外路径保持 `/api/*`。
- 文档路径保持 `/docs/*` 和 `/doc-view/*`。
- `tick` 逐笔行情当前不接入 MarketHub。
- 因子能力不再放在 MarketHub 主路径。

## 启动方式

本地开发：
- `py -3.13 app.py`
- `py -3.13 -m uvicorn app:app --host 0.0.0.0 --port 8803`

生产环境：
- 使用目标环境自己的虚拟环境启动 `services/markethub_api/app.py`。
- 部署目录、环境变量加载方式、服务管理方式由目标环境自行约定。
- 公开仓库不固化服务器名称、绝对路径或私有运维命令。

## 推荐发布流程

正式发布前固定执行：
- 本地 `pytest`
- 上传发布产物
- 在目标环境更新当前发布目录
- 确认目标环境虚拟环境依赖
- 重启服务
- 自动验收与回滚

## 运行前环境

至少需要：
- `MARKETHUB_DB_HOST`
- `MARKETHUB_DB_PORT`
- `MARKETHUB_DB_NAME`
- `MARKETHUB_DB_USER`
- `MARKETHUB_DB_PASSWORD`
- `MARKETHUB_DATA_ROOT`

如需访问真实 provider，还需要在 source instance 配置中写入统一密钥字段：
- `secret_values.api_key`

## 当前来源优先级

- 股票、板块、指数相关接口按 `capability_id` 进入 QuoteMux Store，Store 未命中时再按 Capability Matrix 启用的 source package 并发取数。
- `datalake` source 已废弃；运行时不再把它作为 provider、默认候选源或隐藏 fallback。
- 指数接口当前能力链路为 `static_core / Store -> Tushare/OpenTDX -> efinance -> mootdx -> akshare`。

## 最小验收

至少检查：
- `http://127.0.0.1:8803/api/health`
- `http://127.0.0.1:8803/api/openapi.json`
- `http://127.0.0.1:8803/docs`
- `http://127.0.0.1:8803/docs/all`
- `http://127.0.0.1:8803/doc-view/all`
- `http://127.0.0.1:8803/api/stocks/catalog`
- `http://127.0.0.1:8803/api/boards/catalog`
- `http://127.0.0.1:8803/api/indexes/catalog`
- `http://127.0.0.1:8803/api/markets/calendar/trading`
- `http://127.0.0.1:8803/api/markets/events/news?trade_date=2026-04-12&crawl_date=2026-04-12&sort_by=crawl_time`

## 边界说明

- MarketHub 负责基础目录、基础画像、基础行情、基础指标、统一新闻事件查询和文档服务。
- `factors_proj` 负责因子计算、刷新、存储和 HTTP 出口。
- `stock_platform` 只作为外部消费方和监控方，不再承载 MarketHub 源码。
