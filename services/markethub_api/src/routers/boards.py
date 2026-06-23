from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Query

from data_threads import run_data_task
from services import boards
from services.common import filter_response_fields


router = APIRouter()

BOARD_QUOTE_FIELDS = {"board_code", "board_name", "trade_time", "freq", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "volume", "amount"}
BOARD_MONEY_FLOW_FIELDS = {"board_code", "trade_date", "scope", "inflow", "outflow", "net_inflow"}


def _dump_item_list(loader: Callable[..., list[object]], args: tuple[object, ...]) -> list[dict[str, object]]:
    return [item.model_dump() for item in loader(*args)]


def _dump_optional_item(loader: Callable[..., object], args: tuple[object, ...]) -> dict[str, object]:
    item = loader(*args)
    return item.model_dump() if item is not None else {}


def _filter_items(loader: Callable[..., list[object]], args: tuple[object, ...], fields: str, allowed_fields: set[str]) -> list[dict[str, object]]:
    items = loader(*args)
    return filter_response_fields(items, fields, allowed_fields)


@router.get("/api/boards/quotes", summary='返回单个或多个板块的行情序列', description='`GET` 返回单个或多个板块的行情序列。\n\n## 查询参数\n\n- `board_code`（`str`）：单个板块代码；与 `board_codes` 至少传一个。\n- `board_codes`（`str`）：多个板块代码，逗号分隔；与 `board_code` 至少传一个。\n- `freq`（`str`，默认 `1d`）：行情频率，可选 `1d`、`1w`、`1mo`。\n- `trade_date`（`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `count`（`int | None`）：每个板块返回的最近记录条数。\n- `fields`（`str`）：按逗号指定返回字段，不传返回全部字段。\n- `limit`（`int`，默认 `200`，范围 `1-5000`）：返回记录上限。\n\n## 返回类型\n\n顶层返回 `list[BoardQuoteItem]`。\n\n## 返回字段\n\n- `board_code`（`str`）：板块代码。\n- `board_name`（`str`）：板块名称。\n- `trade_time`（`str`）：交易日期。\n- `freq`（`str`）：数据频率。\n- `open`（`float | None`）：开盘价。\n- `high`（`float | None`）：最高价。\n- `low`（`float | None`）：最低价。\n- `close`（`float | None`）：收盘价。\n- `pre_close`（`float | None`）：前收盘价。\n- `change`（`float | None`）：涨跌额。\n- `pct_chg`（`float | None`）：涨跌幅，单位 `%`。\n- `volume`（`float | None`）：成交量。\n- `amount`（`float | None`）：成交额。\n\n## 补充说明\n\n- `freq=1d` 且指定 `trade_date` 的单日查询直接读取本地 `fact.board_daily_1d`，并通过 `ref.board` 补齐 `board_name`。\n- `pre_close`、`change`、`pct_chg` 会基于前一个已有交易日收盘价派生。\n- 没有本地数据时快速返回空数组，不在请求线程内触发外源补齐。')
async def api_board_quotes(
    board_code: str = Query("", description='板块代码。'),
    board_codes: str = Query("", description='多个板块代码，逗号分隔；与 `board_code` 至少传一个。'),
    freq: str = Query("1d", description='数据频率。'),
    trade_date: str = Query("", description='交易日期，格式 `YYYY-MM-DD`。'),
    start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'),
    end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'),
    start_time: str = Query(""),
    end_time: str = Query(""),
    count: int | None = Query(None, ge=1, description='每个板块返回的最近记录条数。'),
    fields: str = Query("", description='按逗号指定返回字段，不传返回全部字段。'),
    limit: int = Query(200, ge=1, le=5000, description='返回记录上限。'),
) -> list[dict[str, object]]:
    args = (board_code, board_codes, freq, trade_date, start_date, end_date, start_time, end_time, count, limit)
    return await run_data_task(_filter_items, boards.get_quotes, args, fields, BOARD_QUOTE_FIELDS)


@router.get("/api/boards/quotes/daily-snapshot", summary='返回指定交易日的全市场板块日线快照', description='`GET` 返回指定交易日的全市场板块日线快照。\n\n## 查询参数\n\n- `trade_date`（`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `fields`（`str`）：按逗号指定返回字段，不传返回全部字段。\n- `limit`（`int`，默认 `10000`，范围 `1-10000`）：返回记录上限。\n- `offset`（`int`，默认 `0`）：分页偏移量。\n\n## 返回类型\n\n顶层返回 `list[BoardQuoteItem]`。\n\n## 返回字段\n\n- `board_code`（`str`）：板块代码。\n- `board_name`（`str`）：板块名称。\n- `trade_time`（`str`）：交易日期。\n- `freq`（`str`）：固定为 `1d`。\n- `open`（`float | None`）：开盘价。\n- `high`（`float | None`）：最高价。\n- `low`（`float | None`）：最低价。\n- `close`（`float | None`）：收盘价。\n- `pre_close`（`float | None`）：前收盘价。\n- `change`（`float | None`）：涨跌额。\n- `pct_chg`（`float | None`）：涨跌幅，单位 `%`。\n- `volume`（`float | None`）：成交量。\n- `amount`（`float | None`）：成交额。\n\n## 补充说明\n\n- 该接口直接读取本地 `fact.board_daily_1d`，并通过 `ref.board` 补齐 `board_name`。\n- 本地快照为空时，按活跃板块目录请求 `boards.quotes.daily`，由该 capability 的 provider package 补齐日线字段。\n- 调用方不需要传 `board_code` 或 `board_codes`，适合复盘助手一次性获取全市场板块快照。')
async def api_board_daily_snapshot(
    trade_date: str = Query(..., description='交易日期，格式 `YYYY-MM-DD`。'),
    fields: str = Query("", description='按逗号指定返回字段，不传返回全部字段。'),
    limit: int = Query(10000, ge=1, le=10000, description='返回记录上限。'),
    offset: int = Query(0, ge=0, description='分页偏移量。'),
) -> list[dict[str, object]]:
    """获取指定交易日全市场板块快照，按涨跌幅排序"""
    args = (trade_date, limit, offset)
    return await run_data_task(_filter_items, boards.get_market_daily_snapshot, args, fields, BOARD_QUOTE_FIELDS)


@router.get("/api/boards/catalog", summary='返回板块目录清单', description='`GET` 返回板块目录清单。\n\n## 查询参数\n\n- `category`（类型：`str`）：分类筛选。\n- `market`（类型：`str`）：市场筛选。\n- `status`（类型：`str`）：状态筛选。\n- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。\n- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。\n\n## 返回类型\n\n顶层返回 `list[BoardCatalogItem]`。\n\n## 返回字段\n\n- `board_code`（`str`）：板块代码。\n- `board_name`（`str`）：板块名称。\n- `category`（`str`）：分类。\n- `market`（`str`）：板块所属市场，默认 A 股口径。\n- `status`（`str`）：板块状态。')
async def api_board_catalog(
    category: str = Query("", description='分类。'),
    market: str = Query("", description='板块所属市场，默认 A 股口径。'),
    status: str = Query("", description='板块状态。'),
    limit: int = Query(200, ge=1, le=5000, description='返回记录上限。'),
    offset: int = Query(0, ge=0, description='结果偏移量，从 `0` 开始。'),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, boards.get_catalog, (category, market, status, limit, offset))


@router.get("/api/boards/{board_code}/profile", summary='返回单个板块的基础资料', description='`GET` 返回单个板块的基础资料。\n\n## 路径参数\n\n- `board_code`（类型：`str`）：板块代码。\n\n## 返回类型\n\n顶层返回 `BoardCatalogItem`；查不到对应记录时返回空对象 `{}`。\n\n## 返回字段\n\n- `board_code`（`str`）：板块代码。\n- `board_name`（`str`）：板块名称。\n- `category`（`str`）：分类。\n- `market`（`str`）：板块所属市场，默认 A 股口径。\n- `status`（`str`）：板块状态。\n\n## 补充说明\n\n- 查不到对应记录时返回空对象 `{}`。')
async def api_board_profile(board_code: str) -> dict[str, object]:
    return await run_data_task(_dump_optional_item, boards.get_profile, (board_code,))


@router.get("/api/boards/{board_code}/members", summary='返回单个板块在指定交易日的成分列表', description='`GET` 返回单个板块在指定交易日的成分列表。\n\n## 路径参数\n\n- `board_code`（类型：`str`）：板块代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[BoardMemberItem]`。\n\n## 返回字段\n\n- `board_code`（`str`）：板块代码。\n- `code`（`str`）：股票代码。\n- `name`（`str`）：成分股名称。\n- `weight`（`float | None`）：权重。\n- `join_date`（`str`）：纳入日期。')
async def api_board_members(
    board_code: str,
    trade_date: str = Query("", description='交易日期，格式 `YYYY-MM-DD`。'),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, boards.get_members, (board_code, trade_date))


@router.get("/api/boards/{board_code}/members/history", summary='返回单个板块的成分变动历史', description='`GET` 返回单个板块的成分变动历史。\n\n## 路径参数\n\n- `board_code`（类型：`str`）：板块代码。\n\n## 查询参数\n\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[BoardMemberHistoryItem]`。\n\n## 返回字段\n\n- `board_code`（`str`）：板块代码。\n- `code`（`str`）：股票代码。\n- `name`（`str`）：成分股名称。\n- `effective_date`（`str`）：生效日期。\n- `action`（`str`）：变动动作。')
async def api_board_members_history(
    board_code: str,
    start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'),
    end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, boards.get_member_history, (board_code, start_date, end_date))


@router.get("/api/boards/{board_code}/indicators/money-flow", summary='返回单个板块的资金流指标', description='`GET` 返回单个板块的资金流指标。\n\n## 路径参数\n\n- `board_code`（类型：`str`）：板块代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `scope`（类型：`str`；默认：`board`）：资金流统计口径，可选 `board`、`industry`。\n\n## 返回类型\n\n顶层返回 `list[BoardMoneyFlowItem]`。\n\n## 返回字段\n\n- `board_code`（`str`）：板块代码。\n- `trade_date`（`str`）：交易日期。\n- `scope`（`str`）：统计口径。\n- `inflow`（`float | None`）：流入金额。\n- `outflow`（`float | None`）：流出金额。\n- `net_inflow`（`float | None`）：净流入金额。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 和 `end_date` 用于区间筛选。\n- 如果需求是“按 `trade_date` 直接拿全市场板块资金流快照”，请改用 `GET /api/boards/indicators/money-flow`，不要再循环传 `board_code`。')
async def api_board_money_flow(
    board_code: str,
    trade_date: str = Query("", description='交易日期。'),
    start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'),
    end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'),
    scope: str = Query("board", description='统计口径。'),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, boards.get_money_flow, (board_code, trade_date, start_date, end_date, scope))


@router.get("/api/boards/indicators/money-flow", summary='返回指定交易日的全市场板块资金流快照', description='`GET` 返回指定交易日的全市场板块资金流快照。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `scope`（类型：`str`；默认：`board`）：资金流统计口径，可选 `board`、`industry`。\n- `fields`（类型：`str`）：可选返回字段列表，逗号分隔。\n- `limit`（类型：`int`；默认：`10000`；最小值：`1`；最大值：`10000`）：单次返回的最大记录数。\n- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量。\n\n## 返回类型\n\n顶层返回 `list[BoardMoneyFlowItem]`。\n\n## 返回字段\n\n- `board_code`（`str`）：板块代码。\n- `trade_date`（`str`）：交易日期。\n- `scope`（`str`）：统计口径。\n- `inflow`（`float | None`）：流入金额。\n- `outflow`（`float | None`）：流出金额。\n- `net_inflow`（`float | None`）：净流入金额。\n\n## 补充说明\n\n- 这个入口用于“按交易日读取全市场板块资金流快照”，不需要传 `board_code`。\n- 当前由 `static_core` 承担本地快照能力，后续可通过 Capability Matrix 接入补源。\n- 如需分页读取，可配合 `limit` 和 `offset` 使用。')
async def api_board_money_flow_daily_snapshot(
    trade_date: str = Query(..., description='交易日期。'),
    scope: str = Query("board", description='统计口径。'),
    fields: str = Query("", description='可选返回字段列表，逗号分隔。'),
    limit: int = Query(10000, ge=1, le=10000, description='单次返回的最大记录数。'),
    offset: int = Query(0, ge=0, description='结果偏移量。'),
) -> list[dict[str, object]]:
    """批量获取全市场板块资金流快照"""
    args = (trade_date, scope, limit, offset)
    return await run_data_task(_filter_items, boards.get_market_money_flow, args, fields, BOARD_MONEY_FLOW_FIELDS)


@router.get("/api/boards/reference/categories", summary='返回板块分类目录', description='`GET` 返回板块分类目录。\n\n## 查询参数\n\n- `parent_code`（类型：`str`）：父级分类代码。\n- `level`（类型：`int | None`；允许空值；最小值：`1`）：分类层级筛选，从 `1` 开始。\n\n## 返回类型\n\n顶层返回 `list[BoardCategoryItem]`。\n\n## 返回字段\n\n- `category_code`（`str`）：分类代码。\n- `category_name`（`str`）：分类名称。\n- `parent_code`（`str`）：父级分类代码。\n- `level`（`int | None`）：层级。\n- `sort_order`（`int | None`）：排序值。')
async def api_board_categories(
    parent_code: str = Query("", description='父级分类代码。'),
    level: int | None = Query(None, ge=1, description='层级。'),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, boards.get_categories, (parent_code, level))
