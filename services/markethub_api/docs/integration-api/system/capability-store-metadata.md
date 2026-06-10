# Capability Store ?????

???????????????? Store ??? provider ???????????

| capability | result_shape | time_field | key_fields | request_scope_fields | coverage_mode | merge_strategy | store_enabled |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `boards.catalog` | `reference_table` | `as_of_date` | `board_code` | `board_code` | `date_range` | `priority_fallback` | `False` |
| `boards.indicators.money_flow` | `time_series` | `trade_date` | `board_code, trade_date, scope` | `board_code, scope` | `date_range` | `append_dedupe` | `False` |
| `boards.indicators.money_flow.snapshot` | `time_series` | `trade_date` | `board_code, trade_date, scope` | `board_code, scope` | `snapshot` | `append_dedupe` | `False` |
| `boards.members` | `keyed_records` | `join_date` | `board_code, code` | `board_code` | `date_range` | `field_consensus` | `False` |
| `boards.members.history` | `keyed_records` | `effective_date` | `board_code, code` | `board_code` | `date_range` | `field_consensus` | `False` |
| `boards.profile` | `single_record` | `as_of_date` | `board_code` | `board_code` | `date_range` | `priority_fallback` | `False` |
| `boards.quotes.daily` | `time_series` | `trade_time` | `board_code, freq` | `board_code, freq` | `trading_day_range` | `append_dedupe` | `False` |
| `boards.reference.categories` | `reference_table` | `as_of_date` | `category_code` | `board_code` | `date_range` | `priority_fallback` | `False` |
| `indexes.catalog` | `reference_table` | `list_date` | `index_code` | `index_code` | `date_range` | `priority_fallback` | `False` |
| `indexes.members` | `keyed_records` | `trade_date` | `index_code, code` | `index_code` | `date_range` | `field_consensus` | `False` |
| `indexes.profile` | `single_record` | `list_date` | `index_code` | `index_code` | `date_range` | `priority_fallback` | `False` |
| `indexes.quotes.daily` | `time_series` | `trade_time` | `index_code, freq` | `index_code, freq` | `trading_day_range` | `append_dedupe` | `True` |
| `markets.calendar.trading` | `time_series` | `trade_date` | `exchange, trade_date` | `exchange, is_open` | `date_range` | `append_dedupe` | `True` |
| `markets.calendar.trading.next` | `time_series` | `` | `` | `` | `` | `append_dedupe` | `False` |
| `markets.calendar.trading.previous` | `time_series` | `` | `` | `` | `` | `append_dedupe` | `False` |
| `markets.calendar.trading.yearly` | `time_series` | `` | `` | `` | `` | `append_dedupe` | `False` |
| `markets.connect.active_top10` | `time_series` | `trade_date` | `market, trade_date, code, rank` | `market` | `date_range` | `append_dedupe` | `False` |
| `markets.connect.capital_flow` | `time_series` | `trade_date` | `market, trade_date` | `` | `date_range` | `append_dedupe` | `False` |
| `markets.connect.quotas` | `time_series` | `trade_date` | `market, trade_date` | `market` | `date_range` | `append_dedupe` | `False` |
| `markets.events.block_trades` | `time_series` | `trade_date` | `trade_date, code, buyer, seller` | `code` | `event_range` | `append_dedupe` | `False` |
| `markets.events.news` | `event_stream` | `announcement_time` | `event_id` | `event_type, stock_code, sort_by, include_sources, include_content_text` | `event_range` | `append_dedupe` | `True` |
| `markets.indicators.main_capital_flow` | `time_series` | `trade_date` | `market, trade_date` | `` | `date_range` | `append_dedupe` | `False` |
| `markets.participants.dragon_tiger` | `time_series` | `trade_date` | `trade_date, code, reason` | `code` | `date_range` | `append_dedupe` | `False` |
| `markets.participants.dragon_tiger.institutions` | `time_series` | `trade_date` | `trade_date, code, institution_count` | `code` | `date_range` | `append_dedupe` | `False` |
| `markets.participants.hot_money` | `time_series` | `as_of_date` | `name` | `name, tag` | `date_range` | `append_dedupe` | `False` |
| `markets.participants.hot_money.details` | `time_series` | `trade_date` | `trade_date, name, code` | `name` | `date_range` | `append_dedupe` | `False` |
| `markets.trading.open_auctions` | `time_series` | `trade_date` | `code, trade_date, auction_time, session` | `code` | `date_range` | `append_dedupe` | `False` |
| `markets.trading.sessions` | `reference_table` | `as_of_date` | `code` | `codes` | `date_range` | `priority_fallback` | `False` |
| `rankings.research.broker_monthly_picks` | `keyed_records` | `trade_month` | `trade_month, code, institution` | `trade_month` | `date_range` | `field_consensus` | `False` |
| `rankings.research.reports` | `keyed_records` | `trade_date` | `trade_date, code, institution, title` | `` | `date_range` | `field_consensus` | `False` |
| `stocks.catalog` | `reference_table` | `list_date` | `code` | `codes, name, exchange, list_status, include_delisted` | `snapshot` | `priority_fallback` | `False` |
| `stocks.catalog.archive` | `reference_table` | `trade_date` | `code, trade_date` | `code` | `date_range` | `priority_fallback` | `False` |
| `stocks.corporate_actions.dividends` | `time_series` | `announce_date` | `code, announce_date, record_date, ex_date` | `code` | `date_range` | `append_dedupe` | `False` |
| `stocks.corporate_actions.repurchases` | `time_series` | `announce_date` | `code, announce_date, progress` | `code` | `date_range` | `append_dedupe` | `False` |
| `stocks.corporate_actions.rights_issues` | `time_series` | `announce_date` | `code, announce_date, record_date` | `code` | `date_range` | `append_dedupe` | `False` |
| `stocks.corporate_actions.share_changes` | `time_series` | `change_date` | `code, change_date, reason` | `code` | `date_range` | `append_dedupe` | `False` |
| `stocks.corporate_actions.unlock_schedules` | `time_series` | `unlock_date` | `code, unlock_date, holder_type, share_type` | `code` | `date_range` | `append_dedupe` | `False` |
| `stocks.factors.adj` | `keyed_records` | `trade_date` | `code` | `code` | `date_range` | `field_consensus` | `False` |
| `stocks.factors.technical` | `keyed_records` | `trade_date` | `code, trade_date, adjust` | `code` | `date_range` | `field_consensus` | `False` |
| `stocks.finance.audits` | `time_series` | `report_period` | `code, report_period, announce_date` | `code` | `period_range` | `append_dedupe` | `False` |
| `stocks.finance.disclosure_dates` | `time_series` | `report_period` | `code, report_period, plan_date, actual_date` | `code` | `period_range` | `append_dedupe` | `False` |
| `stocks.finance.express` | `time_series` | `report_period` | `code, report_period, announce_date` | `code` | `period_range` | `append_dedupe` | `False` |
| `stocks.finance.forecasts` | `time_series` | `report_period` | `code, report_period, forecast_type` | `code` | `period_range` | `append_dedupe` | `False` |
| `stocks.finance.indicators` | `time_series` | `report_period` | `code, report_period` | `code` | `period_range` | `append_dedupe` | `False` |
| `stocks.finance.main_business` | `time_series` | `report_period` | `code, report_period, classification, segment_name` | `code, classification` | `period_range` | `append_dedupe` | `False` |
| `stocks.finance.statements` | `time_series` | `report_period` | `code, report_period, report_type, statement_type` | `code, report_type` | `period_range` | `append_dedupe` | `False` |
| `stocks.indicators.ah_comparisons` | `keyed_records` | `trade_date` | `code, trade_date` | `code` | `date_range` | `field_consensus` | `False` |
| `stocks.indicators.chip_distribution` | `keyed_records` | `trade_date` | `code, trade_date, price` | `code` | `date_range` | `field_consensus` | `False` |
| `stocks.indicators.chip_performance` | `keyed_records` | `trade_date` | `code, trade_date` | `code` | `date_range` | `field_consensus` | `False` |
| `stocks.indicators.daily_basic` | `keyed_records` | `trade_date` | `code, trade_date` | `code` | `date_range` | `field_consensus` | `False` |
| `stocks.indicators.daily_market_value` | `keyed_records` | `trade_date` | `code, trade_date` | `code` | `date_range` | `field_consensus` | `False` |
| `stocks.indicators.daily_valuation` | `keyed_records` | `trade_date` | `code, trade_date` | `code` | `date_range` | `field_consensus` | `False` |
| `stocks.indicators.money_flow` | `time_series` | `trade_date` | `code, trade_date` | `code, view` | `date_range` | `append_dedupe` | `False` |
| `stocks.indicators.premarket` | `keyed_records` | `trade_date` | `code, trade_date` | `code` | `date_range` | `field_consensus` | `False` |
| `stocks.indicators.risk_flags` | `keyed_records` | `start_date` | `code, flag_type, start_date, end_date, status` | `flag_type, status` | `date_range` | `field_consensus` | `False` |
| `stocks.ownership.ccass_holding_details` | `time_series` | `trade_date` | `code, trade_date, participant_id` | `code` | `date_range` | `append_dedupe` | `False` |
| `stocks.ownership.ccass_holdings` | `time_series` | `trade_date` | `code, trade_date` | `code` | `date_range` | `append_dedupe` | `False` |
| `stocks.ownership.hk_connect_holdings` | `time_series` | `trade_date` | `code, trade_date` | `code` | `date_range` | `append_dedupe` | `False` |
| `stocks.ownership.pledges.details` | `time_series` | `start_date` | `code, holder_name, start_date, end_date, status` | `code` | `date_range` | `append_dedupe` | `False` |
| `stocks.ownership.pledges.stats` | `time_series` | `trade_date` | `code, trade_date` | `code` | `date_range` | `append_dedupe` | `False` |
| `stocks.ownership.shareholders.changes` | `time_series` | `trade_date` | `code, trade_date` | `code` | `date_range` | `append_dedupe` | `False` |
| `stocks.ownership.shareholders.count` | `time_series` | `trade_date` | `code, trade_date` | `code` | `date_range` | `append_dedupe` | `False` |
| `stocks.ownership.shareholders.top10` | `time_series` | `report_period` | `code, report_period, rank, shareholder_name` | `code` | `date_range` | `append_dedupe` | `False` |
| `stocks.ownership.shareholders.top10_float` | `time_series` | `report_period` | `code, report_period, rank, shareholder_name` | `code` | `date_range` | `append_dedupe` | `False` |
| `stocks.profile.basic` | `single_record` | `list_date` | `code` | `code` | `date_range` | `priority_fallback` | `False` |
| `stocks.profile.company` | `single_record` | `as_of_date` | `code` | `code` | `date_range` | `priority_fallback` | `False` |
| `stocks.profile.management_rewards` | `keyed_records` | `ann_date` | `code, ann_date, name, title` | `code` | `date_range` | `field_consensus` | `False` |
| `stocks.profile.managers` | `keyed_records` | `as_of_date` | `code, name, title, begin_date` | `code` | `date_range` | `field_consensus` | `False` |
| `stocks.profile.name_history` | `keyed_records` | `start_date` | `code` | `code` | `date_range` | `field_consensus` | `False` |
| `stocks.quotes.auctions` | `time_series` | `trade_date` | `code, freq, adjust` | `code, freq, adjust` | `date_range` | `append_dedupe` | `False` |
| `stocks.quotes.daily` | `time_series` | `trade_time` | `code, freq, adjust` | `code, freq, adjust` | `trading_day_range` | `append_dedupe` | `True` |
| `stocks.quotes.daily_snapshot` | `time_series` | `trade_time` | `code, freq, adjust` | `trade_date` | `snapshot` | `append_dedupe` | `True` |
| `stocks.quotes.intraday` | `time_series` | `trade_time` | `code, freq, adjust` | `code, freq, adjust` | `minute_range` | `append_dedupe` | `True` |
| `stocks.reference.bse_code_mappings` | `reference_table` | `effective_date` | `old_code, new_code, effective_date` | `code` | `date_range` | `priority_fallback` | `False` |
| `stocks.reference.hk_connect_targets` | `reference_table` | `effective_date` | `code, direction, effective_date` | `code` | `date_range` | `priority_fallback` | `False` |
| `stocks.research.reports` | `time_series` | `report_date` | `code, report_date, institution, title` | `code` | `event_range` | `append_dedupe` | `False` |
| `stocks.research.surveys` | `time_series` | `survey_date` | `code, survey_date, org_name, announcement_date` | `code` | `event_range` | `append_dedupe` | `False` |
| `stocks.signals.hl` | `keyed_records` | `trade_date` | `code, trade_date, signal, first_extreme` | `code` | `date_range` | `field_consensus` | `False` |
| `stocks.signals.nine_turn` | `keyed_records` | `trade_time` | `code, trade_time, freq` | `code, freq` | `date_range` | `field_consensus` | `False` |
