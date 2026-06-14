# MarketHub

## 这是个什么东西？

**MarketHub 是给底层的超级引擎 `QuoteMux` 套上了一层好用的 HTTP API 接口、超详细的文档 和 管理界面**

如果你还不了解底层的 [QuoteMux](https://github.com/williamxhero/QuoteMux) 是干嘛的，一句话概括：它是一个**金融行情数据的超级聚合器**。它没有造什么新轮子，而是把 `Tushare`、`AkShare`、`eFinance`、`OpenTdx` 这些你平时常用的底层数据源**全部打包整合在了一起**，并且加上了**可配置的本地缓存**。

**为什么要这么折腾？主要是为了解决直接搞这些数据源时的一堆破事：**

- **极其不稳定：** 单一数据源经常报错，或者某些特定数据总是缺失。
- **接口乱七八糟：** 换个数据源就要重写一遍对接代码，依赖包还会互相冲突。
- **容易被封 IP：** 很多底层库不带缓存，你稍微多调几次，就被限制调用频率了。

`QuoteMux` 帮你在这些底层库之上垫了一层。你的业务代码、HTTP API 或是管理界面，只需要和 `QuoteMux` 的**一套稳定接口**打交道，彻底把系统和特定的数据源解绑。

## ⚠️ 核心预警：一键安装前必须做的准备

**这里有一个非常关键的设定，请务必先看：**

一键安装脚本 `install_markethub.py` **并不是**在 MarketHub 仓库内部运行的。它要求你必须有一个**统一的工作区根目录**，并且要把 QuoteMux 和 MarketHub 这两个仓库并排放在一起。

另外，**正式运行前还需要先准备 PostgreSQL + TimescaleDB**。当前 MarketHub/QuoteMux 的默认运行口径依赖本地 PostgreSQL，并要求目标数据库已启用 `timescaledb` 扩展。

**最标准的操作姿势如下：**

先建一个工作区目录，把两个仓库 clone 下来。

**Windows:**

PowerShell

```
mkdir D:\market_stack
cd D:\market_stack
git clone --branch main https://github.com/williamxhero/QuoteMux.git
git clone --branch main https://github.com/williamxhero/MarketHub.git
```

**Linux:**

Bash

```
mkdir -p ~/market_stack
cd ~/market_stack
git clone --branch main https://github.com/williamxhero/QuoteMux.git
git clone --branch main https://github.com/williamxhero/MarketHub.git
```

然后，你需要把 `install_markethub.py` 脚本放到这个工作区的根目录下。搞定后，你的目录结构看起来必须是这样的：

Plaintext

```
market_stack/                <-- 你运行脚本时，必须处在这个目录下！
├─ install_markethub.py      <-- 一键安装脚本
├─ QuoteMux/                 <-- 核心引擎仓库
└─ MarketHub/                <-- 本仓库
```

## 第一步：最短一键安装路径

确认你的终端当前正处于工作区根目录（比如 `D:\market_stack`）后，直接运行：

**Windows:**

PowerShell

```
py -3.13 install_markethub.py
```

**Linux:**

Bash

```
python3 install_markethub.py
```

这个脚本会自动帮你搞定所有的前置脏活：

- 创建 Python 虚拟环境 (`.venv`)
- 安装本地的 `QuoteMux` 核心引擎
- 安装 `MarketHub` 运行所需的所有依赖
- 编译构建可视化 Console 的静态页面
- 跑一遍最小可用性校验

*注：这一步仅仅是把服务框架搭好了，底层用来对接 Tushare 等库的具体“数据源接入包”还没装。*

## 第二步：一键拉取全部数据源包 (Packages)

主体服务跑起来之后，你需要把真正的“数据弹药”装填进去：

1. 确保 MarketHub 服务已启动。
2. 浏览器打开管理后台：`http://127.0.0.1:8803/admin`
3. 找到 **Source Packages** 区域，点击 **`安装或更新全部 Packages`** 按钮。

点完后去喝口水。这个按钮会在后台调用 `QuoteMux` 的本体能力，自动从官方弹药库（[QuoteMux_Packages](https://github.com/williamxhero/QuoteMux_Packages)）把所有远程接入包一次性拉取并安装完毕。

## 日常使用：一键启动服务

如果你已经走完了上面的安装流程，以后想要单独启动服务，可以直接在 `MarketHub` 目录下执行统一启动脚本：

**Windows:**

PowerShell

```
powershell -ExecutionPolicy Bypass -File scripts/run_api.ps1
```

**Linux:**

Bash

```
bash scripts/run_api.sh
```

**通用入口（Windows / Linux 都可用）：**

```
python scripts/run_api.py
```

这个启动脚本会固定使用工作区根目录下的 `.venv`，不会误用系统 Python。

启动后的默认服务地址：

- **HTTP API 入口:** `http://127.0.0.1:8803`
- **Admin 管理后台:** `http://127.0.0.1:8803/admin`

## 整个生态是怎么配合的？（防头晕指南）

别被一堆仓库名绕晕了，它们的分工极其明确：

- **`QuoteMux`：** 核心引擎。真正干活的，负责整合数据能力、处理缓存、加载和安装接入包。
- **`MarketHub` (本仓库)：** 交互外壳。提供 HTTP API 接口，文档和网页管理后台。
- **`QuoteMux_Packages`：** 远程弹药库。里面全是对接具体提供商（如 Tushare、AkShare）的插件代码。

## 运行目录和全局数据更新脚本

`install_markethub.py` 是通用安装入口，不绑定某一台机器。所有人只要按上面的工作区结构运行它，都可以完成本机安装。

如需把运行数据、日志和 Task Center 调用脚本安装到指定位置，可在执行前设置 `MARKETHUB_RUNTIME_ROOT`。不设置时默认使用工作区根目录下的 `runtime`。

Windows PowerShell 示例：

```
$env:MARKETHUB_RUNTIME_ROOT="D:\market_stack_runtime"
py -3.13 install_markethub.py
```

Linux Bash 示例：

```
MARKETHUB_RUNTIME_ROOT=/data/markethub python3 install_markethub.py
```

安装脚本会创建运行目录、默认环境文件和全局数据更新脚本。安装后的脚本位置为：

```
$MARKETHUB_RUNTIME_ROOT/scripts/global-data-update.sh
```

`MarketHub/scripts/run_api.py` 会读取 `$MARKETHUB_RUNTIME_ROOT/env/markethub.env`，保证 API、QuoteMux runtime 和全局数据更新脚本使用同一个运行目录配置。
