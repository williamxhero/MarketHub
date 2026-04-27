# /docs/search

`GET` 搜索 MarketHub 全部文档内容，返回匹配结果列表。

## 请求参数

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| q | str | 是 | 搜索关键词 |
| limit | int | 否 | 返回数量上限，范围 `1..50` |

## 返回结构

顶层返回一个对象，包含 `items` 字段。

每个命中项通常包含：

- `path`：文档路径，例如 `stocks/quotes`
- `title`：文档标题
- `summary`：摘要
- `snippet`：命中片段
- `score`：搜索评分

## 覆盖范围

- 搜索结果来自 `services/markethub_api/docs/integration-api` 全目录。
- 包括接口文档、分组 README、系统说明和文档服务说明。
- 设计目标是不遗漏任何一篇 markdown 文档。

## 备注

- 如果要快速浏览当前全部文档，优先打开 `/docs/all` 或 `/doc-view/all`。
- 如果要查看页面化搜索界面，继续打开 `/doc-view/search`，或直接打开站点首页 `/`。
