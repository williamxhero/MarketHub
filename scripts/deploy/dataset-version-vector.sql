begin;

set local lock_timeout = '5s';

create schema if not exists audit;
create schema if not exists readmodel;

create table if not exists audit.dataset_version_state (
    dataset_id text primary key,
    baseline_id text not null,
    generation bigint not null check (generation >= 1),
    updated_at_utc timestamptz not null default clock_timestamp()
);

create table if not exists audit.dataset_version_dependency (
    dataset_id text not null references audit.dataset_version_state(dataset_id) on delete cascade,
    source_schema text not null,
    source_table text not null,
    managed_by text not null,
    primary key (dataset_id, source_schema, source_table)
);

create table if not exists audit.dataset_version_publication (
    dataset_id text not null,
    market_data_version text not null,
    dataset_version text not null,
    manifest_sha256 text not null check (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    relative_root text not null,
    published_at_utc timestamptz not null default clock_timestamp(),
    primary key (dataset_id, market_data_version),
    unique (dataset_id, dataset_version, market_data_version)
);

insert into audit.dataset_version_state(dataset_id,baseline_id,generation)
select dataset_id,md5(dataset_id || clock_timestamp()::text || random()::text || txid_current()::text),1
from unnest(array[
    'stock_reference','stock_daily_1d','stock_bar_1m','stock_bar_5m','stock_bar_30m',
    'future_bar_1m','concept_daily_1d','stock_research_daily'
]::text[]) dataset_id
on conflict (dataset_id) do nothing;

delete from audit.dataset_version_dependency where managed_by='query_read_v3';

insert into audit.dataset_version_dependency(dataset_id,source_schema,source_table,managed_by)
values
    ('stock_reference','ref','stock','query_read_v3'),
    ('stock_reference','ref','trade_calendar','query_read_v3'),
    ('stock_reference','fact','stock_listing_board_history','query_read_v3'),
    ('stock_daily_1d','fact','stock_daily_1d','query_read_v3'),
    ('stock_daily_1d','fact','stock_suspension_history','query_read_v3'),
    ('stock_daily_1d','ref','stock','query_read_v3'),
    ('stock_daily_1d','ref','trade_calendar','query_read_v3'),
    ('stock_bar_1m','audit','stock_bar_1m_write_event','query_read_v3'),
    ('stock_bar_1m','fact','stock_suspension_history','query_read_v3'),
    ('stock_bar_1m','ref','stock','query_read_v3'),
    ('stock_bar_1m','ref','trade_calendar','query_read_v3'),
    ('stock_bar_5m','fact','stock_bar_5m','query_read_v3'),
    ('stock_bar_5m','fact','stock_suspension_history','query_read_v3'),
    ('stock_bar_5m','ref','stock','query_read_v3'),
    ('stock_bar_5m','ref','trade_calendar','query_read_v3'),
    ('stock_bar_30m','fact','stock_bar_30m','query_read_v3'),
    ('stock_bar_30m','fact','stock_suspension_history','query_read_v3'),
    ('stock_bar_30m','ref','stock','query_read_v3'),
    ('stock_bar_30m','ref','trade_calendar','query_read_v3'),
    ('future_bar_1m','fact','future_bar_1m','query_read_v3'),
    ('future_bar_1m','ref','future_series','query_read_v3'),
    ('concept_daily_1d','fact','concept_daily_1d','query_read_v3'),
    ('concept_daily_1d','ref','concept','query_read_v3'),
    ('concept_daily_1d','ref','concept_stock_membership','query_read_v3'),
    ('concept_daily_1d','fact','stock_daily_1d','query_read_v3'),
    ('stock_research_daily','fact','stock_financial_pit_factor','query_read_v3'),
    ('stock_research_daily','fact','stock_listing_board_history','query_read_v3'),
    ('stock_research_daily','fact','stock_market_indicators_daily','query_read_v3'),
    ('stock_research_daily','fact','stock_money_flow_daily','query_read_v3'),
    ('stock_research_daily','fact','stock_price_band_daily','query_read_v3'),
    ('stock_research_daily','public','capability_cache_rows','query_read_v3')
on conflict (dataset_id,source_schema,source_table) do update set managed_by=excluded.managed_by;

create or replace function audit.bump_dataset_versions()
returns trigger language plpgsql security definer set search_path=pg_catalog,audit as $$
begin
    update audit.dataset_version_state state
    set generation=state.generation+1,updated_at_utc=clock_timestamp()
    where exists (
        select 1 from audit.dataset_version_dependency dependency
        where dependency.dataset_id=state.dataset_id
          and dependency.source_schema=tg_table_schema
          and dependency.source_table=tg_table_name
    );
    return null;
end;
$$;

do $$
declare source_row record;
declare target regclass;
begin
    for source_row in
        select distinct source_schema,source_table
        from audit.dataset_version_dependency
        where managed_by='query_read_v3'
    loop
        target := to_regclass(format('%I.%I',source_row.source_schema,source_row.source_table));
        if target is not null and not exists (
            select 1 from pg_trigger
            where tgrelid=target and tgname='markethub_dataset_version_bump' and not tgisinternal
        ) then
            execute format(
                'create trigger markethub_dataset_version_bump after insert or update or delete or truncate on %s for each statement execute function audit.bump_dataset_versions()',
                target
            );
        end if;
    end loop;
end;
$$;

do $$
declare target regclass;
begin
    foreach target in array array[
        to_regclass('fact.stock_daily_1d'),
        to_regclass('fact.stock_suspension_history'),
        to_regclass('ref.stock'),
        to_regclass('ref.trade_calendar')
    ] loop
        if target is not null and exists (
            select 1 from pg_trigger
            where tgrelid=target and tgname='markethub_stock_daily_dataset_version_bump' and not tgisinternal
        ) then
            execute format('drop trigger markethub_stock_daily_dataset_version_bump on %s',target);
        end if;
    end loop;
end;
$$;

create table if not exists readmodel.dataset_build_state (
    dataset_id text not null,
    dataset_version text not null,
    status text not null check(status in ('coverage_pending','building','parquet_pending','ready','failed','online')),
    source_generation bigint not null,
    coverage_ready boolean not null default false,
    complete boolean not null default false,
    row_count bigint not null default 0,
    checksum_sha256 text,
    built_at_utc timestamptz,
    error_message text not null default '',
    updated_at_utc timestamptz not null default clock_timestamp(),
    primary key(dataset_id,dataset_version),
    check(checksum_sha256 is null or checksum_sha256 ~ '^[0-9a-f]{64}$')
);

create table if not exists readmodel.stock_daily_coverage_day (
    dataset_version text not null,
    trade_date date not null,
    market text not null,
    expected_rows integer not null,
    actual_rows integer not null,
    missing_rows integer not null,
    duplicate_rows integer not null,
    complete boolean not null,
    built_at_utc timestamptz not null default clock_timestamp(),
    primary key(dataset_version,trade_date,market)
);

create table if not exists readmodel.stock_daily_coverage_gap (
    dataset_version text not null,
    trade_date date not null,
    market text not null,
    code text not null,
    reason text not null check(reason in ('missing','duplicate','invalid_required_fields')),
    expected_rows integer not null,
    actual_rows integer not null,
    primary key(dataset_version,trade_date,market,code,reason)
);

create table if not exists readmodel.stock_bar_1m_daily_coverage (
    market text not null,
    code text not null,
    trade_date date not null,
    row_count integer not null,
    first_bar_time timestamp without time zone,
    last_bar_time timestamp without time zone,
    updated_at timestamptz not null default clock_timestamp(),
    primary key(market,code,trade_date)
);

create index if not exists stock_bar_1m_daily_coverage_code_date_idx
on readmodel.stock_bar_1m_daily_coverage(code,trade_date)
include(row_count,first_bar_time,last_bar_time);

grant select on audit.dataset_version_state,audit.dataset_version_dependency,audit.dataset_version_publication to public;
grant select on all tables in schema readmodel to public;
grant insert,update on audit.dataset_version_publication to current_user;

commit;
