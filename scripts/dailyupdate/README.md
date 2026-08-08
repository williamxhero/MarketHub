# 每日更新脚本

此目录存放 Task Center 中直接调用的 MarketHub 每日更新入口脚本。

- `global-data-update.sh`：执行常规全局数据更新，补跑到期 capability、处理分钟数据缺口并校验行情覆盖。
- `global-data-update-with-health.sh`：用于每日最终补跑；完成全局数据更新后，再执行数据健康检查。

安装后，这些脚本会复制到运行目录的 `$MARKETHUB_RUNTIME_ROOT/scripts/`，Task Center 继续从该运行目录执行。
