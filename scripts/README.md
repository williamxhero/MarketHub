# 脚本目录

- `deploy/`：新环境安装、数据库初始化和 API 启动所需的公共脚本。
- `dailyupdate/`：可选的 Linux/WSL 定时更新入口。
- `local/`：本地运维、一次性回填和临时脚本；该目录被 Git 忽略，不是部署依赖。

在工作区根目录运行 `MarketHub/install_markethub.py` 完成安装；随后运行 `MarketHub/scripts/deploy/run_api.py` 启动 API。
