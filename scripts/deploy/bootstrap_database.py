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
    do $$
    begin
      if to_regclass('fact.stock_bar_1m') is null then
        create table fact.stock_bar_1m (
          market character varying not null,
          code character(6) not null,
          bar_time timestamp without time zone not null,
          open double precision not null,
          high double precision not null,
          low double precision not null,
          close double precision not null,
          volume bigint not null,
          amount double precision,
          loaded_at timestamp with time zone not null default now(),
          primary key (market,code,bar_time)
        );
        perform create_hypertable(
          'fact.stock_bar_1m'::regclass,
          by_range('bar_time',interval '14 days'),
          create_default_indexes => false
        );
        create index stock_bar_1m_code_time_idx on fact.stock_bar_1m(code,bar_time);
        create index stock_bar_1m_time_idx on fact.stock_bar_1m(bar_time desc);
        alter table fact.stock_bar_1m set (
          timescaledb.enable_columnstore=true,
          timescaledb.segmentby='market,code',
          timescaledb.orderby='bar_time ASC'
        );
        call add_columnstore_policy(
          'fact.stock_bar_1m'::regclass,
          after => interval '30 days',
          if_not_exists => true
        );
      end if;
    end $$
    """,
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
    create table if not exists ref.stock_code_migration (
        old_market character varying not null,
        old_code character varying not null,
        new_market character varying not null,
        new_code character varying not null,
        trade_date date not null,
        source text not null,
        source_evidence_sha256 text not null,
        loaded_at timestamp with time zone not null default now(),
        primary key (old_market, old_code, trade_date),
        unique (new_market, new_code, trade_date),
        constraint stock_code_migration_bjse_only check (old_market = 'BJSE' and new_market = 'BJSE'),
        constraint stock_code_migration_code_format check (
            old_code ~ '^[0-9]{6}$' and new_code ~ '^[0-9]{6}$' and old_code <> new_code
        ),
        constraint stock_code_migration_evidence_sha256 check (
            source_evidence_sha256 ~ '^[0-9a-f]{64}$'
        )
    )
    """,
    "create index if not exists stock_code_migration_trade_date_idx on ref.stock_code_migration (old_market, old_code, trade_date)",
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
    *(
        f"""
        do $$
        begin
          if to_regclass('fact.{table_name}') is null then
            create table fact.{table_name} (
              market character varying not null,
              code character(6) not null,
              bar_time timestamp without time zone not null,
              open double precision not null,
              high double precision not null,
              low double precision not null,
              close double precision not null,
              volume bigint not null,
              amount double precision,
              loaded_at timestamp with time zone not null default now(),
              primary key (market,code,bar_time)
            );
            perform create_hypertable(
              'fact.{table_name}'::regclass,
              by_range('bar_time',interval '14 days'),
              create_default_indexes => false
            );
            create index {table_name}_code_time_idx on fact.{table_name}(code,bar_time);
            create index {table_name}_time_idx on fact.{table_name}(bar_time desc);
            alter table fact.{table_name} set (
              timescaledb.enable_columnstore=true,
              timescaledb.segmentby='market,code',
              timescaledb.orderby='bar_time ASC'
            );
            call add_columnstore_policy(
              'fact.{table_name}'::regclass,
              after => interval '30 days',
              if_not_exists => true
            );
          end if;
        end $$
        """
        for table_name in ("stock_bar_5m", "stock_bar_30m")
    ),
    """
    create table if not exists ref.future_series (
        product_code text not null,
        exchange text not null,
        series_type text not null,
        display_name text not null default '',
        loaded_at timestamp with time zone not null default now(),
        primary key (product_code, exchange, series_type),
        check (series_type in ('apex_l0_adjusted', 'main_continuous'))
    )
    """,
    """
    do $$
    begin
      if to_regclass('fact.future_bar_1m') is null then
        create table fact.future_bar_1m (
          product_code text not null,
          exchange text not null,
          series_type text not null,
          bar_time timestamp without time zone not null,
          open double precision,
          high double precision,
          low double precision,
          close double precision,
          volume double precision,
          open_interest double precision,
          adjustment_offset double precision,
          source_key text not null,
          loaded_at timestamp with time zone not null default now(),
          primary key (product_code,exchange,series_type,bar_time),
          foreign key (product_code,exchange,series_type)
            references ref.future_series(product_code,exchange,series_type)
        );
        perform create_hypertable(
          'fact.future_bar_1m'::regclass,
          by_range('bar_time',interval '14 days'),
          create_default_indexes => false
        );
        create index future_bar_1m_time_idx on fact.future_bar_1m(bar_time,product_code,series_type);
      end if;
    end $$
    """,
    """
    create table if not exists fact.stock_price_band_daily (
        market text not null,
        code text not null,
        trade_date date not null,
        upper_limit double precision,
        lower_limit double precision,
        price_band_status text not null default 'price_limits',
        source_evidence_sha256 text,
        source text not null,
        loaded_at timestamp with time zone not null default now(),
        primary key (market, code, trade_date),
        foreign key (market, code) references ref.stock (market, code)
    )
    """,
    "create index if not exists stock_price_band_daily_date_idx on fact.stock_price_band_daily (trade_date)",
)

# This is deliberately separate from CREATE TABLE IF NOT EXISTS: production
# deployments already have this table, so a restart must also upgrade it.
PRICE_BAND_STATUS_MIGRATION_SQL = (
    "alter table fact.stock_price_band_daily add column if not exists price_band_status text not null default 'price_limits'",
    "alter table fact.stock_price_band_daily add column if not exists source_evidence_sha256 text",
    """
    do $$ begin
      if not exists (
        select 1 from pg_constraint
        where conname = 'stock_price_band_daily_status_check'
          and conrelid = 'fact.stock_price_band_daily'::regclass
      ) then
        alter table fact.stock_price_band_daily
          add constraint stock_price_band_daily_status_check check (
            price_band_status = 'price_limits'
            or (price_band_status = 'no_price_limit'
                and upper_limit is null and lower_limit is null
                and source_evidence_sha256 ~ '^[0-9a-f]{64}$')
          ) not valid;
      end if;
    end $$
    """,
)


def main() -> None:
    _load_environment()
    database_config = _database_config()
    _ensure_database(database_config)
    _ensure_extension(database_config)
    _ensure_base_schema(database_config)
    _ensure_daily_snapshot_index(database_config)
    _ensure_quotemux_schema()
    _ensure_dataset_version_vector(database_config)
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
    if database_config["host"] not in ("", "127.0.0.1", "localhost", "::1"):
        return False
    psql = shutil.which("psql")
    sudo = shutil.which("sudo")
    if os.name == "nt" or psql is None or sudo is None:
        return False
    user_name = database_config["user"]
    database_name = database_config["dbname"]
    database_port = database_config["port"]
    password = database_config["password"].replace("'", "''")
    role_sql = (
        "do $$ begin "
        f"if exists (select 1 from pg_roles where rolname = '{user_name}') then "
        f"alter role {user_name} login password '{password}'; "
        "else "
        f"create role {user_name} login password '{password}'; "
        "end if; end $$"
    )
    if _run_as_postgres(psql, ("-p", database_port, "-v", "ON_ERROR_STOP=1", "-d", "postgres", "-c", role_sql)) != 0:
        return False
    exists_sql = f"select 1 from pg_database where datname = '{database_name}'"
    result = subprocess.run([sudo, "-n", "-u", "postgres", psql, "-p", database_port, "-tAc", exists_sql], capture_output=True, text=True)
    if result.returncode != 0:
        return False
    if result.stdout.strip() == "1":
        return True
    return _run_as_postgres(psql, ("-p", database_port, "-v", "ON_ERROR_STOP=1", "-d", "postgres", "-c", f"create database {database_name} owner {user_name}")) == 0


def _run_as_postgres(psql: str, arguments: tuple[str, ...]) -> int:
    return subprocess.run(["sudo", "-n", "-u", "postgres", psql, *arguments], capture_output=True, text=True).returncode


def _ensure_extension(database_config: dict[str, str]) -> None:
    try:
        with _connect(database_config) as connection:
            with connection.cursor() as cursor:
                cursor.execute("create extension if not exists timescaledb")
        with _connect(database_config) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select e.extversion, a.default_version "
                    "from pg_extension e join pg_available_extensions a on a.name = e.extname "
                    "where e.extname = 'timescaledb'"
                )
                versions = cursor.fetchone()
        if versions is not None and versions[0] != versions[1]:
            # TimescaleDB rejects ALTER EXTENSION when its old library was already
            # loaded in the session. Keep the upgrade as the first statement of a
            # fresh connection, as required by the extension itself.
            with _connect(database_config) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("alter extension timescaledb update")
    except psycopg.Error as exc:
        if _ensure_extension_with_local_postgres_user(database_config):
            return
        raise RuntimeError("无法启用 TimescaleDB 扩展。请确认目标 PostgreSQL 已安装 TimescaleDB，且当前账号有创建扩展权限。") from exc


def _ensure_extension_with_local_postgres_user(database_config: dict[str, str]) -> bool:
    if database_config["host"] not in ("", "127.0.0.1", "localhost", "::1"):
        return False
    psql = shutil.which("psql")
    sudo = shutil.which("sudo")
    if os.name == "nt" or psql is None or sudo is None:
        return False
    if _run_as_postgres(
        psql,
        (
            "-X",
            "-p",
            database_config["port"],
            "-v",
            "ON_ERROR_STOP=1",
            "-d",
            database_config["dbname"],
            "-c",
            "create extension if not exists timescaledb",
        ),
    ) != 0:
        return False
    return _run_as_postgres(
        psql,
        (
            "-X",
            "-p",
            database_config["port"],
            "-v",
            "ON_ERROR_STOP=1",
            "-d",
            database_config["dbname"],
            "-c",
            "alter extension timescaledb update",
        ),
    ) == 0


def _ensure_base_schema(database_config: dict[str, str]) -> None:
    with _connect(database_config) as connection:
        with connection.cursor() as cursor:
            for statement in BASE_SCHEMA_SQL:
                cursor.execute(statement)
            for statement in PRICE_BAND_STATUS_MIGRATION_SQL:
                cursor.execute(statement)


def _ensure_daily_snapshot_index(database_config: dict[str, str]) -> None:
    """Create the date/code access path without duplicating an equivalent index."""
    equivalent_index_sql = """
        select exists (
            select 1
            from pg_index index_rows
            join pg_class tables on tables.oid = index_rows.indrelid
            join pg_namespace schemas on schemas.oid = tables.relnamespace
            where schemas.nspname = 'fact'
              and tables.relname = 'stock_daily_1d'
              and index_rows.indisvalid
              and index_rows.indisready
              and (
                  select array_agg(attributes.attname order by keys.ordinality)
                  from unnest(index_rows.indkey) with ordinality keys(attnum, ordinality)
                  join pg_attribute attributes
                    on attributes.attrelid = index_rows.indrelid
                   and attributes.attnum = keys.attnum
                  where keys.ordinality <= 2
              ) = array['trade_date', 'code']::name[]
        )
    """
    with _connect(database_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(equivalent_index_sql)
            if cursor.fetchone()[0]:
                return
            cursor.execute(
                "create index concurrently if not exists stock_daily_1d_trade_date_code_idx "
                "on fact.stock_daily_1d (trade_date, code)"
            )


def _ensure_quotemux_schema() -> None:
    from quotemux.runtime import QuoteMux
    from quotemux.futures import ensure_future_schema
    from quotemux.store.timeout_admin import QuoteMuxTimeoutAdmin

    runtime = QuoteMux()
    runtime.cache.list_policies()
    runtime.capture.list_policies()
    ensure_future_schema()
    QuoteMuxTimeoutAdmin().sync_defaults()


def _ensure_dataset_version_vector(database_config: dict[str, str]) -> None:
    migration_sql = (SCRIPT_ROOT / "dataset-version-vector.sql").read_text(encoding="utf-8")
    with _connect(database_config) as connection:
        connection.execute(migration_sql)


if __name__ == "__main__":
    main()
