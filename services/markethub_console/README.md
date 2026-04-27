# MarketHub Console 运维说明

MarketHub Console 是 MarketHub API 同进程托管的前端资产，不再单独启动 Web service。运行 `markethub_api` 后访问 `http://127.0.0.1:8803/admin`。

本地构建：

```powershell
powershell -ExecutionPolicy Bypass -File services/markethub_console/scripts/build.ps1
```

本地测试：

```powershell
py -3.13 -m pytest services/markethub_console/tests -q
```

`markethub_api` 的 `/console` 兼容跳转到 `/admin`，`/admin-console` 是备选入口。
