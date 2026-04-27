# MarketHub 连接复用与限流说明

MarketHub 当前只通过安装后的 QuoteMux 访问数据源。服务不直接在业务循环中创建临时 HTTP client，也不直接 import source/proxy/provider。

## 访问点清单

- `QuoteMux`：MarketHub 唯一数据入口，内部按 RuntimeProfile、source instance 和 source package 执行。
- `datalake`：由 QuoteMux 作为普通 source package 管理。
- `Tushare / OpenTDX / efinance / mootdx / akshare`：由 QuoteMux source package 管理。

## 限流配置

- `MHK_GLOBAL_CONCURRENCY`：全局 provider 并发上限，默认 `16`。
- `MHK_TUSHARE_CONCURRENCY`：TS 单 provider 并发上限，默认 `4`。
- `MHK_TUSHARE_RPS`：TS 每秒请求上限，默认 `3`。
- `MHK_OPENTDX_CONCURRENCY`：OpenTDX 单 provider 并发上限，默认 `4`。
- `MHK_OPENTDX_RPS`：OpenTDX 每秒请求上限，默认 `4`。
- `MHK_EFINANCE_CONCURRENCY`：efinance 单 provider 并发上限，默认 `4`。
- `MHK_MOOTDX_CONCURRENCY`：mootdx 单 provider 并发上限，默认 `3`。
- `MHK_AKSHARE_CONCURRENCY`：akshare 单 provider 并发上限，默认 `3`。
- `MHK_DB_CONCURRENCY`：datalake DB provider 并发上限，默认 `8`。
- `MHK_DB_RPS`：datalake DB 每秒请求上限，默认 `0`，表示不按 RPS 限流。
- `MHK_DB_POOL_SIZE`：datalake DB 连接池大小，默认 `8`。
- `MHK_PROVIDER_MAX_RETRIES`：provider 最大重试次数，默认 `2`。
- `MHK_TUSHARE_MAX_RETRIES`：TS 最大重试次数，未配置时使用 `MHK_PROVIDER_MAX_RETRIES`。
- `MHK_DB_MAX_RETRIES`：datalake DB 最大重试次数，默认 `0`。
- `MHK_PROVIDER_BACKOFF_SECONDS`：provider 指数退避基础秒数，默认 `0.2`。
- `MHK_PROVIDER_QUEUE_TIMEOUT_SECONDS`：provider 队列等待超时秒数，默认 `10`；超过后快速失败，避免排队请求长期占住服务线程。

## 连接池配置

- 上游连接、限流和重试由 QuoteMux runtime 管理。
- MarketHub 只暴露 HTTP 路由、线程池状态和 QuoteMux 汇总指标。
- datalake DB 连接池由 QuoteMux 管理；连接池指标可通过诊断接口查看。

## 诊断接口

`GET /api/diagnostics/connections` 返回：

- QuoteMux runtime 汇总指标。
- `tushare`、`opentdx`、`efinance`、`mootdx`、`akshare`、`datalake_db` 的活跃数、排队数、总请求数、错误率、重试次数、限流等待次数和耗时。
- datalake DB 连接池大小、已创建连接数、活跃连接数、空闲连接数、复用次数和丢弃次数。

## 压测命令

```powershell
netstat -ano | findstr TIME_WAIT
netstat -ano | findstr CLOSE_WAIT
$env:MARKETHUB_PROBE_HOST='127.0.0.1'
$env:MARKETHUB_PROBE_PORT='8803'
$env:MARKETHUB_PROBE_WORKERS='8'
$env:MARKETHUB_PROBE_REQUESTS_PER_WORKER='10'
$env:MARKETHUB_PROBE_PATH='/api/stocks/indicators/daily-basic?trade_date=2025-01-02'
py -3.13 ops/scripts/markethub_connection_probe.py
netstat -ano | findstr TIME_WAIT
netstat -ano | findstr CLOSE_WAIT
curl.exe http://127.0.0.1:8803/api/diagnostics/connections
```

## 回滚方式

- 回滚代码：恢复 MarketHub API、Console 和 QuoteMux 依赖版本。
- 回滚部署：恢复上一版镜像或发布包，然后按目标环境的服务编排方式重启 MarketHub。
- 回滚配置：如果只需要放宽限制，可调大 `MHK_GLOBAL_CONCURRENCY`、`MHK_TUSHARE_CONCURRENCY`、`MHK_OPENTDX_CONCURRENCY` 和 `MHK_DB_POOL_SIZE`。

## 后续监控

- 定期查看 `/api/diagnostics/connections` 的 `queued`、`total_retries`、`rate_waits` 和 `error_rate`。
- 若 `queued` 长时间大于 `0`，说明当前并发上限偏小或上游响应变慢。
- 若 `total_retries` 增长过快，应检查 TS、OpenTDX、B3 provider、代理或网络状态。
- 若 DB `created` 接近 `pool_size` 且 `active` 长期不下降，应检查慢 SQL 或 datalake 连接状态。
