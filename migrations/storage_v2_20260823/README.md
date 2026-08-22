# MarketHub storage-v2 性能迁移发布说明

本目录是一次性、版本化迁移包，不是普通覆盖部署脚本。它负责把上一版普通 PostgreSQL 分钟表迁移到 storage-v2，并在确认 TimescaleDB canonical hypertable 正常后清理 legacy 表。

版本合同：

- 迁移 ID：`markethub-storage-v2-20260823`
- 源存储版本：`storage-v1-postgresql-ordinary-bars`
- 目标存储版本：`storage-v2.0.0-timescale-parquet-arrow`
- 已知上一应用发布：`deploy_20260822_191100`
- PostgreSQL：16 或 18
- TimescaleDB：不低于 2.27.2

`manifest.json` 是机器可读的正式版本合同。未来普通版本仍走覆盖部署，不应自动重复运行本目录。

## 给“张三的 AI thread”的唯一执行流程

张三已经部署过上一版时，他的 AI thread 应执行以下流程，不要手工执行迁移 SQL：

1. 在 MarketHub2 工作区拉取 MarketHub、QuoteMux 和 QuoteMux_Packages 的最新代码，确认三个仓库没有未保存改动。
2. 先只读核对当前发布、磁盘余量、PostgreSQL/TimescaleDB 版本和 API 健康。
3. 调用本目录的 `deploy_and_migrate_yosef.ps1`。脚本先创建完整新 release，再运行版本化迁移器。
4. 如果脚本失败，保留现场；修复本目录脚本并提交后，从同一个入口重跑。禁止在数据库里手工补做某一步。
5. `apply.json` 和 `verify.json` 均成功后才允许清理 legacy。清理仍由同一入口的 `-CleanupLegacy` 完成。
6. 最后核对 API、四张 canonical hypertable、迁移残留、systemd、磁盘空间和 Git commit。

标准命令：

```powershell
git -C MarketHub pull --ff-only
git -C QuoteMux pull --ff-only
git -C QuoteMux_Packages pull --ff-only

pwsh -File MarketHub/migrations/storage_v2_20260823/deploy_and_migrate_yosef.ps1 `
  -ExpectedSourceStorageVersion storage-v1-postgresql-ordinary-bars `
  -TargetStorageVersion storage-v2.0.0-timescale-parquet-arrow
```

迁移通过并决定释放 legacy 空间时：

```powershell
pwsh -File MarketHub/migrations/storage_v2_20260823/deploy_and_migrate_yosef.ps1 `
  -ExpectedSourceStorageVersion storage-v1-postgresql-ordinary-bars `
  -TargetStorageVersion storage-v2.0.0-timescale-parquet-arrow `
  -CleanupLegacy `
  -PruneOldReleases
```

第二条命令是幂等的：已经迁移的表不会重迁，已经清理的 legacy 不会重复删除。清理动作必须精确确认目标版本，且会检查 canonical hypertable、cutover 验收 SHA、反向 journal 已移除、无 shadow/failed/journal/trigger 残留、legacy 无 OID 依赖；任一条件不满足都会 fail closed。

## 迁移器的状态处理

`release_migration.py` 支持四种入口：

```bash
python release_migration.py --env-file /data/markethub/env/markethub.env inspect
python release_migration.py --env-file /data/markethub/env/markethub.env apply --service-name markethub-api
python release_migration.py --env-file /data/markethub/env/markethub.env verify
python release_migration.py --env-file /data/markethub/env/markethub.env cleanup-legacy \
  --confirm-target-version storage-v2.0.0-timescale-parquet-arrow
```

状态语义：

- 普通 canonical 表：建立 shadow、安装范围 journal、逐月可恢复回填、列式转换、暂停 writer、全量验证、事务切换、反向 journal 探针、同日验收。
- 已完成 cutover 但未移除反向 journal：从 cutover ledger 恢复并完成验收。
- canonical 已是 hypertable 且 ledger 已验收：幂等通过。
- 新安装且 canonical 已直接创建为 hypertable：按 fresh-install 通过。
- shadow/failed/ledger 状态矛盾：拒绝继续，必须先修复迁移脚本。

脚本生成的正式证据默认保存在 `/data/markethub/migrations/markethub-storage-v2-20260823/`。

## 失败恢复原则

- 迁移器逐月写 `audit.timescale_shadow_migration`，已验证月份重跑时跳过。
- writer 在回填期间可以继续工作，范围 journal 会追平变化；最终校验和 cutover 期间脚本暂停 API 服务。
- 任何验证不等、journal backlog、依赖关系、版本、验收 SHA 或残留对象异常都会中止。
- 脚本会在退出路径恢复由它停止的 API 服务。
- 不要手工 rename/drop/reconcile；把修复写入本迁移包、补测试、提交，再完整重跑。

## 普通后续发布

storage-v2 完成后，普通代码升级只运行 `scripts/local/deploy_yosef_server.ps1`。只有目标数据库仍处于本 manifest 支持的源/中间状态，或需要幂等核验本次迁移时，才再次调用本目录。
