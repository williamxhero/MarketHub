# MarketHub / 合源 文档服务

这里收口 MarketHub 文档服务自身的访问方式。

当前文档自身建议从 `/docs/docs` 或 `/doc-view/docs` 打开。

## 文档接口

- `/docs`：程序对接根文档。
- `/docs/all`：全部已接入接口文档聚合页。
- `/docs/docs`：本文档，即文档服务说明页。
- `/docs/search-docs`：搜索能力说明。
- `/docs/search?q=stocks`：JSON 搜索接口。

## 页面化入口

- `/doc-view`：页面化根文档。
- `/doc-view/all`：页面化的全部文档聚合页。
- `/doc-view/docs`：页面化的文档服务说明页。
- `/doc-view/search`：页面化搜索入口。

## 程序对接建议

1. 先读取 `/api/openapi.json` 获取当前接口定义。
2. 再读取 `/docs` 获取程序对接说明和当前边界。
3. 如果需要快速扫全量入口，读取 `/docs/all` 或 `/doc-view/all`。
4. 实际业务调用统一走 `/api/*`。
