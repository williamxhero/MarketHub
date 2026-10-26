begin;

set local lock_timeout = '5s';

-- Cache refreshes are not market facts; keep all fact/reference and DDL triggers.
drop trigger if exists markethub_market_data_version_bump on public.capability_cache_rows;
drop trigger if exists markethub_dataset_version_bump on public.capability_cache_rows;
delete from audit.dataset_version_dependency
where source_schema = 'public' and source_table = 'capability_cache_rows';

commit;
