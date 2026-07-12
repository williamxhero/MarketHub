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
    "alter table fact.stock_daily_1d drop constraint if exists stock_daily_1d_market_code_check",
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
    "alter table ref.stock add column if not exists board_type character varying not null default ''",
    """
    insert into ref.stock (market, code, name, industry, listing_board, listed_date, delisted_date, area)
    select
        'SHSE',
        code,
        name,
        industry,
        listing_board,
        listed_date,
        delisted_date,
        area
    from ref.stock
    where market = 'BJSE' and left(code, 3) = '900'
    on conflict (market, code) do update set
        name = excluded.name,
        industry = excluded.industry,
        listing_board = excluded.listing_board,
        listed_date = excluded.listed_date,
        delisted_date = excluded.delisted_date,
        area = excluded.area,
        updated_at = now()
    """,
    "delete from ref.stock where market = 'BJSE' and left(code, 3) = '900'",
    """
    update ref.stock
    set listed_date = case code
            when '900941' then date '1996-08-09'
            when '900945' then date '1997-06-26'
        end,
        listing_board = 'main_board',
        updated_at = now()
    where market = 'SHSE'
      and code in ('900941', '900945')
    """,
    """
    do $$
    begin
        if to_regclass('ref.concept_stock_membership') is not null then
            delete from ref.concept_stock_membership
            where stock_market = 'BJSE' and stock_code = '834683';
        end if;
    end $$;
    """,
    "delete from ref.stock where market = 'BJSE' and code = '834683' and listed_date is null",
    """
    do $$
    begin
        if to_regclass('ref.concept_stock_membership') is not null then
            insert into ref.concept_stock_membership (concept_id, stock_market, stock_code, valid_from, valid_to, weight, updated_at)
            select concept_id, 'SHSE', stock_code, valid_from, valid_to, weight, now()
            from ref.concept_stock_membership
            where stock_market = 'BJSE' and left(stock_code, 3) = '900'
            on conflict (concept_id, stock_market, stock_code, valid_from) do update set
                valid_to = excluded.valid_to,
                weight = excluded.weight,
                updated_at = now();

            delete from ref.concept_stock_membership
            where stock_market = 'BJSE' and left(stock_code, 3) = '900';
        end if;
    end $$;
    """,
    """
    update fact.stock_daily_1d target
    set market = 'SHSE'
    where target.market = 'BJSE'
      and left(target.code, 3) = '900'
      and not exists (
          select 1
          from fact.stock_daily_1d existing_rows
          where existing_rows.market = 'SHSE'
            and existing_rows.code = target.code
            and existing_rows.trade_date = target.trade_date
      )
    """,
    "delete from fact.stock_daily_1d where market = 'BJSE' and left(code, 3) = '900'",
    """
    alter table fact.stock_daily_1d add constraint stock_daily_1d_market_code_check check (
        (market = 'SHSE' and left(code, 1) in ('5', '6', '9'))
        or (market = 'BJSE' and (left(code, 1) in ('4', '8') or left(code, 3) = '920'))
        or (market = 'SZSE' and left(code, 1) not in ('4', '5', '6', '8', '9'))
    )
    """,
    """
    update ref.stock
    set listing_board = case
        when market = 'BJSE' or left(code, 1) in ('4', '8') or left(code, 3) = '920' then 'beijing'
        when market = 'SHSE' and left(code, 3) in ('688', '689') then 'star_market'
        when market = 'SZSE' and left(code, 3) in ('300', '301') then 'chi_next'
        else 'main_board'
    end
    """,
    "update ref.stock set board_type = listing_board where board_type <> listing_board",
    """
    do $$
    begin
        if to_regclass('capability_capture_policy') is not null then
            update capability_capture_policy
            set cadence = 'daily',
                month_day = null,
                window_count = 1,
                batch_size = 1,
                notes = '股票目录，每日同步新上市和退市状态',
                updated_at = now()
            where capability_id = 'stocks.catalog';
        end if;
    end $$;
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
    "delete from fact.board_daily_1d where left(board_code, 9) <> 'INDUSTRY:'",
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
)
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
"""


BACKFILL_REMAINING_SQL = """
with metric_rows as (
    select
        market,
        code,
        trade_date,
        lag(close) over (partition by market, code order by trade_date) as previous_close
    from fact.stock_daily_1d
)
select count(*) as remaining_count
from fact.stock_daily_1d target
join metric_rows
  on target.market = metric_rows.market
 and target.code = metric_rows.code
 and target.trade_date = metric_rows.trade_date
where metric_rows.previous_close is not null
  and (target.pre_close is null or target.change is null or target.pct_chg is null)
"""


def main() -> None:
    for statement in SCHEMA_SQL:
        if not execute_sql(statement, ()):
            raise RuntimeError("行情与股票引用表结构迁移失败")
    if not execute_sql(BACKFILL_SQL, ()):
        raise RuntimeError("历史日线指标回填失败")
    frame = query_dataframe(BACKFILL_REMAINING_SQL, ())
    remaining_count = 0 if frame.empty else int(frame.iloc[0].to_dict().get("remaining_count", 0) or 0)
    if remaining_count != 0:
        raise RuntimeError(f"历史日线指标回填不完整，剩余 {remaining_count} 行")
    print("行情与股票引用表结构迁移完成，历史日线指标已回填")


if __name__ == "__main__":
    main()
