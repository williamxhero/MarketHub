# MarketHub AI 安装与部署指南

本文件面向第一次接触 MarketHub 的安装 AI。先阅读用户 prompt，所有路径、主机、端口、数据库、远程目标和调度器都必须由用户指定；缺少关键值时主动询问。不得把安装 AI 自己机器上的路径、主机名、端口、数据库、账号、服务名或调度器配置带到用户目标机器；只能使用用户提供或在目标机器探测确认的配置。

## 0. 参数收集

确认 OS（Windows/Linux/WSL2）、WORKSPACE_ROOT、Python >= 3.12 解释器路径、是否已有 PostgreSQL + TimescaleDB、DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD、provider token、SCHEDULER 和时区。没有数据库时，询问用户是否授权 AI 在目标机器安装；如果用户没有现成数据库且授权安装，AI 必须自己安装并配置，不得要求用户手工完成。密码、token 和私有主机信息不得写入 Git 或文档。

## 1. 目录与两个仓库

MarketHub 和 QuoteMux 必须同级，安装器复制到工作区根目录：

<WORKSPACE_ROOT>/
  MarketHub/
  QuoteMux/

在用户确认的工作区执行：

git clone --branch main https://github.com/williamxhero/MarketHub.git MarketHub
git clone --branch main https://github.com/williamxhero/QuoteMux.git QuoteMux

已有仓库先检查 git status --short；有未提交修改停止并询问，干净时才允许 git pull --ff-only。安装器必须从工作区根目录调用。

## 2. 前置依赖

安装 Git、Python、pip、curl。运行时需要 PostgreSQL 和 TimescaleDB。数据库不存在时，安装 AI 应根据用户确认的 OS 使用原生包管理器安装 PostgreSQL 和 TimescaleDB，创建运行所需的数据库和账号，并写入运行环境；数据库已存在时，使用用户提供的 DB_* 参数连接。不要因为数据库尚未准备好就结束安装。

CREATE EXTENSION IF NOT EXISTS timescaledb;

不要假设数据库端口、数据库名、用户或密码。将确认或刚刚创建的 DB_* 值写入运行环境文件或 secret 存储。然后判断项目是否需要提前建表：如果 MarketHub/QuoteMux 的启动或初始化入口会自动创建表，直接调用该入口；如果必须提前创建，安装 AI 自己执行项目已有的初始化入口，例如 MarketHub/scripts/deploy/bootstrap_database.py。不要要求用户手工执行 SQL，也不要让 AI 凭空重写替代项目契约的全量建表脚本。

## 3. 安装核心和 Packages

在工作区根目录执行：

<PYTHON> MarketHub/install_markethub.py

该安装器创建共享 .venv，以 editable 模式安装 QuoteMux，安装 MarketHub 依赖，准备 Console、runtime、日志、缓存和环境文件；随后从 git+https://github.com/williamxhero/QuoteMux_Packages.git@main 在线安装全部 source packages，并自动运行数据库 bootstrap。不要 clone QuoteMux_Packages，也不要设置 QUOTEMUX_PACKAGE_REPO_SPEC 指向本地目录。带 requirements.txt 的 package 进入隔离环境，需要时安装 Playwright Chromium。

数据库 bootstrap 会创建或确认数据库角色和数据库、启用 TimescaleDB、创建基础行情/引用表、初始化 QuoteMux 缓存/采集/超时策略表。安装器会先运行 scripts/deploy/install_database_service.py 探测并尝试安装数据库服务；如果目标 OS 的包管理器或管理员权限不可用，AI 必须报告该真实权限阻塞，而不是要求用户手工建库/建表。安装结果必须包含成功的 PackageInstallResult 和数据库初始化输出；失败停止。

## 4. 配置与启动

在用户指定的 runtime 环境文件中设置 MARKETHUB_HOST、MARKETHUB_PORT、MARKETHUB_DB_*、MARKETHUB_RUNTIME_ROOT、QUOTEMUX_RUNTIME_ROOT、QUOTEMUX_PACKAGE_VENV_ROOT 和 provider secrets。不要把示例值当默认值。

启动 API：

<PYTHON> MarketHub/scripts/deploy/run_api.py

启动脚本会读取环境文件并加入 QuoteMux、Packages 和 API 源码路径。使用用户确认的主机和端口验收：

curl --fail http://<MARKETHUB_HOST>:<MARKETHUB_PORT>/api/health
curl --fail http://<MARKETHUB_HOST>:<MARKETHUB_PORT>/admin
<PYTHON> -c "from quotemux import install_all_packages; print(install_all_packages())"

在 Admin 执行一次“安装或更新全部 Packages”，并检查 runtime 日志。健康接口、数据库、manifest、provider 依赖任一失败时停止并报告命令、退出码和 traceback。

## 5. 持久运行与定时更新

先询问用户选择的 Windows Task Scheduler、Linux systemd、WSL2 systemd 或 Task Center，以及时区。QuoteMux 到期检查调用为：

POST http://<MARKETHUB_HOST>:<MARKETHUB_PORT>/api/admin/capture/run-due-async

调度器必须使用能保留真实退出码的 shell 执行器；Task Center 通常使用 shell_file。安装器复制到用户指定的 MARKETHUB_RUNTIME_ROOT/scripts/ 的入口包括 global-data-update.sh、global-data-update-with-health.sh、data-health-check.sh；这些脚本依赖 Linux shell 工具，不能直接交给 Windows 原生任务计划。注册后必须手动运行一次，验证状态、退出码、日志和数据库变化。

如果用户选择 Task Center，先读取其 health、README、API schema 和现有任务，再按 schema 注册，禁止猜 payload 或覆盖现有任务。

如果用户选择 systemd，创建 service/timer 时使用用户确认的环境文件和绝对脚本路径，并执行 daemon-reload、enable --now 及一次手动运行。

如果用户选择 Windows Task Scheduler，只注册 API 启动任务和 HTTP 到期检查；不要声称 Bash 全局更新已经部署。


## 7. 停止条件

缺少用户参数或 secret、TimescaleDB 不可用、PackageInstallResult 失败、API 非 2xx、调度器退出码丢失或数据健康检查失败时，停止并报告需要补充的参数与证据。
