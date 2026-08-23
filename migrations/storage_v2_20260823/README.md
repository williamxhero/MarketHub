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
2. 调用本目录的入口。入口必须先运行 `discover_environment.py`，只读核对实际发布目录、运行目录、env 文件、systemd 服务及用户、磁盘余量、PostgreSQL/TimescaleDB 版本、四张目标表和 API 地址，并把结果保存为 `preflight.json`。
3. 环境合同通过后，脚本才创建完整新 release，再运行版本化迁移器。通用入口是 `deploy_and_migrate_remote.ps1`；`deploy_and_migrate_yosef.ps1` 是保留给现有运维命令的兼容入口。
4. 如果脚本失败，保留现场；修复本目录脚本并提交后，从同一个入口重跑。禁止在数据库里手工补做某一步。
5. `apply.json` 和 `verify.json` 均成功后才允许清理 legacy。清理仍由同一入口的 `-CleanupLegacy` 完成。
6. 最后核对 API、四张 canonical hypertable、迁移残留、systemd、磁盘空间和 Git commit。

标准命令：

```powershell
git -C MarketHub pull --ff-only
git -C QuoteMux pull --ff-only
git -C QuoteMux_Packages pull --ff-only

pwsh -File MarketHub/migrations/storage_v2_20260823/deploy_and_migrate_remote.ps1 `
  -HostName zhangsan-markethub `
  -ExpectedSourceStorageVersion storage-v1-postgresql-ordinary-bars `
  -TargetStorageVersion storage-v2.0.0-timescale-parquet-arrow
```

迁移通过并决定释放 legacy 空间时：

```powershell
pwsh -File MarketHub/migrations/storage_v2_20260823/deploy_and_migrate_remote.ps1 `
  -HostName zhangsan-markethub `
  -ExpectedSourceStorageVersion storage-v1-postgresql-ordinary-bars `
  -TargetStorageVersion storage-v2.0.0-timescale-parquet-arrow `
  -CleanupLegacy `
  -PruneOldReleases
```

第二条命令是幂等的：已经迁移的表不会重迁，已经清理的 legacy 不会重复删除。清理动作必须精确确认目标版本，且会检查 canonical hypertable、cutover 验收 SHA、反向 journal 已移除、无 shadow/failed/journal/trigger 残留、legacy 无 OID 依赖；任一条件不满足都会 fail closed。

## 环境发现合同

`yosef-server`、张三和李四只是不同部署目标，不是脚本内置环境。默认主机名仅是操作者的便捷默认值；远端路径、服务名、服务用户、env 文件、端口和数据库版本都以目标机器的只读发现结果为准。

可单独执行前置脚本查看清单；输出不会包含数据库密码：

```bash
python3 MarketHub/migrations/storage_v2_20260823/discover_environment.py \
  --app-root /srv/MarketHub2 \
  --runtime-root /var/lib/markethub \
  --env-path /etc/markethub/markethub.env \
  --service-name markethub-api
```

需要让远端入口只保存正式 `preflight.json`、不创建 release 或迁移时，使用同一命令并追加 `-PreflightOnly`。

发现优先级如下：

1. 已加载 systemd unit 的 `WorkingDirectory`、`EnvironmentFiles`、`User` 和 `ExecStart`。
2. env 文件内的运行目录、监听地址和端口。
3. 操作者显式传入的路径提示。
4. 仅在全新安装没有现有证据时，才使用 `/data/MarketHub2`、`/data/markethub` 和 `markethub-api` 作为初始建议值。

远端入口支持用 `-RemoteRoot`、`-RemoteRuntimeRoot`、`-RemoteEnvPath`、`-ServiceName` 和 `-HealthUrl` 描述非标准的新环境；已有部署则优先采用机器上真实 unit/env 的值。本机 WSL 入口只自动探测已经运行的 distribution，避免为了探测而启动另一个 distribution 的 PostgreSQL。若多个 WSL 同时运行，会在写入前要求显式给出 `-Distribution`；脚本不会默认终止它们。只有确认它们共享数据库目录时，操作者才可追加 `-QuiesceConflictingDistributions`，由同一顶层脚本停止其他 WSL。

现有部署若数据库不可达、PostgreSQL/TimescaleDB 不满足 manifest、存储状态无法归类，脚本会在 release 创建和数据库写入前停止。前置脚本还会在 PostgreSQL 数据目录所在卷按普通表当前总占用的 1.2 倍加 10 GiB 计算 shadow migration 最低可用空间，不足时拒绝开始。若数据库在另一台主机，脚本无法替它读取磁盘；AI thread 必须先在数据库主机核对空间，再显式追加 `-ConfirmRemoteDatabaseSpace`。`preflight.json`、`inspect-before-apply.json`、`apply.json`、`verify.json` 共同构成迁移证据链。

若目标 PostgreSQL 已安装但 TimescaleDB 低于 manifest 最低版本，默认仍是 fail closed。只有操作者明确允许系统包升级时，才追加 `-InstallOrUpgradePrerequisites`；入口会调用本目录的 `ensure_prerequisites_linux.sh`，通过 apt 将对应 PostgreSQL major 的 TimescaleDB 升至可用候选版本，再重新运行完整环境发现。数据库内扩展升级也由 `bootstrap_database.py` 的受控本机 postgres fallback 完成，不允许手工执行 `ALTER EXTENSION`。

可选的本机 WSL 演练入口同样不绑定用户名、盘符或发行版；它不是远端正式迁移的前置条件：

```powershell
pwsh -File MarketHub/migrations/storage_v2_20260823/deploy_and_migrate_local_wsl.ps1 `
  -ExpectedSourceStorageVersion storage-v1-postgresql-ordinary-bars `
  -TargetStorageVersion storage-v2.0.0-timescale-parquet-arrow
```

若机器上确实有多个相同候选，显式追加 `-Distribution Ubuntu-24.04`。若多个正在运行的 WSL 共享同一 PostgreSQL 数据目录，则使用 `-Distribution Ubuntu-24.04 -QuiesceConflictingDistributions`；非标准路径只需作为提示传入，不需要修改脚本。

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
