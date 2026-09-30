# 每日更新脚本

安装器会将本目录的脚本复制到 `$MARKETHUB_RUNTIME_ROOT/scripts/`。请在 Linux、WSL 或其他具备 Bash、curl 的调度器中执行。

- `global-data-update.sh`：调用本地 API 运行所有到期采集，并在结果中出现失败任务时退出失败。
- `data-health-check.sh`：调用本地 API 生成并校验数据健康快照。
- `global-data-update-with-health.sh`：依次运行上述两个脚本。
- `stock-intraday-capture-with-health.sh`：由 Task Center 在每个交易日 20:15 直接触发 `stocks.quotes.intraday`；它和全局更新共用锁，并要求 capture 返回完整成功。
- `adj-factor-daily-update.sh`：补齐 `QUOTEMUX_ADJUSTMENT_BASE_DATE` 之后缺失的复权因子交易日；自动检测并回填间隙，每日状态持久化到 `audit.stock_adj_factor_daily_status` 表，失败日期可见且可重试。调用 `backfill_tushare_adj_factor_snapshots.py daily` 从 Tushare provider 获取全市场 A 股复权因子快照，冲突时 fail-closed 而非覆盖历史值。
- `reconcile_task_center.py --task intraday`：恢复并校验分钟线 Task Center 任务配置。

脚本只依赖安装器生成的运行环境文件和公开 API，不依赖特定服务器路径、私有回填脚本或本地数据文件。
