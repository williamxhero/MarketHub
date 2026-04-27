# 开发规则

## 安全须知

任何磁盘文件删除操作必须且只能使用 `safe-del`。严禁使用 `del`、`erase`、`rm`、`rmdir`、`rd` 或任何其他删除命令，严禁通过脚本、别名、封装或间接方式绕过。

## 项目边界

- MarketHub 是 QuoteMux 的 HTTP 服务壳，和 QuoteMux 平级。
- MarketHub 只依赖安装后的外部 `quotemux`。
- MarketHub 不依赖 `stock_platform` 源码路径。
- Public API 只做 HTTP facade。
- Admin API 只做 package、instance、profile、policy、report、audit 管理。
- Console 只调用 Admin API，不直连 QuoteMux。

## 开发要求

- 注释必须使用中文。
- 需要显示中文时，必须直接写中文。
- 禁止使用 Unicode escape 表示中文。
- 本地文件链接必须写成以 `/` 开头、使用 `/` 分隔符的绝对路径 Markdown 链接。
- 必须按职责拆分函数、文件、目录。
- 每个模块只能保留一个稳定的对外入口。
- 对外入口函数必须直接返回最终强类型结果。
- `str` 类型必须为非可空，字符串缺失值统一使用 `""`。
- 禁止添加“以后可能会用”的占位字段。
- 禁止恢复 `stock_platform/libs/quotemux` 或旧 provider/db/model 目录。

## 验证要求

- 修改 API 后运行 `py -3.13 -m pytest services/markethub_api/tests -q`。
- 修改 Console 后运行 `py -3.13 -m pytest services/markethub_console/tests -q`。
- 收口前运行 `py -3.13 -m pytest services/markethub_api/tests services/markethub_console/tests -q`。
