from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

import psycopg


def main() -> None:
    if _postgres_is_available():
        print("PostgreSQL 已可用")
        return
    if os.name == "nt":
        _install_windows()
        return
    _install_linux()


def _postgres_is_available() -> bool:
    host = os.getenv("MARKETHUB_DB_HOST", "127.0.0.1")
    port = int(os.getenv("MARKETHUB_DB_PORT", "5432"))
    name = os.getenv("MARKETHUB_DB_NAME", "markethub")
    user = os.getenv("MARKETHUB_DB_USER", "markethub")
    password = os.getenv("MARKETHUB_DB_PASSWORD", "")
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        pass
    if password == "":
        return False
    try:
        with psycopg.connect(host=host, port=port, dbname=name, user=user, password=password, connect_timeout=3):
            return True
    except psycopg.Error:
        return shutil.which("psql") is not None and host not in {"127.0.0.1", "localhost", "::1"}


def _install_windows() -> None:
    winget = shutil.which("winget")
    if winget is None:
        raise RuntimeError("未找到 PostgreSQL，且 Windows 未提供 winget。安装 AI 需要管理员权限以及可用的系统包管理器。")
    subprocess.run(
        [winget, "install", "--exact", "--id", "PostgreSQL.PostgreSQL", "--accept-package-agreements", "--accept-source-agreements"],
        check=True,
    )
    raise RuntimeError("PostgreSQL 已安装。请重新运行安装器；如果 TimescaleDB 尚未安装，安装 AI 需要使用目标系统可用的 TimescaleDB 安装源完成安装。")


def _install_linux() -> None:
    manager = _linux_package_manager()
    if manager == "apt-get":
        subprocess.run(["sudo", "apt-get", "update"], check=True)
        subprocess.run(["sudo", "apt-get", "install", "-y", "postgresql", "postgresql-contrib", "timescaledb-2-postgresql-16"], check=True)
        subprocess.run(["sudo", "systemctl", "enable", "--now", "postgresql"], check=True)
        return
    if manager == "dnf":
        subprocess.run(["sudo", "dnf", "install", "-y", "postgresql-server", "postgresql-contrib", "timescaledb-2-postgresql-16"], check=True)
        subprocess.run(["sudo", "postgresql-setup", "--initdb"], check=True)
        subprocess.run(["sudo", "systemctl", "enable", "--now", "postgresql"], check=True)
        return
    raise RuntimeError("未识别目标 Linux 的包管理器。安装 AI 需要选择该系统支持的 PostgreSQL 和 TimescaleDB 安装方式。")


def _linux_package_manager() -> str:
    if shutil.which("apt-get") is not None:
        return "apt-get"
    if shutil.which("dnf") is not None:
        return "dnf"
    return ""


if __name__ == "__main__":
    main()
