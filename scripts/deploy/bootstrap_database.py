from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import psycopg
from psycopg import sql


SCRIPT_ROOT = Path(__file__).resolve().parent
MARKETHUB_ROOT = SCRIPT_ROOT.parents[1]
WORKSPACE_ROOT = MARKETHUB_ROOT.parent
RUNTIME_ROOT = Path(os.getenv("MARKETHUB_RUNTIME_ROOT", str(WORKSPACE_ROOT / "runtime"))).expanduser().resolve()


BASE_SCHEMA_SQL = (
    "create schema if not exists ref",
    "create schema if not exists fact",
    """
    create table if not exists ref.stock (
        market character varying not null,
        code character varying not null,
        name character varying not null default '',
        industry character varying not null default '',
        listing_board character varying not null default '',
        listed_date date,
        delisted_date date,
        area character varying not null default '',
        board_type character varying not null default '',
        updated_at timestamp with time zone not null default now(),
        primary key (market, code)
    )
    """,
    """
    create table if not exists fact.stock_daily_1d (
        market character varying not null,
        code character varying not null,
        trade_date date not null,
        open double precision,
        high double precision,
        low double precision,
        close double precision,
        volume double precision,
        amount double precision,
        is_suspended boolean not null default false,
        is_st boolean not null default false,
        pre_close double precision,
        change double precision,
        pct_chg double precision,
        adj_factor double precision,
        loaded_at timestamp with time zone not null default now(),
        primary key (market, code, trade_date),
        foreign key (market, code) references ref.stock (market, code)
    )
    """,
    "create index if not exists stock_daily_1d_trade_date_idx on fact.stock_daily_1d (trade_date, market, code)",
    """
    create table if not exists fact.stock_price_band_daily (
        market text not null,
        code text not null,
        trade_date date not null,
        upper_limit double precision,
        lower_limit double precision,
        source text not null,
        loaded_at timestamp with time zone not null default now(),
        primary key (market, code, trade_date),
        foreign key (market, code) references ref.stock (market, code)
    )
    """,
    "create index if not exists stock_price_band_daily_date_idx on fact.stock_price_band_daily (trade_date)",
)


def main() -> None:
    _load_environment()
    database_config = _database_config()
    _ensure_database(database_config)
    _ensure_extension(database_config)
    _ensure_base_schema(database_config)
    _ensure_quotemux_schema()
    print("数据库初始化完成")


def _load_environment() -> None:
    environment_path = Path(os.getenv("MARKETHUB_ENV_PATH", str(RUNTIME_ROOT / "env" / "markethub.env")))
    if not environment_path.is_file():
        return
    for raw_line in environment_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line == "" or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name, value)


def _database_config() -> dict[str, str]:
    values = {
        "host": os.getenv("MARKETHUB_DB_HOST", "127.0.0.1"),
        "port": os.getenv("MARKETHUB_DB_PORT", "5432"),
        "dbname": os.getenv("MARKETHUB_DB_NAME", "markethub"),
        "user": os.getenv("MARKETHUB_DB_USER", "markethub"),
        "password": os.getenv("MARKETHUB_DB_PASSWORD", ""),
    }
    if values["password"] == "":
        raise RuntimeError("MARKETHUB_DB_PASSWORD 不能为空")
    for name in ("dbname", "user"):
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", values[name]) is None:
            raise RuntimeError(f"数据库 {name} 只能包含字母、数字和下划线，且不能以数字开头")
    return values


def _admin_config(database_config: dict[str, str]) -> dict[str, str]:
    return {
        "host": os.getenv("MARKETHUB_DB_ADMIN_HOST", database_config["host"]),
        "port": os.getenv("MARKETHUB_DB_ADMIN_PORT", database_config["port"]),
        "dbname": os.getenv("MARKETHUB_DB_ADMIN_NAME", "postgres"),
        "user": os.getenv("MARKETHUB_DB_ADMIN_USER", "postgres"),
        "password": os.getenv("MARKETHUB_DB_ADMIN_PASSWORD", ""),
    }


def _connect(config: dict[str, str]) -> psycopg.Connection:
    return psycopg.connect(
        host=config["host"],
        port=int(config["port"]),
        dbname=config["dbname"],
        user=config["user"],
        password=config["password"],
        autocommit=True,
        connect_timeout=10,
    )


def _ensure_database(database_config: dict[str, str]) -> None:
    try:
        with _connect(database_config):
            return
    except psycopg.Error:
        pass
    admin_config = _admin_config(database_config)
    try:
        with _connect(admin_config) as connection:
            with connection.cursor() as cursor:
                cursor.execute("select 1 from pg_roles where rolname = %s", (database_config["user"],))
                if cursor.fetchone() is None:
                    cursor.execute(
                        sql.SQL("create role {} login password {}").format(
                            sql.Identifier(database_config["user"]),
                            sql.Literal(database_config["password"]),
                        )
                    )
                cursor.execute("select 1 from pg_database where datname = %s", (database_config["dbname"],))
                if cursor.fetchone() is None:
                    cursor.execute(
                        sql.SQL("create database {} owner {}").format(
                            sql.Identifier(database_config["dbname"]),
                            sql.Identifier(database_config["user"]),
                        )
                    )
    except psycopg.Error as exc:
        if _create_database_with_local_postgres_user(database_config):
            return
        raise RuntimeError(
            "无法创建或确认 MarketHub 数据库。安装 AI 应先在目标机器安装 PostgreSQL/TimescaleDB，"
            "再提供可创建角色和数据库的管理凭证，或配置一个可写入的现有数据库。"
        ) from exc


def _create_database_with_local_postgres_user(database_config: dict[str, str]) -> bool:
    psql = shutil.which("psql")
    sudo = shutil.which("sudo")
    if os.name == "nt" or psql is None or sudo is None:
        return False
    user_name = database_config["user"]
    database_name = database_config["dbname"]
    password = database_config["password"].replace("'", "''")
    role_sql = (
        "do $$ begin "
        f"if not exists (select 1 from pg_roles where rolname = '{user_name}') then "
        f"create role {user_name} login password '{password}'; "
        "end if; end $$"
    )
    if _run_as_postgres(psql, ("-v", "ON_ERROR_STOP=1", "-d", "postgres", "-c", role_sql)) != 0:
        return False
    exists_sql = f"select 1 from pg_database where datname = '{database_name}'"
    result = subprocess.run([sudo, "-n", "-u", "postgres", psql, "-tAc", exists_sql], capture_output=True, text=True)
    if result.returncode != 0:
        return False
    if result.stdout.strip() == "1":
        return True
    return _run_as_postgres(psql, ("-v", "ON_ERROR_STOP=1", "-d", "postgres", "-c", f"create database {database_name} owner {user_name}")) == 0


def _run_as_postgres(psql: str, arguments: tuple[str, ...]) -> int:
    return subprocess.run(["sudo", "-n", "-u", "postgres", psql, *arguments], capture_output=True, text=True).returncode


def _ensure_extension(database_config: dict[str, str]) -> None:
    try:
        with _connect(database_config) as connection:
            with connection.cursor() as cursor:
                cursor.execute("create extension if not exists timescaledb")
    except psycopg.Error as exc:
        raise RuntimeError("无法启用 TimescaleDB 扩展。请确认目标 PostgreSQL 已安装 TimescaleDB，且当前账号有创建扩展权限。") from exc


def _ensure_base_schema(database_config: dict[str, str]) -> None:
    with _connect(database_config) as connection:
        with connection.cursor() as cursor:
            for statement in BASE_SCHEMA_SQL:
                cursor.execute(statement)


def _ensure_quotemux_schema() -> None:
    from quotemux.runtime import QuoteMux
    from quotemux.futures import ensure_future_schema
    from quotemux.store.timeout_admin import QuoteMuxTimeoutAdmin

    runtime = QuoteMux()
    runtime.cache.list_policies()
    runtime.capture.list_policies()
    ensure_future_schema()
    QuoteMuxTimeoutAdmin().sync_defaults()


if __name__ == "__main__":
    main()
