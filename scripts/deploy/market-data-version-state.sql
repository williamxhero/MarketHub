begin;

lock table
    fact.stock_daily_1d,
    fact.stock_financial_pit_factor,
    fact.stock_listing_board_history,
    fact.stock_market_indicators_daily,
    fact.stock_money_flow_daily,
    fact.stock_price_band_daily,
    fact.concept_daily_1d,
    audit.stock_bar_1m_write_event,
    ref.concept_stock_membership,
    ref.concept,
    ref.stock,
    ref.trade_calendar,
    public.capability_cache_rows
in share row exclusive mode;

create table if not exists audit.market_data_version_state (
    singleton boolean primary key default true check (singleton),
    baseline_id text not null,
    generation bigint not null check (generation >= 1),
    updated_at_utc timestamptz not null default clock_timestamp()
);

insert into audit.market_data_version_state (singleton, baseline_id, generation)
values (
    true,
    md5(clock_timestamp()::text || random()::text || txid_current()::text),
    1
)
on conflict (singleton) do nothing;

create or replace function audit.bump_market_data_version()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, audit
as $$
begin
    update audit.market_data_version_state
    set generation = generation + 1,
        updated_at_utc = clock_timestamp()
    where singleton = true;
    return null;
end;
$$;

do $$
declare
    target regclass;
begin
    foreach target in array array[
        'fact.stock_daily_1d'::regclass,
        'fact.stock_financial_pit_factor'::regclass,
        'fact.stock_listing_board_history'::regclass,
        'fact.stock_market_indicators_daily'::regclass,
        'fact.stock_money_flow_daily'::regclass,
        'fact.stock_price_band_daily'::regclass,
        'fact.concept_daily_1d'::regclass,
        'audit.stock_bar_1m_write_event'::regclass,
        'ref.concept_stock_membership'::regclass,
        'ref.concept'::regclass,
        'ref.stock'::regclass,
        'ref.trade_calendar'::regclass,
        'public.capability_cache_rows'::regclass
    ]
    loop
        if not exists (
            select 1
            from pg_trigger
            where tgrelid = target
              and tgname = 'markethub_market_data_version_bump'
              and not tgisinternal
        ) then
            execute format(
                'create trigger markethub_market_data_version_bump after insert or update or delete or truncate on %s for each statement execute function audit.bump_market_data_version()',
                target
            );
        end if;
    end loop;
end;
$$;

create or replace function audit.bump_market_data_version_on_ddl()
returns event_trigger
language plpgsql
security definer
set search_path = pg_catalog, audit
as $$
declare
    should_bump boolean;
begin
    select exists (
        select 1
        from pg_event_trigger_ddl_commands()
        where coalesce(schema_name, '') not in (
                  'pg_catalog',
                  'information_schema',
                  'pg_toast',
                  '_timescaledb_internal',
                  '_timescaledb_catalog',
                  '_timescaledb_config',
                  '_timescaledb_cache'
              )
          and coalesce(schema_name, '') not like 'pg_temp_%'
          and not (
              schema_name = 'fact'
              and object_identity ~ '_(ts_shadow|ts_shadow_failed|legacy)([._]|$)'
          )
          and not (
              schema_name = 'audit'
              and object_identity ~ 'stock_bar_.*_ts_(forward|reverse)_(delta|journal)'
          )
    ) into should_bump;

    if not should_bump then
        return;
    end if;

    update audit.market_data_version_state
    set generation = generation + 1,
        updated_at_utc = clock_timestamp()
    where singleton = true;
end;
$$;

do $$
begin
    if not exists (
        select 1
        from pg_event_trigger
        where evtname = 'markethub_market_data_version_ddl_bump'
    ) then
        execute 'create event trigger markethub_market_data_version_ddl_bump on ddl_command_end execute function audit.bump_market_data_version_on_ddl()';
    end if;
end;
$$;

grant select on audit.market_data_version_state to public;

commit;
