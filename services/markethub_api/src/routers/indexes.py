from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Query

from data_threads import run_data_task
from services import indexes
from services.common import filter_response_fields


router = APIRouter()

INDEX_QUOTE_FIELDS = {"index_code", "trade_time", "freq", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "volume", "amount"}


def _dump_item_list(loader: Callable[..., list[object]], args: tuple[object, ...]) -> list[dict[str, object]]:
    return [item.model_dump() for item in loader(*args)]


def _dump_optional_item(loader: Callable[..., object], args: tuple[object, ...]) -> dict[str, object]:
    item = loader(*args)
    return item.model_dump() if item is not None else {}


def _filter_items(loader: Callable[..., list[object]], args: tuple[object, ...], fields: str, allowed_fields: set[str]) -> list[dict[str, object]]:
    items = loader(*args)
    return filter_response_fields(items, fields, allowed_fields)


@router.get("/api/indexes/catalog", summary='返回指数目录清单', description='`GET` 返回指数目录清单。\n\n## 查询参数\n\n- `category`（类型：`str`）：分类筛选。\n- `market`（类型：`str`）：市场筛选。\n- `publisher`（类型：`str`）：编制方筛选。\n- `status`（类型：`str`）：状态筛选。\n- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。\n- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。\n\n## 返回类型\n\n顶层返回 `list[IndexCatalogItem]`。\n\n## 返回字段\n\n- `index_code`（`str`）：指数代码。\n- `index_name`（`str`）：指数名称。\n- `category`（`str`）：分类。\n- `market`（`str`）：指数覆盖市场。\n- `publisher`（`str`）：编制方。\n- `list_date`（`str`）：上市日期。\n- `status`（`str`）：指数状态。')
async def api_index_catalog(
    category: str = Query("", description='分类。'),
    market: str = Query("", description='指数覆盖市场。'),
    publisher: str = Query("", description='编制方。'),
    status: str = Query("", description='指数状态。'),
    limit: int = Query(200, ge=1, le=5000, description='返回记录上限。'),
    offset: int = Query(0, ge=0, description='结果偏移量，从 `0` 开始。'),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, indexes.get_catalog, (category, market, publisher, status, limit, offset))


@router.get("/api/indexes/{index_code}/profile", summary='返回单个指数的基础资料', description='`GET` 返回单个指数的基础资料。\n\n## 路径参数\n\n- `index_code`（类型：`str`）：指数代码。\n\n## 返回类型\n\n顶层返回 `IndexCatalogItem`；查不到对应记录时返回空对象 `{}`。\n\n## 返回字段\n\n- `index_code`（`str`）：指数代码。\n- `index_name`（`str`）：指数名称。\n- `category`（`str`）：分类。\n- `market`（`str`）：指数覆盖市场。\n- `publisher`（`str`）：编制方。\n- `list_date`（`str`）：上市日期。\n- `status`（`str`）：指数状态。\n\n## 补充说明\n\n- 查不到对应记录时返回空对象 `{}`。')
async def api_index_profile(index_code: str) -> dict[str, object]:
    return await run_data_task(_dump_optional_item, indexes.get_profile, (index_code,))


@router.get("/api/indexes/quotes", summary='返回单个或多个指数的行情序列', description='`GET` 返回单个或多个指数的行情序列。\n\n## 查询参数\n\n- `index_code`（类型：`str`）：单个指数代码；与 `index_codes` 至少传一个。\n- `index_codes`（类型：`str`）：多个指数代码，逗号分隔；与 `index_code` 至少传一个。\n- `freq`（类型：`str`；默认：`1d`）：行情频率，可选 `1d`、`1w`、`1mo`。\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `count`（类型：`int | None`；允许空值；最小值：`1`）：每个代码返回的最近记录条数。\n- `fields`（类型：`str`）：按逗号指定返回字段，不传返回全部字段。\n- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。\n\n## 返回类型\n\n顶层返回 `list[IndexQuoteItem]`。\n\n## 返回字段\n\n- `index_code`（`str`）：指数代码。\n- `trade_time`（`str`）：时间点；日频返回交易日，分钟级返回具体时间。\n- `freq`（`str`）：数据频率。\n- `open`（`float | None`）：开盘价。\n- `high`（`float | None`）：最高价。\n- `low`（`float | None`）：最低价。\n- `close`（`float | None`）：收盘价。\n- `pre_close`（`float | None`）：前收盘价。\n- `change`（`float | None`）：涨跌额。\n- `pct_chg`（`float | None`）：涨跌幅，单位 %。\n- `volume`（`float | None`）：成交量。\n- `amount`（`float | None`）：成交额。\n\n## 补充说明\n\n- `index_code` 与 `index_codes` 至少需要传一个，`count` 按每个指数分别生效。\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。\n- 传入 `fields` 后，响应中的每条记录只保留所选字段。')
async def api_index_quotes(
    index_code: str = Query("", description='指数代码。'),
    index_codes: str = Query("", description='多个指数代码，逗号分隔；与 `index_code` 至少传一个。'),
    freq: str = Query("1d", description='数据频率。'),
    trade_date: str = Query("", description='交易日期，格式 `YYYY-MM-DD`。'),
    start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'),
    end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'),
    count: int | None = Query(None, ge=1, description='每个代码返回的最近记录条数。'),
    fields: str = Query("", description='按逗号指定返回字段，不传返回全部字段。'),
    limit: int = Query(200, ge=1, le=5000, description='返回记录上限。'),
) -> list[dict[str, object]]:
    args = (index_code, index_codes, freq, trade_date, start_date, end_date, count, limit)
    return await run_data_task(_filter_items, indexes.get_quotes, args, fields, INDEX_QUOTE_FIELDS)


@router.get("/api/indexes/{index_code}/members", summary='返回单个指数的成分列表', description='`GET` 返回单个指数的成分列表。\n\n## 路径参数\n\n- `index_code`（类型：`str`）：指数代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[IndexMemberItem]`。\n\n## 返回字段\n\n- `index_code`（`str`）：指数代码。\n- `code`（`str`）：股票代码。\n- `name`（`str`）：成分股名称。\n- `weight`（`float | None`）：权重。\n- `trade_date`（`str`）：成分权重对应的交易日。')
async def api_index_members(index_code: str, trade_date: str = Query("", description='成分权重对应的交易日。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, indexes.get_members, (index_code, trade_date))
