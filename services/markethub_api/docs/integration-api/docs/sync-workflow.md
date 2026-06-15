# MarketHub 文档同步流程

这里记录当 API、系统行为或文档结构发生变化后，怎样同步更新 `/docs`、`/doc-view/*`、`/docs/all`、`/doc-view/all` 和搜索索引。

## 先理解四类页面的来源

- `/docs`：直接读取 `services/markethub_api/docs/integration-api/README.md`。
- `/doc-view/`：读取同一份根文档，再渲染成 HTML 页面。
- `/docs/{path}` 与 `/doc-view/{path}`：直接读取 `services/markethub_api/docs/integration-api` 下对应的 markdown 源文件。
- `/docs/all` 与 `/doc-view/all`：运行时扫描整个 `services/markethub_api/docs/integration-api` 目录，自动聚合全部 markdown 文档。
- `/docs/search` 与 `/doc-view/search`：依赖本地搜索索引；文档文件更新后需要额外执行 reindex。

## 哪些内容是自动的，哪些不是

自动生效：

- 新增或修改 `docs/integration-api` 下的 markdown 文件后，`/docs/*`、`/doc-view/*`、`/docs/all`、`/doc-view/all` 会直接读取新内容。
- 新增文档文件后，聚合页会自动把它收进去，不需要另外维护一份 all 页面。

不自动生效：

- 搜索索引不会自动刷新，部署后要手动调用 `POST /api/admin/docs/reindex`。
- 根文档 `/docs` 不会自动知道你新增了什么专题；如果入口结构或使用方式变了，要手动改 `docs/integration-api/README.md`。
- 分组 `README.md` 也不会自动补入口；新增了系统文档、专题说明或维护流程时，要手动把目录入口补进去。
- `scripts/refresh_markethub_api_docs.py` 目前只覆盖 `stocks / boards / indexes / markets / rankings` 这些公开 `GET` 接口文档，不覆盖 admin 路由和 docs 路由。

## 标准同步步骤

### 1. 先改代码和测试

- 先完成 API 代码、控制台页面、服务行为和测试更新。
- 如果行为改动会影响调用方式、返回字段、状态机或运维入口，不要等到最后再补文档。

### 2. 区分“自动生成接口文档”和“手写说明文档”

对公开 `GET` 接口：

- 如果接口位于 `routers/stocks.py`、`boards.py`、`indexes.py`、`markets.py`、`rankings.py`，优先运行：

```powershell
py -3.13 MarketHub/scripts/refresh_markethub_api_docs.py
```

- 该脚本会按路由签名和返回模型刷新对应 markdown 文件。

对 admin、docs、system、运行规则类文档：

- 这些内容目前需要手写维护。
- 常见位置：
  - `services/markethub_api/docs/integration-api/README.md`
  - `services/markethub_api/docs/integration-api/docs/*.md`
  - `services/markethub_api/docs/integration-api/system/*.md`

### 3. 手动更新根入口和分组入口

出现以下情况时，必须同步改入口文档：

- 新增了一类接口或新专题文档。
- 某个能力的推荐入口、使用顺序、职责边界发生变化。
- `/docs` 根文档里应该能直接看到这次变化。

通常要检查这几处：

- `services/markethub_api/docs/integration-api/README.md`
- `services/markethub_api/docs/integration-api/docs/README.md`
- 相关分组的 `README.md`，例如 `system/README.md`
- 若发布/验收口径变化，还要同步 `system/runbook.md` 和 `system/minimal-regression.md`

### 4. 本地验证文档源文件是否串起来了

至少确认：

- 根文档能看到新的入口说明。
- 分组 README 能导航到新增专题。
- 新增 markdown 文件位于 `docs/integration-api` 目录树内；否则 `/docs/all` 不会收录。
- 没有为同一个专题额外复制一份“聚合页专用文档”；all 页本来就是扫描源目录生成的。

### 5. 部署文档文件到目标环境

- 如果只是改 markdown 源文件，服务通常不需要改代码逻辑；但目标机器上必须拿到新文件。
- 如果同时改了路由或渲染逻辑，按正常发布流程部署新版本并重启服务。

### 6. 部署后手动重建搜索索引

文档文件同步到目标环境后，执行：

```bash
curl -X POST http://127.0.0.1:8803/api/admin/docs/reindex
```

说明：

- `/docs`、`/doc-view/*`、`/docs/all`、`/doc-view/all` 直接读文件，不依赖这个索引。
- `/docs/search` 和 `/doc-view/search` 依赖这个索引；不 reindex 的话，搜索结果会停留在旧版本。

### 7. 最终验收

至少打开并确认：

- `/docs`
- `/doc-view/`
- `/docs/all`
- `/doc-view/all`

如果这次改动涉及具体专题或新接口，再补查对应页面，例如：

- `/docs/system/admin-warmups`
- `/doc-view/system/admin-warmups`
- `/docs/sync-workflow`

## 给以后 thread 的最短操作清单

1. 改代码和测试。
2. 判断这次变更是公开 `GET` 接口、admin/system 说明，还是两者都有。
3. 公开 `GET` 接口先跑 `refresh_markethub_api_docs.py`。
4. 手动补根 README、分组 README、system/docs 说明页。
5. 把改动部署到目标环境。
6. 调 `POST /api/admin/docs/reindex`。
7. 打开 `/docs`、`/doc-view/`、`/docs/all`、`/doc-view/all` 验收。
