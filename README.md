# MarketHub

MarketHub 是 QuoteMux 的独立 HTTP 服务壳。它不属于 `stock_platform`，也不并入 QuoteMux 内核。

## 职责

- Public API：把 QuoteMux 数据能力暴露为 HTTP 只读接口。
- Admin API：管理 source package、source instance、RuntimeProfile、contract policy、runtime report 和 audit。
- Console：由 MarketHub API 同进程承载在 `/admin`，只调用 MarketHub Admin API。
- 文档：承载 MarketHub API 对接文档和文档搜索。

## 本地运行

先安装 QuoteMux：

```powershell
py -3.13 -m pip install -e ../QuoteMux
```

安装 MarketHub 开发依赖：

```powershell
py -3.13 -m pip install -r requirements.dev.txt
```

启动 MarketHub：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_api.ps1
```

默认地址：

- API：`http://127.0.0.1:8803`
- Console：`http://127.0.0.1:8803/admin`

## 验证

```powershell
powershell -ExecutionPolicy Bypass -File scripts/test.ps1
```
