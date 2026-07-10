from __future__ import annotations

import os
from pathlib import Path


def _load_env_file(path: Path) -> bool:
    if not path.is_file():
        return False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line == "" or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name, value)
    return True


def _bootstrap_env() -> None:
    workspace_root = Path(__file__).resolve().parents[2]
    candidates = (
        Path(os.getenv("MARKETHUB_ENV_PATH", "")),
        Path(os.getenv("MARKETHUB_RUNTIME_ROOT", "")) / "env" / "markethub.env",
        workspace_root / "runtime" / "env" / "markethub.env",
        Path("/data/markethub/env/markethub.env"),
    )
    for path in candidates:
        if str(path) != "." and _load_env_file(path):
            break


_bootstrap_env()

from quotemux.infra.db.client import execute_sql, query_dataframe


SCHEMA_SQL = (
    "alter table fact.stock_daily_1d add column if not exists pre_close double precision",
    "alter table fact.stock_daily_1d add column if not exists change double precision",
    "alter table fact.stock_daily_1d add column if not exists pct_chg double precision",
    """
    do $$
    begin
        if exists (
            select 1 from information_schema.columns
            where table_schema = 'ref' and table_name = 'stock' and column_name = 'board_type'
        ) and not exists (
            select 1 from information_schema.columns
            where table_schema = 'ref' and table_name = 'stock' and column_name = 'industry'
        ) then
            alter table ref.stock rename column board_type to industry;
        elsif exists (
            select 1 from information_schema.columns
            where table_schema = 'ref' and table_name = 'stock' and column_name = 'board_type'
        ) then
            update ref.stock
            set industry = board_type
            where coalesce(industry, '') = '' and coalesce(board_type, '') <> '';
            alter table ref.stock drop column board_type;
        end if;
    end $$
    """,
    "alter table ref.stock add column if not exists industry character varying not null default ''",
    "alter table ref.stock add column if not exists listing_board character varying not null default ''",
    """
    update ref.stock
    set listing_board = case
        when market = 'BJSE' or code like '4%' or code like '8%' or code like '9%' then 'beijing'
        when market = 'SHSE' and (code like '688%' or code like '689%') then 'star_market'
        when market = 'SZSE' and (code like '300%' or code like '301%') then 'chi_next'
        else 'main_board'
    end
    where listing_board = ''
    """,
    """
    create table if not exists fact.board_daily_1d (
        board_code character varying not null,
        trade_date date not null,
        open double precision,
        high double precision,
        low double precision,
        close double precision,
        volume double precision,
        amount double precision,
        pre_close double precision,
        change double precision,
        pct_chg double precision,
        loaded_at timestamp with time zone not null default now(),
        primary key (board_code, trade_date)
    )
    """,
    "create index if not exists board_daily_1d_date_idx on fact.board_daily_1d (trade_date)",
)


BACKFILL_SQL = """
with metric_rows as (
    select
        market,
        code,
        trade_date,
        close,
        lag(close) over (partition by market, code order by trade_date) as previous_close
    from fact.stock_daily_1d
),
updated_rows as (
    update fact.stock_daily_1d target
    set pre_close = coalesce(target.pre_close, metric_rows.previous_close),
        change = coalesce(target.change, target.close - metric_rows.previous_close),
        pct_chg = coalesce(
            target.pct_chg,
            (target.close - metric_rows.previous_close) / nullif(metric_rows.previous_close, 0) * 100
        )
    from metric_rows
    where target.market = metric_rows.market
      and target.code = metric_rows.code
      and target.trade_date = metric_rows.trade_date
      and metric_rows.previous_close is not null
      and (target.pre_close is null or target.change is null or target.pct_chg is null)
    returning 1
)
select count(*) as updated_count from updated_rows
"""


def main() -> None:
    for statement in SCHEMA_SQL:
        if not execute_sql(statement, ()):
            raise RuntimeError("行情与股票引用表结构迁移失败")
    frame = query_dataframe(BACKFILL_SQL, ())
    updated_count = 0 if frame.empty else int(frame.iloc[0].to_dict().get("updated_count", 0) or 0)
    print(f"行情与股票引用表结构迁移完成，历史日线指标回填 {updated_count} 行")


if __name__ == "__main__":
    main()
