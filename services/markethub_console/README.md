# MarketHub Console 运维说明

MarketHub Console 是 MarketHub API 同进程托管的前端资产，不再单独启动 Web service。运行 `markethub_api` 后访问 `http://127.0.0.1:8803/admin`。

当前 `/admin` 已提供三类后台运维入口：

- `Source Packages`：安装或更新 QuoteMux 数据源包。
- `Capabilities`：查看 capability 矩阵、缓存策略和定时更新配置。
- `Warmups`：创建大范围预热后台任务，异步执行 capability 批量预热，并查看任务进度、明细和最近历史。

本地构建：

```powershell
powershell -ExecutionPolicy Bypass -File services/markethub_console/scripts/build.ps1
```

本地测试：

```powershell
py -3.13 -m pytest services/markethub_console/tests -q
```

`markethub_api` 的 `/console` 兼容跳转到 `/admin`，`/admin-console` 是备选入口。
