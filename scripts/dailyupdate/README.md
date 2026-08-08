# 每日更新脚本

安装器会将本目录的脚本复制到 `$MARKETHUB_RUNTIME_ROOT/scripts/`。请在 Linux、WSL 或其他具备 Bash、curl 的调度器中执行。

- `global-data-update.sh`：调用本地 API 运行所有到期采集，并在结果中出现失败任务时退出失败。
- `data-health-check.sh`：调用本地 API 生成并校验数据健康快照。
- `global-data-update-with-health.sh`：依次运行上述两个脚本。

脚本只依赖安装器生成的运行环境文件和公开 API，不依赖特定服务器路径、私有回填脚本或本地数据文件。
