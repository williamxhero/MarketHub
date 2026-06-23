from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Query

from data_threads import run_data_task
from services import markets


router = APIRouter()


def _dump_item_list(loader: Callable[..., list[object]], args: tuple[object, ...]) -> list[dict[str, object]]:
    return [item.model_dump() for item in loader(*args)]


@router.get("/api/markets/calendar/trading", summary='返回交易日历列表', description='`GET` 返回交易日历列表。\n\n## 查询参数\n\n- `exchange`（`str`，默认 `SSE`）：交易所标识。\n- `start_date`（`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `is_open`（`bool | None`）：是否只返回开市日。\n\n## 返回类型\n\n顶层返回 `list[TradingCalendarItem]`。\n\n## 返回字段\n\n- `exchange`（`str`）：交易所标识，如 `SSE`、`SZSE`、`BSE`、`HKEX`。\n- `trade_date`（`str`）：交易日期。\n- `is_open`（`bool`）：是否为开市日。\n\n## 补充说明\n\n- 默认 provider 候选是 `static_core -> Tushare -> AKShare emergency`。\n- 主路径先读取 QuoteMux Store 的 `markets.calendar.trading`，未命中时按 Capability Matrix 并发读取完整日历。\n- Runtime Profile 会按 Capability Matrix 勾选的源并发读取完整日历，再按该 capability 的 `merge_strategy` 合并成一份结果。\n- 最终的 `is_open` 过滤在合并完成后再执行，因此不会因为先过滤开市日而误判缺口。\n- `AKShare emergency` 只用于应急日期覆盖，不视为正式 `trade_cal` 等价物。')
async def api_market_trading_calendar(exchange: str = Query("SSE", description='交易所标识，如 `SSE`、`SZSE`、`BSE`、`HKEX`。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'), is_open: bool | None = Query(None, description='是否为开市日。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_trading_calendar, (exchange, start_date, end_date, is_open))


@router.get("/api/markets/calendar/trading/previous", summary='返回给定日期之前的最近若干个交易日', description='`GET` 返回给定日期之前的最近若干个交易日。\n\n## 查询参数\n\n- `exchange`（类型：`str`；默认：`SSE`）：交易所标识。\n- `trade_date`（类型：`str`）：参考交易日，返回该日期之前的开市日，格式 `YYYY-MM-DD`。\n- `n`（类型：`int`；默认：`1`；范围：`1-5000`）：返回记录数量。\n\n## 返回类型\n\n顶层返回 `list[TradingCalendarItem]`。\n\n## 返回字段\n\n- `exchange`（`str`）：交易所标识，如 `SSE`、`SZSE`、`BSE`、`HKEX`。\n- `trade_date`（`str`）：交易日日期。\n- `is_open`（`bool`）：是否为开市日。\n\n## 补充说明\n\n- 结果按日期升序返回，最多返回 `n` 个开市日。\n- 该接口是显式登记在 `DERIVED_CAPABILITY_BASE_IDS` 的派生视图；不独立配置 TTL、缓存策略、采集策略或更新频率。\n- 执行时读取主 capability `markets.calendar.trading` 的 QuoteMux Store 和配置，再从交易日历中截取 `trade_date` 之前最多 `n` 个开市日。')
async def api_market_previous_trading_days(
    exchange: str = Query("SSE", description='交易所标识，如 `SSE`、`SZSE`、`BSE`、`HKEX`。'),
    trade_date: str = Query("", description='交易日日期。'),
    n: int = Query(1, ge=1, le=5000, description='返回记录数量。'),
    date: str = Query(""),
    count: int | None = Query(None, ge=1, le=5000),
) -> list[dict[str, object]]:
    actual_trade_date = trade_date if trade_date != "" else date
    actual_count = n if count is None else count
    return await run_data_task(_dump_item_list, markets.get_previous_trading_days, (exchange, actual_trade_date, actual_count))


@router.get("/api/markets/calendar/trading/next", summary='返回给定日期之后的最近若干个交易日', description='`GET` 返回给定日期之后的最近若干个交易日。\n\n## 查询参数\n\n- `exchange`（类型：`str`；默认：`SSE`）：交易所标识。\n- `trade_date`（类型：`str`）：参考交易日，返回该日期之后的开市日，格式 `YYYY-MM-DD`。\n- `n`（类型：`int`；默认：`1`；范围：`1-5000`）：返回记录数量。\n\n## 返回类型\n\n顶层返回 `list[TradingCalendarItem]`。\n\n## 返回字段\n\n- `exchange`（`str`）：交易所标识，如 `SSE`、`SZSE`、`BSE`、`HKEX`。\n- `trade_date`（`str`）：交易日日期。\n- `is_open`（`bool`）：是否为开市日。\n\n## 补充说明\n\n- 结果按日期升序返回，最多返回 `n` 个开市日。\n- 该接口是显式登记在 `DERIVED_CAPABILITY_BASE_IDS` 的派生视图；不独立配置 TTL、缓存策略、采集策略或更新频率。\n- 执行时读取主 capability `markets.calendar.trading` 的 QuoteMux Store 和配置，再从交易日历中截取 `trade_date` 之后最多 `n` 个开市日。')
async def api_market_next_trading_days(exchange: str = Query("SSE", description='交易所标识，如 `SSE`、`SZSE`、`BSE`、`HKEX`。'), trade_date: str = Query("", description='交易日日期。'), n: int = Query(1, ge=1, le=5000, description='返回记录数量。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_next_trading_days, (exchange, trade_date, n))


@router.get("/api/markets/calendar/trading/yearly", summary='返回指定年份区间的交易日历汇总', description='`GET` 返回指定年份区间的交易日历汇总。\n\n## 查询参数\n\n- `exchange`（类型：`str`；默认：`SSE`）：交易所标识。\n- `start_year`（类型：`int`；默认：`2024`；范围：`1990-2100`）：起始年份。\n- `end_year`（类型：`int`；默认：`2026`；范围：`1990-2100`）：结束年份。\n\n## 返回类型\n\n顶层返回 `list[TradingCalendarItem]`。\n\n## 返回字段\n\n- `exchange`（`str`）：交易所标识，如 `SSE`、`SZSE`、`BSE`、`HKEX`。\n- `trade_date`（`str`）：交易日日期。\n- `is_open`（`bool`）：是否为开市日。\n\n## 补充说明\n\n- 该接口是显式登记在 `DERIVED_CAPABILITY_BASE_IDS` 的派生视图；不独立配置 TTL、缓存策略、采集策略或更新频率。\n- 执行时读取主 capability `markets.calendar.trading` 的 QuoteMux Store 和配置，再按 `start_year` / `end_year` 生成年度日期范围。\n- 默认 provider 候选源继承主交易日历：`static_core -> Tushare -> AKShare emergency`。')
async def api_market_yearly_trading_calendar(exchange: str = Query("SSE", description='交易所标识，如 `SSE`、`SZSE`、`BSE`、`HKEX`。'), start_year: int = Query(2024, ge=1990, le=2100, description='起始年份。'), end_year: int = Query(2026, ge=1990, le=2100, description='结束年份。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_yearly_trading_calendar, (exchange, start_year, end_year))


@router.get("/api/markets/indicators/main-capital-flow", summary='返回市场主力资金流指标', description='`GET` 返回市场主力资金流指标。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[MarketCapitalFlowItem]`。\n\n## 返回字段\n\n- `trade_date`（`str`）：交易日期。\n- `market`（`str`）：市场范围标识，如沪市、深市或全市场口径。\n- `main_inflow`（`float | None`）：主力流入金额。\n- `main_outflow`（`float | None`）：主力流出金额。\n- `net_inflow`（`float | None`）：净流入金额。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_market_main_capital_flow(trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_main_capital_flow, (trade_date, start_date, end_date))


@router.get("/api/markets/connect/capital-flow", summary='返回沪深港通资金流向数据', description='`GET` 返回沪深港通资金流向数据。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[ConnectCapitalFlowItem]`。\n\n## 返回字段\n\n- `trade_date`（`str`）：交易日期。\n- `market`（`str`）：互联互通市场方向，如沪股通、深股通或港股通。\n- `buy_amount`（`float | None`）：买入金额。\n- `sell_amount`（`float | None`）：卖出金额。\n- `net_amount`（`float | None`）：净买入金额。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_market_connect_capital_flow(trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_connect_capital_flow, (trade_date, start_date, end_date))


@router.get("/api/markets/connect/quotas", summary='返回沪深港通额度使用情况', description='`GET` 返回沪深港通额度使用情况。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `type`（类型：`str`）：互联互通额度类型筛选，如沪股通、深股通或港股通。\n\n## 返回类型\n\n顶层返回 `list[ConnectQuotaItem]`。\n\n## 返回字段\n\n- `trade_date`（`str`）：交易日期。\n- `market`（`str`）：互联互通市场方向，如沪股通、深股通或港股通。\n- `quota_total`（`float | None`）：总额度。\n- `quota_balance`（`float | None`）：剩余额度。\n- `quota_used`（`float | None`）：已使用额度。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_market_connect_quotas(trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'), type: str = Query("", description='互联互通额度类型筛选，如沪股通、深股通或港股通。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_connect_quotas, (trade_date, start_date, end_date, type))


@router.get("/api/markets/connect/active-top10", summary='返回沪深港通活跃成交前十明细', description='`GET` 返回沪深港通活跃成交前十明细。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `type`（类型：`str`）：互联互通市场类型筛选，如沪股通、深股通或港股通。\n- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。\n\n## 返回类型\n\n顶层返回 `list[ConnectActiveTop10Item]`。\n\n## 返回字段\n\n- `trade_date`（`str`）：交易日期。\n- `market`（`str`）：互联互通市场方向，如沪股通、深股通或港股通。\n- `code`（`str`）：股票代码。\n- `name`（`str`）：证券简称。\n- `rank`（`int | None`）：排名。\n- `buy_amount`（`float | None`）：买入金额。\n- `sell_amount`（`float | None`）：卖出金额。\n- `net_amount`（`float | None`）：净买入金额。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_market_connect_active_top10(trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'), type: str = Query("", description='互联互通市场类型筛选，如沪股通、深股通或港股通。'), limit: int = Query(200, ge=1, le=5000, description='返回记录上限。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_connect_active_top10, (trade_date, start_date, end_date, type, limit))


@router.get("/api/markets/events/block-trades", summary='返回市场大宗交易明细', description='`GET` 返回市场大宗交易明细。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `code`（类型：`str`）：股票代码筛选。\n- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。\n\n## 返回类型\n\n顶层返回 `list[BlockTradeItem]`。\n\n## 返回字段\n\n- `trade_date`（`str`）：交易日期。\n- `code`（`str`）：股票代码。\n- `name`（`str`）：证券简称。\n- `price`（`float | None`）：价格。\n- `volume`（`float | None`）：成交量。\n- `amount`（`float | None`）：成交额。\n- `buyer`（`str`）：买方营业部或席位名称。\n- `seller`（`str`）：卖方营业部或席位名称。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_market_block_trades(trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'), code: str = Query("", description='股票代码。'), limit: int = Query(200, ge=1, le=5000, description='返回记录上限。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_block_trades, (trade_date, start_date, end_date, code, limit))


@router.get("/api/markets/participants/dragon-tiger", summary='返回龙虎榜成交明细', description='`GET` 返回龙虎榜成交明细。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `code`（类型：`str`）：股票代码筛选。\n- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。\n\n## 返回类型\n\n顶层返回 `list[DragonTigerItem]`。\n\n## 返回字段\n\n- `trade_date`（`str`）：交易日期。\n- `code`（`str`）：股票代码。\n- `name`（`str`）：证券简称。\n- `reason`（`str`）：上榜原因。\n- `buy_amount`（`float | None`）：买入金额。\n- `sell_amount`（`float | None`）：卖出金额。\n- `net_amount`（`float | None`）：净买入金额。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_market_dragon_tiger(trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'), code: str = Query("", description='股票代码。'), limit: int = Query(200, ge=1, le=5000, description='返回记录上限。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_dragon_tiger, (trade_date, start_date, end_date, code, limit))


@router.get("/api/markets/participants/dragon-tiger/institutions", summary='返回龙虎榜机构席位明细', description='`GET` 返回龙虎榜机构席位明细。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `code`（类型：`str`）：股票代码筛选。\n- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。\n\n## 返回类型\n\n顶层返回 `list[DragonTigerInstitutionItem]`。\n\n## 返回字段\n\n- `trade_date`（`str`）：交易日期。\n- `code`（`str`）：股票代码。\n- `name`（`str`）：证券简称。\n- `buy_amount`（`float | None`）：买入金额。\n- `sell_amount`（`float | None`）：卖出金额。\n- `net_amount`（`float | None`）：净买入金额。\n- `institution_count`（`int | None`）：机构席位数量。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_market_dragon_tiger_institutions(trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'), code: str = Query("", description='股票代码。'), limit: int = Query(200, ge=1, le=5000, description='返回记录上限。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_dragon_tiger_institutions, (trade_date, start_date, end_date, code, limit))


@router.get("/api/markets/participants/hot-money", summary='返回游资营业部榜单', description='`GET` 返回游资营业部榜单。\n\n## 查询参数\n\n- `name`（类型：`str`）：游资或营业部名称关键字。\n- `tag`（类型：`str`）：游资标签筛选。\n- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。\n- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。\n\n## 返回类型\n\n顶层返回 `list[HotMoneyProfileItem]`。\n\n## 返回字段\n\n- `name`（`str`）：游资或营业部名称。\n- `alias`（`str`）：别名。\n- `tag`（`str`）：标签。\n- `style`（`str`）：风格标签。')
async def api_market_hot_money(name: str = Query("", description='游资或营业部名称。'), tag: str = Query("", description='标签。'), limit: int = Query(200, ge=1, le=5000, description='返回记录上限。'), offset: int = Query(0, ge=0, description='结果偏移量，从 `0` 开始。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_hot_money, (name, tag, limit, offset))


@router.get("/api/markets/participants/hot-money/details", summary='返回游资营业部交易明细', description='`GET` 返回游资营业部交易明细。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `name`（类型：`str`）：游资或营业部名称筛选。\n- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。\n- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。\n\n## 返回类型\n\n顶层返回 `list[HotMoneyDetailItem]`。\n\n## 返回字段\n\n- `trade_date`（`str`）：交易日期。\n- `name`（`str`）：游资或营业部名称。\n- `code`（`str`）：股票代码。\n- `stock_name`（`str`）：股票名称。\n- `buy_amount`（`float | None`）：买入金额。\n- `sell_amount`（`float | None`）：卖出金额。\n- `net_amount`（`float | None`）：净买入金额。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_market_hot_money_details(trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'), name: str = Query("", description='游资或营业部名称。'), limit: int = Query(200, ge=1, le=5000, description='返回记录上限。'), offset: int = Query(0, ge=0, description='结果偏移量，从 `0` 开始。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_hot_money_details, (trade_date, start_date, end_date, name, limit, offset))


@router.get("/api/markets/trading/open-auctions", summary='返回市场开盘竞价汇总', description='`GET` 返回市场开盘竞价汇总。\n\n## 查询参数\n\n- `codes`（类型：`str`）：股票代码列表，逗号分隔。\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `instrument_type`（类型：`str`；默认：`stock`）：标的类型，当前实现仅按股票口径返回。\n\n## 返回类型\n\n顶层返回 `list[AuctionItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `auction_time`（`str`）：竞价时间。\n- `price`（`float | None`）：价格。\n- `volume`（`float | None`）：成交量。\n- `amount`（`float | None`）：成交额。\n- `session`（`str`）：竞价时段，如开盘竞价。\n\n## 补充说明\n\n- `instrument_type` 当前不会改变返回口径，接口始终按股票竞价数据返回。')
async def api_market_open_auctions(codes: str = Query("", description='股票代码列表，逗号分隔。'), trade_date: str = Query("", description='交易日期。'), instrument_type: str = Query("stock", description='标的类型，当前实现仅按股票口径返回。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_open_auctions, (codes, trade_date, instrument_type))


@router.get("/api/markets/trading/sessions", summary='返回交易时段定义', description='`GET` 返回交易时段定义。\n\n## 查询参数\n\n- `codes`（类型：`str`）：股票代码列表，逗号分隔；不传时返回默认交易时段定义。\n\n## 返回类型\n\n顶层返回 `list[TradingSessionItem]`。\n\n## 返回字段\n\n- `code`（`str`）：证券代码。\n- `session_name`（`str`）：交易时段名称，如集合竞价、连续竞价。\n- `start_time`（`str`）：开始时间。\n- `end_time`（`str`）：结束时间。\n- `timezone`（`str`）：时区标识。')
async def api_market_sessions(codes: str = Query("", description='股票代码列表，逗号分隔；不传时返回默认交易时段定义。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, markets.get_sessions, (codes,))
