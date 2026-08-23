# Query Read v3 性能迁移

这是一次独立的性能 read-model 迁移，不是普通覆盖部署，也不是 storage-v2 的重复执行。它适用于已经完成 storage-v2 的 MarketHub。

迁移内容包括数据集版本向量、严格只读 public query、日线和股票 1m coverage read model、自动 Parquet 发布、Arrow 流式交付和版本化响应缓存。迁移只新增 audit/readmodel 对象，不删除事实数据。回滚代码时保留这些表；它们不影响旧 release。

## 通用流程

执行 AI 必须先探测目标机器，不能假设主机名、用户、路径、端口、数据库名或 systemd unit。先从仓库根目录运行：

```powershell
pwsh -File MarketHub/migrations/query_read_v3_20260823/deploy_and_migrate_remote.ps1 `
  -HostName <TARGET_HOST> -PreflightOnly
```

检查生成的 evidence/preflight JSON。通过后运行同一入口，不传 `-PreflightOnly`。入口会：

1. 调用现有只读发现器读取 systemd、env、current release、数据库和磁盘。
2. 用正式 freeze 工具暂停 capture/update。
3. 调用项目正式 release 部署器。
4. 在新 release 环境执行 `release_migration.py apply`，按月幂等回填 1m coverage，构建并校验日线 coverage。
5. 执行 `verify`，检查 build state、版本向量、分钟汇总抽样和 API health。
6. 只有全部通过才 restore freeze。

迁移失败时不要手工补 SQL 或绕过脚本。保留 evidence，先在本目录修复脚本并补测试、commit/push，再用同一入口重跑。回填按月提交，已完成月份可安全覆盖重建。

## 单机已部署后的直接入口

仅当 env 已经由部署器加载、API service 已停止或 capture freeze 已生效时：

```bash
python MarketHub/migrations/query_read_v3_20260823/release_migration.py preflight
python MarketHub/migrations/query_read_v3_20260823/release_migration.py apply
python MarketHub/migrations/query_read_v3_20260823/release_migration.py verify
```

`apply` 不删除事实表、Parquet 或旧 release。失败后的代码回滚只需把 `current` 切回前一个正式 release 并重启服务；readmodel 表保留，后续可继续迁移。
