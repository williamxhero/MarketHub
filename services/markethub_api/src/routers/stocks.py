from __future__ import annotations

from collections.abc import Callable
import math
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from quotemux.models import DividendItem, DividendPage, RightsIssueItem, RightsIssuePage, StockQuotesQueryResult, StockStrategyFactorItem

from data_threads import QuoteClientDisconnectedError, run_data_task, run_quote_task
from routers.stock_quote_models import StockDailyWindowQueryPayload, StockDailyWindowQueryResponse, StockQuotesQueryPayload
from services import daily_window, stock_1m_delivery, stock_quotes_arrow, stocks
from services.common import filter_response_fields
from services.runtime_memory import run_with_memory_log


router = APIRouter()


def _sanitize_json_value(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _sanitize_json_value(item) for key, item in value.items()}
    return value

STOCK_QUOTE_FIELDS = {"code", "trade_time", "freq", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "volume", "amount", "adjust", "is_suspended", "is_st"}
TECHNICAL_FIELDS = {
    "code",
    "trade_date",
    "adjust",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "ema12",
    "ema26",
    "dif",
    "dea",
    "macd",
    "rsi6",
    "rsi12",
    "rsi24",
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "boll_upper",
    "boll_mid",
    "boll_lower",
}


def _dump_item_list(loader: Callable[..., list[object]], args: tuple[object, ...]) -> list[dict[str, object]]:
    return [item.model_dump() for item in loader(*args)]


def _dump_optional_item(loader: Callable[..., object], args: tuple[object, ...]) -> dict[str, object]:
    item = loader(*args)
    return item.model_dump() if item is not None else {}


def _dump_page(loader: Callable[..., object], args: tuple[object, ...]) -> dict[str, object]:
    return loader(*args).model_dump()


def _filter_items(loader: Callable[..., list[object]], args: tuple[object, ...], fields: str, allowed_fields: set[str]) -> list[dict[str, object]]:
    items = loader(*args)
    return filter_response_fields(items, fields, allowed_fields)


def _filter_quote_query_result(loader: Callable[..., object], args: tuple[object, ...], fields: str, allowed_fields: set[str]) -> dict[str, object]:
    result = loader(*args)
    payload = result.model_dump()
    if fields == "":
        return payload
    filtered_items = filter_response_fields(result.items, fields, allowed_fields)
    return {"items": filtered_items, "meta": payload["meta"]}


def _quote_request_detail(codes: str, freq: str, start_date: str, end_date: str, limit: int | None) -> dict[str, object]:
    code_count = len([item for item in codes.split(",") if item != ""])
    return {"freq": freq, "code_count": code_count, "start_date": start_date, "end_date": end_date, "limit": limit or 0}


def _filter_stock_quote_query_result(loader: Callable[..., object], args: tuple[object, ...], fields: str, allowed_fields: set[str], detail: dict[str, object]) -> dict[str, object]:
    return run_with_memory_log("stocks.quotes", detail, lambda: _filter_quote_query_result(loader, args, fields, allowed_fields))


@router.get("/api/stocks/quotes", summary='返回股票行情数据和完整性元信息，适合单股、多股批量查询、本地扫描和需要确认每只股票覆盖情况的调用场景', description='`GET` 返回股票行情数据和完整性元信息，适合单股、多股批量查询、本地扫描和需要确认每只股票覆盖情况的调用场景。\n\n## 查询参数\n\n- `code`（`str`）：单个股票代码；与 `codes` 至少传一个。\n- `codes`（`str`）：多个股票代码，逗号分隔；与 `code` 至少传一个。\n- `freq`（`str`，默认 `1d`）：行情频率，可选 `tick`、`1m`、`5m`、`15m`、`30m`、`60m`、`1d`、`1w`、`1mo`。\n- `trade_date`（`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `start_time`（`str`）：起始时间；分钟行情可传完整时间字符串。\n- `end_time`（`str`）：结束时间；分钟行情可传完整时间字符串。\n- `count`（`int | None`）：每只股票返回最近若干条记录。\n- `adjust`（`str`，默认 `none`）：复权方式。\n- `fields`（`str`）：按逗号指定 `items` 的返回字段；不影响 `meta`。\n- `limit`（`int | None`）：调用方主动裁剪总返回条数；不传则返回完整结果。\n- `skip_suspended`（`bool`，默认 `true`）：仅对 `1d/1w/1mo` 生效；强制过滤停牌行。\n- `skip_st`（`bool`，默认 `false`）：仅对 `1d/1w/1mo` 生效；如果某只股票在请求窗口内任一行 `is_st=true`，则该股票所有返回行都会被过滤。\n- `fill_missing`（`bool`，默认 `false`）：控制是否返回日线缺口补洞产生的停牌占位行；历史交易日缺口默认会进入 provider 补缺链路，只有 `fill_missing=true&skip_suspended=false` 时才返回 `is_suspended=true` 行。\n\n## 返回类型\n\n顶层返回 `StockQuotesQueryResult`。\n\n## 返回字段\n\n- `items`（`list[StockQuoteItem]`）：行情记录列表。\n- `meta`（`StockQuotesMeta`）：本次查询的完整性元信息。\n- `items.code`（`str`）：股票代码。\n- `items.trade_time`（`str`）：时间点；日频返回交易日，分钟频返回具体时间。\n- `items.freq`（`str`）：数据频率。\n- `items.open`（`float | None`）：开盘价。\n- `items.high`（`float | None`）：最高价。\n- `items.low`（`float | None`）：最低价。\n- `items.close`（`float | None`）：收盘价。\n- `items.pre_close`（`float | None`）：前收盘价。\n- `items.change`（`float | None`）：涨跌额。\n- `items.pct_chg`（`float | None`）：涨跌幅，单位 `%`。\n- `items.volume`（`float | None`）：成交量。\n- `items.amount`（`float | None`）：成交额。\n- `items.adjust`（`str`）：复权方式。\n- `items.is_suspended`（`bool`）：该行是否停牌。\n- `items.is_st`（`bool`）：该行是否 ST。\n- `meta.total_rows`（`int`）：过滤后可返回集合的总行数。\n- `meta.returned_rows`（`int`）：实际返回行数。\n- `meta.complete`（`bool`）：整体结果是否完整。\n- `meta.truncated`（`bool`）：是否被 `limit` 裁剪。\n- `meta.codes`（`list[StockQuoteCodeSummary]`）：每只股票的完整性统计。\n- `meta.codes.code`（`str`）：股票代码。\n- `meta.codes.row_count`（`int`）：该股票实际返回行数。\n- `meta.codes.expected_bar_count`（`int`）：预期 bar 数。\n- `meta.codes.actual_bar_count`（`int`）：实际覆盖 bar 数。\n- `meta.codes.first_trade_time`（`str`）：首条返回时间。\n- `meta.codes.last_trade_time`（`str`）：末条返回时间。\n- `meta.codes.complete`（`bool`）：该股票是否完整。\n- `meta.codes.truncated`（`bool`）：该股票是否被裁剪。\n- `meta.codes.missing_trade_dates`（`list[str]`）：缺失交易日。\n- `meta.codes.missing_trade_times`（`list[str]`）：缺失 bar 时间。\n\n## 补充说明\n\n- `fields` 只裁剪 `items`，不裁剪 `meta`。\n- `freq=1d/1w/1mo` 先读本地 `fact.stock_daily_1d`；历史交易日缺口默认进入 `stocks.quotes.daily` provider 补缺链路。\n- 当前交易日或未来日期不会同步补缺，只返回本地结果和完整性 `meta`。\n- Runtime Profile 会按 Capability Matrix 勾选的源补齐本地缺口。\n- `fact.stock_daily_1d` 已纳入 `BJSE` 正式日线口径，所以 `1d` 日线查询会正常返回北交所股票。\n- 如果 provider 补缺后仍缺少历史交易日，且该股票能找到前一个交易日，系统会用前一交易日收盘价写入一条 `is_suspended=true` 的停牌占位日线。\n- 已写入 `fact.stock_daily_1d` 的停牌占位日线会参与完整性覆盖计算，因此不会再计入 `missing_trade_dates`。\n- `fill_missing=false` 默认不返回停牌占位行；`skip_suspended=true` 会强制过滤所有停牌行。\n- `skip_suspended` 只过滤停牌行，不会过滤 ST 股票。\n- `skip_st=true` 会按请求窗口整只过滤 ST 股票；被过滤掉的股票仍会在 `meta.codes` 中保留一条 summary，但 `row_count=0`、`complete=false`。\n- `1w`、`1mo` 在日线结果上聚合，并沿用同一套 `fill_missing`、`skip_suspended`、`skip_st` 规则。\n- 分钟线不应用停牌补洞和 ST 整只过滤规则。')
async def api_stock_quotes(
    code: str = Query("", description='单个股票代码；与 `codes` 至少传一个。'),
    codes: str = Query("", description='多个股票代码，逗号分隔；与 `code` 至少传一个。'),
    freq: str = Query("1d", description='行情频率，可选 `tick`、`1m`、`5m`、`15m`、`30m`、`60m`、`1d`、`1w`、`1mo`。'),
    trade_date: str = Query("", description='交易日期，格式 `YYYY-MM-DD`。'),
    start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'),
    end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'),
    start_time: str = Query("", description='起始时间；分钟行情可传完整时间字符串。'),
    end_time: str = Query("", description='结束时间；分钟行情可传完整时间字符串。'),
    count: int | None = Query(None, ge=1, description='每只股票返回最近若干条记录。'),
    adjust: str = Query("none", description='复权方式。'),
    fields: str = Query("", description='按逗号指定 `items` 的返回字段；不影响 `meta`。'),
    limit: int | None = Query(None, ge=1, description='调用方主动裁剪总返回条数；不传则返回完整结果。'),
    skip_suspended: bool = Query(True, description='仅对 `1d/1w/1mo` 生效；强制过滤停牌行。'),
    skip_st: bool = Query(False, description='仅对 `1d/1w/1mo` 生效；如果某只股票在请求窗口内任一行 `is_st=true`，则该股票所有返回行都会被过滤。'),
    fill_missing: bool = Query(False, description='控制是否返回源数据已明确标记的停牌行；服务不会生成前收盘价占位 OHLCV。'),
    meta_detail: Literal["summary", "full"] = Query("summary", description="summary 只返回缺失数量；full 额外展开 missing_trade_times。"),
    data_version: str = Query(..., min_length=1, description="/api/health 返回的市场数据版本；版本不匹配会拒绝读取。"),
) -> dict[str, object]:
    actual_codes = codes if codes != "" else code
    detail = _quote_request_detail(actual_codes, freq, start_date, end_date, limit)
    args = (code, codes, freq, trade_date, start_date, end_date, start_time, end_time, count, adjust, limit, skip_suspended, skip_st, fill_missing, meta_detail, data_version)
    is_heavy = int(detail["code_count"]) > 5 or int(detail["limit"]) > 2000
    runner = run_quote_task if is_heavy else run_data_task
    return await runner(_filter_stock_quote_query_result, stocks.get_quotes_query_result, args, fields, STOCK_QUOTE_FIELDS, detail)


@router.post(
    "/api/stocks/quotes/query",
    summary="批量查询股票行情和完整性元数据",
    description=(
        "大批量股票行情的标准入口，推荐每批 100 至 200 只；少量代码和交互调试可使用 GET /api/stocks/quotes。"
        "请求体可指定 adjust、trade_date 或时间范围。data_version 只能放在 JSON 请求体，值必须取自 /api/health；limit 是整个响应的硬裁剪上限，不是分页大小；"
        "发生裁剪时 meta.truncated=true，meta.total_rows 和 meta.returned_rows 分别给出裁剪前后行数。"
        "meta_detail=summary 默认只返回 missing_count，full 才展开 missing_trade_times。"
        "覆盖率仅计算证券上市至退市区间内的交易日；若 meta.codes.expected_bar_count=0 且 complete=true，表示该日期无可交易行，策略适配器不得要求 OHLCV。"
        "is_suspended=true 的行不是可交易行，策略适配器必须排除；服务不会用零值或前收盘价合成 OHLCV。"
        "返回合同固定为 StockQuotesQueryResult，GET 和 POST 对相同查询条件返回相同 items 与 meta。"
    ),
    response_model=StockQuotesQueryResult,
    responses={
        200: {
            "content": {
                "application/json": {},
                "application/vnd.apache.arrow.stream": {"schema": {"type": "string", "format": "binary"}},
            }
        },
        406: {"description": "Accept 不支持"},
    },
)
async def api_stock_quotes_query(payload: StockQuotesQueryPayload, request: Request) -> dict[str, object] | Response:
    accept = request.headers.get("accept", "").lower()
    wants_arrow = stock_quotes_arrow.ARROW_MEDIA_TYPE in accept
    accepts_json = not accept or "*/*" in accept or "application/json" in accept
    if not wants_arrow and not accepts_json:
        raise HTTPException(
            status_code=406,
            detail={"code": "STOCK_QUOTES_MEDIA_TYPE_NOT_ACCEPTABLE", "message": "仅支持 application/json 或 Arrow IPC stream"},
        )
    if payload.freq == "1m" and payload.count is None:
        try:
            if wants_arrow:
                prepared = await run_quote_task(
                    stock_1m_delivery.prepare_arrow,
                    payload,
                    is_disconnected=request.is_disconnected,
                )
                return StreamingResponse(prepared.body, media_type=stock_1m_delivery.ARROW_MEDIA_TYPE, headers=prepared.headers)
            return await run_quote_task(
                stock_1m_delivery.build_json,
                payload,
                is_disconnected=request.is_disconnected,
            )
        except QuoteClientDisconnectedError as exc:
            raise HTTPException(status_code=499, detail={"code": "CLIENT_DISCONNECTED", "message": str(exc)}) from exc
    codes = ",".join(payload.codes)
    detail = _quote_request_detail(codes, payload.freq, payload.start_date, payload.end_date, payload.limit)
    args = (
        "", codes, payload.freq, payload.trade_date, payload.start_date, payload.end_date,
        payload.start_time, payload.end_time, payload.count, payload.adjust, payload.limit,
        payload.skip_suspended, payload.skip_st, payload.fill_missing, payload.meta_detail, payload.data_version,
    )
    try:
        result = await run_quote_task(
            _filter_stock_quote_query_result,
            stocks.get_quotes_query_result,
            args,
            "",
            STOCK_QUOTE_FIELDS,
            detail,
            is_disconnected=request.is_disconnected,
        )
        if wants_arrow:
            encoded = stock_quotes_arrow.encode(result)
            return Response(content=encoded.content, media_type=stock_quotes_arrow.ARROW_MEDIA_TYPE, headers=encoded.headers)
        return result
    except QuoteClientDisconnectedError as exc:
        raise HTTPException(status_code=499, detail={"code": "CLIENT_DISCONNECTED", "message": str(exc)}) from exc


@router.post(
    "/api/stocks/quotes/daily-window/query",
    summary="只读、版本冻结、可恢复分页的批量日线窗口",
    description=(
        "ApexTrade 等远程只读调用方的正式日线批量入口。freq 固定为 1d；支持精确 codes 集合或 all_a universe。"
        "服务只读本地 fact/ref 表，不触发 provider、capture、回填或缓存写入。"
        "请求必须携带 /api/health 的 data_version；版本漂移、coverage 缺失、重复行或 cursor 不匹配均 fail-closed。"
        "items 固定按 trade_time ASC, code ASC；分页通过 next_cursor 继续，不将 page_size 视为截断。"
    ),
    response_model=StockDailyWindowQueryResponse,
    responses={
        200: {
            "content": {
                "application/json": {},
                "application/vnd.apache.arrow.stream": {"schema": {"type": "string", "format": "binary"}},
            }
        },
        406: {"description": "Accept 不支持"},
    },
)
async def api_stock_daily_window_query(payload: StockDailyWindowQueryPayload, request: Request) -> Response:
    accept = request.headers.get("accept", "").lower()
    wants_arrow = daily_window.ARROW_MEDIA_TYPE in accept
    accepts_json = not accept or "*/*" in accept or "application/json" in accept
    if not wants_arrow and not accepts_json:
        raise HTTPException(
            status_code=406,
            detail={"code": "DAILY_WINDOW_MEDIA_TYPE_NOT_ACCEPTABLE", "message": "仅支持 application/json 或 Arrow IPC stream"},
        )
    try:
        if wants_arrow:
            prepared = await run_quote_task(
                daily_window.prepare_arrow_response,
                payload,
                is_disconnected=request.is_disconnected,
            )
            return StreamingResponse(prepared.body, media_type=daily_window.ARROW_MEDIA_TYPE, headers=prepared.headers)
        encoded = await run_quote_task(
            daily_window.build_response,
            payload,
            "gzip" in request.headers.get("accept-encoding", "").lower(),
            is_disconnected=request.is_disconnected,
        )
    except QuoteClientDisconnectedError as exc:
        raise HTTPException(status_code=499, detail={"code": "CLIENT_DISCONNECTED", "message": str(exc)}) from exc
    response_etag = encoded.headers.get("ETag", "")
    if response_etag and request.headers.get("if-none-match", "") == response_etag:
        return Response(status_code=304, headers=encoded.headers)
    return Response(content=encoded.content, media_type="application/json", headers=encoded.headers)


@router.get("/api/stocks/quotes/daily-snapshot", summary='返回指定交易日的全市场股票日线快照', description='`GET` 返回指定交易日的全市场股票日线快照。\n\n## 查询参数\n\n- `trade_date`（`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `fields`（`str`）：按逗号指定返回字段。\n- `limit`（`int`）：返回记录上限。\n- `offset`（`int`）：结果偏移量。\n- `skip_suspended`（`bool`）：过滤停牌行。\n- `skip_st`（`bool`）：过滤 ST 股票。\n\n## 返回类型\n\n顶层返回 `list[StockQuoteItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_time`（`str`）：交易日期。\n- `freq`（`str`）：固定返回 `1d`。\n- `open`（`float | None`）：开盘价。\n- `high`（`float | None`）：最高价。\n- `low`（`float | None`）：最低价。\n- `close`（`float | None`）：收盘价。\n- `pre_close`（`float | None`）：前收盘价。\n- `change`（`float | None`）：涨跌额。\n- `pct_chg`（`float | None`）：涨跌幅。\n- `volume`（`float | None`）：成交量。\n- `amount`（`float | None`）：成交额。\n- `adjust`（`str`）：固定返回 `none`。\n- `is_suspended`（`bool`）：是否停牌。\n- `is_st`（`bool`）：是否 ST。\n\n## 补充说明\n\n- 该接口直接读取本地 `fact.stock_daily_1d`，返回口径与 `/api/stocks/quotes?freq=1d&start_date=...&end_date=...` 的日线事实表保持一致。\n- 缺少指定交易日数据时快速返回空数组，不在请求线程内触发全市场外源补齐。\n- `pre_close`、`change`、`pct_chg` 会基于前一个已有交易日收盘价派生。')
async def api_stock_daily_snapshot(
    trade_date: str = Query(..., description='交易日期，格式 `YYYY-MM-DD`。'),
    fields: str = Query("", description='按逗号指定返回字段。'),
    limit: int = Query(10000, ge=1, le=10000, description='返回记录上限。'),
    offset: int = Query(0, ge=0, description='结果偏移量。'),
    skip_suspended: bool = Query(True, description='过滤停牌行。'),
    skip_st: bool = Query(False, description='过滤 ST 股票。'),
    data_version: str = Query("", description="兼容字段：全局市场数据版本。"),
    dataset_version: str = Query("", description="推荐字段：stock_daily_1d 数据集版本。"),
) -> list[dict[str, object]]:
    args = (trade_date, limit, offset, skip_suspended, skip_st, data_version, dataset_version)
    return await run_quote_task(_filter_items, stocks.get_market_daily_snapshot, args, fields, STOCK_QUOTE_FIELDS)


@router.get("/api/stocks/quotes/daily-local-window", summary='返回指定日期区间内的全市场股票日线', description='`GET` 返回指定日期区间内的全市场股票日线。\n\n## 查询参数\n\n- `start_date`（`str`）：起始交易日期，格式 `YYYY-MM-DD`。\n- `end_date`（`str`）：结束交易日期，格式 `YYYY-MM-DD`。\n- `fields`（`str`）：按逗号指定返回字段。\n- `limit`（`int`）：返回记录上限。\n- `offset`（`int`）：结果偏移量。\n- `skip_suspended`（`bool`）：过滤停牌行。\n- `skip_st`（`bool`）：如果某只股票在请求窗口内任一行 `is_st=true`，则整只股票过滤。\n\n## 返回类型\n\n顶层返回 `list[StockQuoteItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_time`（`str`）：交易日期。\n- `freq`（`str`）：固定返回 `1d`。\n- `open`（`float | None`）：开盘价。\n- `high`（`float | None`）：最高价。\n- `low`（`float | None`）：最低价。\n- `close`（`float | None`）：收盘价。\n- `pre_close`（`float | None`）：前收盘价。\n- `change`（`float | None`）：涨跌额。\n- `pct_chg`（`float | None`）：涨跌幅。\n- `volume`（`float | None`）：成交量。\n- `amount`（`float | None`）：成交额。\n- `adjust`（`str`）：固定返回 `none`。\n- `is_suspended`（`bool`）：是否停牌。\n- `is_st`（`bool`）：是否 ST。\n\n## 补充说明\n\n- 不需要传 `code` 或 `codes`。\n- 该接口直接读取本地 `fact.stock_daily_1d`，不复用 `/api/stocks/quotes` 的逐股票缺口补源链路。\n- 先按 `skip_st` 整只过滤 ST 股票，再按 `skip_suspended` 过滤停牌行。\n- 分页在过滤后生效，排序固定为 `trade_time, code`。\n- 如果本地日线事实表缺数据，应先修复日线表刷新。')
async def api_stock_daily_local_window(
    start_date: str = Query(..., description='起始交易日期，格式 `YYYY-MM-DD`。'),
    end_date: str = Query(..., description='结束交易日期，格式 `YYYY-MM-DD`。'),
    fields: str = Query("", description='按逗号指定返回字段。'),
    limit: int = Query(50000, ge=1, description='返回记录上限。'),
    offset: int = Query(0, ge=0, description='结果偏移量。'),
    skip_suspended: bool = Query(True, description='过滤停牌行。'),
    skip_st: bool = Query(False, description='如果某只股票在请求窗口内任一行 `is_st=true`，则整只股票过滤。'),
    data_version: str = Query("", description="兼容字段：全局市场数据版本。"),
    dataset_version: str = Query("", description="推荐字段：stock_daily_1d 数据集版本。"),
) -> list[dict[str, object]]:
    args = (start_date, end_date, limit, offset, skip_suspended, skip_st, data_version, dataset_version)
    return await run_quote_task(_filter_items, stocks.get_market_daily_local_window, args, fields, STOCK_QUOTE_FIELDS)


@router.get("/api/stocks/catalog", summary='返回股票基础清单', description='`GET` 返回股票基础清单。\n\n## 查询参数\n\n- `codes`（类型：`str`）：多个股票代码，逗号分隔。\n- `name`（类型：`str`）：股票简称关键字。\n- `market`（类型：`str`）：兼容参数，当前实现保留该入参但不参与筛选。\n- `exchange`（类型：`str`）：交易所标识。\n- `list_status`（类型：`str`）：上市状态筛选。\n- `is_hs`（类型：`str`）：兼容参数，当前实现保留该入参但不参与筛选。\n- `include_delisted`（类型：`bool`；默认：`false`）：是否包含已退市标的。\n- `limit`（类型：`int`；默认：`5000`；范围：`1-5000`）：返回记录上限。\n- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。\n\n## 返回类型\n\n顶层返回 `list[StockBasicInfo]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `name`（`str`）：名称。\n- `exchange`（`str`）：交易所。\n- `market`（`str`）：所属市场板块，如主板、创业板或北交所等口径值。\n- `list_status`（`str`）：上市状态。\n- `list_date`（`str`）：上市日期。\n- `delist_date`（`str`）：退市日期；未退市时为空字符串。\n- `industry`（`str`）：所属行业。\n- `area`（`str`）：所属地域。\n\n## 补充说明\n\n- `market` 和 `is_hs` 当前只是兼容入参，不参与实际筛选。')
async def api_stock_catalog(
    request: Request,
    codes: str = Query("", description='多个股票代码，逗号分隔。'),
    name: str = Query("", description='名称。'),
    market: str = Query("", description='所属市场板块，如主板、创业板或北交所等口径值。'),
    exchange: str = Query("", description='交易所。'),
    list_status: str = Query("", description='上市状态。'),
    is_hs: str = Query("", description='兼容参数，当前实现保留该入参但不参与筛选。'),
    include_delisted: bool = Query(False, description='是否包含已退市标的。'),
    limit: int = Query(5000, ge=1, le=5000, description='返回记录上限。'),
    offset: int = Query(0, ge=0, description='结果偏移量，从 `0` 开始。'),
    data_version: str = Query(..., min_length=1, description="/api/health 返回的市场数据版本"),
) -> list[dict[str, object]]:
    del market
    del is_hs
    args = (codes, name, exchange, list_status, include_delisted, limit, offset, data_version)
    encoded = await run_data_task(lambda loader, loader_args: loader(*loader_args), stocks.get_catalog_encoded, args)
    if request.headers.get("if-none-match", "") == encoded.headers["ETag"]:
        return Response(status_code=304, headers=encoded.headers)  # type: ignore[return-value]
    return Response(content=encoded.content, media_type="application/json", headers=encoded.headers)  # type: ignore[return-value]


@router.get("/api/stocks/catalog/archive", summary='返回指定交易日的股票归档清单', description='`GET` 返回指定交易日的股票归档清单。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：归档交易日，格式 `YYYY-MM-DD`。\n- `code`（类型：`str`）：股票代码。\n- `name`（类型：`str`）：股票简称关键字。\n- `industry`（类型：`str`）：所属行业筛选。\n- `area`（类型：`str`）：所属地域筛选。\n- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。\n- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。\n\n## 返回类型\n\n顶层返回 `list[StockArchiveItem]`。\n\n## 返回字段\n\n- `trade_date`（`str`）：交易日期。\n- `code`（`str`）：股票代码。\n- `name`（`str`）：名称。\n- `exchange`（`str`）：归档时点对应的交易所。\n- `market`（`str`）：归档时点对应的所属市场板块。\n- `list_status`（`str`）：归档时点对应的上市状态。\n- `industry`（`str`）：所属行业。\n- `area`（`str`）：所属地域。')
async def api_stock_catalog_archive(
    trade_date: str = Query("", description='交易日期。'),
    code: str = Query("", description='股票代码。'),
    name: str = Query("", description='名称。'),
    industry: str = Query("", description='所属行业。'),
    area: str = Query("", description='所属地域。'),
    limit: int = Query(200, ge=1, le=5000, description='返回记录上限。'),
    offset: int = Query(0, ge=0, description='结果偏移量，从 `0` 开始。'),
) -> list[dict[str, object]]:
    args = (trade_date, code, name, industry, area, limit, offset)
    return await run_data_task(_dump_item_list, stocks.get_archive, args)


@router.get("/api/stocks/{code}/profile/basic", summary='返回单只股票的基础资料', description='`GET` 返回单只股票的基础资料。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 返回类型\n\n顶层返回 `StockBasicInfo`；查不到对应记录时返回空对象 `{}`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `name`（`str`）：名称。\n- `exchange`（`str`）：交易所。\n- `market`（`str`）：所属市场板块，如主板、创业板或北交所等口径值。\n- `list_status`（`str`）：上市状态。\n- `list_date`（`str`）：上市日期。\n- `delist_date`（`str`）：退市日期；未退市时为空字符串。\n- `industry`（`str`）：所属行业。\n- `area`（`str`）：所属地域。\n\n## 补充说明\n\n- 查不到对应记录时返回空对象 `{}`。')
async def api_stock_profile_basic(
    code: str,
    data_version: str = Query("", description="兼容字段：全局市场数据版本。"),
    dataset_version: str = Query("", description="推荐字段：stock_reference 数据集版本。"),
) -> dict[str, object]:
    return await run_data_task(_dump_optional_item, stocks.get_basic, (code, data_version, dataset_version))


@router.get("/api/stocks/{code}/profile", summary='返回单只股票的公司概况', description='`GET` 返回单只股票的公司概况。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 返回类型\n\n顶层返回 `StockProfileItem`；查不到对应记录时返回空对象 `{}`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `company_name`（`str`）：公司简称或工商登记简称。\n- `full_name`（`str`）：公司全称。\n- `chairman`（`str`）：董事长。\n- `manager`（`str`）：总经理或经营负责人。\n- `website`（`str`）：公司网站。\n- `employee_count`（`int | None`）：员工人数。\n- `main_business`（`str`）：主营业务描述。\n- `office`（`str`）：办公地址。\n\n## 补充说明\n\n- 查不到对应记录时返回空对象 `{}`。')
async def api_stock_profile(
    code: str,
    data_version: str = Query("", description="兼容字段：全局市场数据版本。"),
    dataset_version: str = Query("", description="推荐字段：stock_reference 数据集版本。"),
) -> dict[str, object]:
    return await run_data_task(_dump_optional_item, stocks.get_profile, (code, data_version, dataset_version))


@router.get("/api/stocks/{code}/profile/name-history", summary='返回单只股票的名称变更记录', description='`GET` 返回单只股票的名称变更记录。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[NameHistoryItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `name`（`str`）：名称。\n- `start_date`（`str`）：start日期。\n- `end_date`（`str`）：END日期。\n- `ann_date`（`str`）：公告日期。')
async def api_stock_name_history(code: str, start_date: str = Query("", description='start日期。'), end_date: str = Query("", description='END日期。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_name_history, (code, start_date, end_date))


@router.get("/api/stocks/{code}/profile/managers", summary='返回单只股票的管理层名单', description='`GET` 返回单只股票的管理层名单。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 返回类型\n\n顶层返回 `list[StockManagerItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `name`（`str`）：名称。\n- `title`（`str`）：职务。\n- `gender`（`str`）：性别。\n- `education`（`str`）：学历。\n- `begin_date`（`str`）：开始日期。\n- `end_date`（`str`）：END日期。')
async def api_stock_managers(code: str) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_managers, (code,))


@router.get("/api/stocks/{code}/profile/management-rewards", summary='返回单只股票的高管薪酬记录', description='`GET` 返回单只股票的高管薪酬记录。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[ManagementRewardItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `ann_date`（`str`）：公告日期。\n- `name`（`str`）：名称。\n- `title`（`str`）：职务。\n- `reward_amount`（`float | None`）：薪酬金额。\n- `hold_amount`（`float | None`）：持股数量。')
async def api_stock_management_rewards(code: str, start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_management_rewards, (code, start_date, end_date))


@router.get("/api/stocks/{code}/signals/hl", summary='返回单只股票的新高新低信号', description='`GET` 返回单只股票的新高新低信号。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[HLSignalItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `first_extreme`（`str`）：首次触发的新高或新低类型。\n- `high_time`（`str`）：触发新高的时间。\n- `low_time`（`str`）：触发新低的时间。\n- `signal`（`str`）：新高新低信号类型。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_hl_signal(code: str, trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_hl_signal, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/signals/limit-order-amount")
async def api_stock_limit_order_amount(trade_date: str = Query(...)) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_limit_order_amount, (trade_date,))


@router.get("/api/stocks/{code}/signals/nine-turn", summary='返回单只股票的神奇九转信号', description='`GET` 返回单只股票的神奇九转信号。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `freq`（类型：`str`；默认：`daily`）：神奇九转计算周期。\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[NineTurnItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_time`（`str`）：时间点；日频返回交易日，分钟级返回具体时间。\n- `freq`（`str`）：数据频率。\n- `setup_index`（`int | None`）：九转 setup 序号。\n- `countdown_index`（`int | None`）：九转 countdown 序号。\n- `signal`（`str`）：九转信号类型。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_nine_turn(code: str, freq: str = Query("daily", description='数据频率。'), trade_date: str = Query("", description='交易日期，格式 `YYYY-MM-DD`。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_nine_turn, (code, freq, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/factors/adj", summary='返回单只股票的复权因子序列', description='`GET` 返回单只股票的复权因子序列。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `base_date`（类型：`str`）：参数说明见接口上下文。\n\n## 返回类型\n\n顶层返回 `list[AdjFactorItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `adj_factor`（`float | None`）：复权因子。')
async def api_stock_adj_factors(code: str, start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'), base_date: str = Query("", description='参数说明见接口上下文。')) -> list[dict[str, object]]:
    return await run_quote_task(_dump_item_list, stocks.get_adj_factors, (code, start_date, end_date, base_date))


@router.get("/api/stocks/{code}/factors/technical", summary='返回单只股票的技术指标序列', description='`GET` 返回单只股票的技术指标序列。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `adjust`（类型：`str`；默认：`none`）：复权方式。\n- `fields`（类型：`str`）：按逗号指定返回字段，不传返回全部字段。\n\n## 返回类型\n\n顶层返回 `list[TechnicalFactorItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `adjust`（`str`）：复权方式。\n- `ma5`（`float | None`）：5 日均线。\n- `ma10`（`float | None`）：10 日均线。\n- `ma20`（`float | None`）：20 日均线。\n- `ma60`（`float | None`）：60 日均线。\n- `ema12`（`float | None`）：12 日 EMA。\n- `ema26`（`float | None`）：26 日 EMA。\n- `dif`（`float | None`）：MACD 的 DIF 值。\n- `dea`（`float | None`）：MACD 的 DEA 值。\n- `macd`（`float | None`）：MACD 柱值。\n- `rsi6`（`float | None`）：6 日 RSI。\n- `rsi12`（`float | None`）：12 日 RSI。\n- `rsi24`（`float | None`）：24 日 RSI。\n- `kdj_k`（`float | None`）：KDJ 的 K 值。\n- `kdj_d`（`float | None`）：KDJ 的 D 值。\n- `kdj_j`（`float | None`）：KDJ 的 J 值。\n- `boll_upper`（`float | None`）：布林带上轨。\n- `boll_mid`（`float | None`）：布林带中轨。\n- `boll_lower`（`float | None`）：布林带下轨。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。\n- 传入 `fields` 后，响应中的每条记录只保留所选字段。')
async def api_stock_technical_factors(
    code: str,
    trade_date: str = Query("", description='交易日期。'),
    start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'),
    end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'),
    adjust: str = Query("none", description='复权方式。'),
    fields: str = Query("", description='按逗号指定返回字段，不传返回全部字段。'),
) -> list[dict[str, object]]:
    args = (code, trade_date, start_date, end_date, adjust)
    return await run_data_task(_filter_items, stocks.get_technical_factors, args, fields, TECHNICAL_FIELDS)


@router.get(
    "/api/stocks/factors/strategy-window",
    summary="返回全市场点时策略因子窗口",
    description="""`GET` 从本地事实表批量返回全 A 股策略因子窗口。

## 查询参数

- `start_date`（`str`）：起始交易日期，格式 `YYYY-MM-DD`。
- `end_date`（`str`）：结束交易日期，格式 `YYYY-MM-DD`。
- `data_version`（`str`）：`/api/health` 返回的市场数据版本；版本不匹配时拒绝读取。

## 返回类型

顶层直接返回 `list[StockStrategyFactorItem]`，固定按 `trade_date, code` 排序。

## 返回字段

- `trade_date`、`code`：交易日与股票代码。
- `listing_board`：上市板块。
- `close`：未复权收盘价。
- `dividend_yield_pct`：Tushare `dv_ratio`，单位 `%`。
- `float_market_cap`：Tushare `circ_mv`，单位万元。
- `circulating_shares`：Tushare `float_share`，单位万股。
- `free_float_shares`：Tushare `free_share`，单位万股。
- `active_buy_amount`：Tushare 四档主动买入金额之和，单位元。
- `active_buy_amount_proportion_all`：`active_buy_amount / (float_market_cap * 10000)`；市值缺失或为零时返回 `null`。
- `volume_shares`：成交量，统一单位为股。
- `mean_volume_5d_shares`：最近 5 个交易日平均成交量，单位股。
- `mean_volume_5d_to_free_float_shares`：`mean_volume_5d_shares / (free_float_shares * 10000)`；自由流通股本缺失或为零时返回 `null`。
- `ma10`、`ma20`、`ma40`：基于本地未复权日收盘价计算的移动平均。
- `is_st`、`is_suspended`：该交易日的风险与停牌状态。
- `upper_limit`、`lower_limit`、`price_band_state`：历史涨跌停边界及收盘价所处状态。

## 数据流

仅使用 `derived_core` source package；该 source 一次 SQL 读取本地 `fact` / `ref` 表，不在请求线程逐股票访问外源，也不做隐式 provider fallback。缺失值保持 `null`，缺失字符串保持空字符串。""",
)
async def api_stock_strategy_factor_window(
    start_date: str = Query(..., description="起始交易日期，格式 YYYY-MM-DD。"),
    end_date: str = Query(..., description="结束交易日期，格式 YYYY-MM-DD。"),
    codes: str = Query("", description="可选股票代码，逗号分隔；不传则返回全市场。"),
    data_version: str = Query(..., min_length=1, description="/api/health 返回的市场数据版本；版本不匹配会拒绝读取。"),
) -> list[StockStrategyFactorItem]:
    result = await run_data_task(stocks.get_strategy_factor_window, start_date, end_date, codes, data_version)
    return _sanitize_json_value(result)


@router.get("/api/stocks/{code}/indicators/money-flow", summary='返回单只股票的资金流指标', description='`GET` 返回单只股票的资金流指标。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `view`（类型：`str`；默认：`main`）：资金流视图，当前固定使用 `main`。\n\n## 返回类型\n\n顶层返回 `list[StockMoneyFlowItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `view`（`str`）：返回视图标识。\n- `main_inflow`（`float | None`）：主力流入金额。\n- `main_outflow`（`float | None`）：主力流出金额。\n- `net_inflow`（`float | None`）：净流入金额。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_money_flow(code: str, trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'), view: str = Query("main", description='返回视图标识。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_money_flow, (code, trade_date, start_date, end_date, view))




@router.get("/api/stocks/indicators/money-flow/batch", summary='返回多只股票在指定交易日的资金流指标', description='`GET` 返回多只股票在指定交易日的资金流指标。\n\n## 查询参数\n\n- `codes`（`str`）：股票代码列表，多个代码用逗号分隔。\n- `trade_date`（`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `view`（`str`，默认 `main`）：资金流视图，当前固定使用 `main`。\n\n## 返回类型\n\n顶层返回 `list[StockMoneyFlowItem]`。\n\n## Capability\n\n- `stocks.indicators.money_flow`\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `view`（`str`）：返回视图标识。\n- `main_inflow`（`float | None`）：主力流入金额。\n- `main_outflow`（`float | None`）：主力流出金额。\n- `net_inflow`（`float | None`）：净流入金额。\n\n## 补充说明\n\n- 该接口直接从本地 `fact.stock_daily_1d` 批量读取资金流字段，返回结构与单票资金流接口一致。\n- 当日线存在但资金流明细字段缺失时，`net_inflow` 返回 `0.0`，避免调用方把已有行情覆盖误判成接口断链。\n- 缺少指定交易日行情数据时快速返回空数组，不按股票代码串行触发外源补齐。')
async def api_stock_money_flow_batch(
    codes: str = Query(..., description='股票代码列表，多个代码用逗号分隔。'),
    trade_date: str = Query(..., description='交易日期。'),
    view: str = Query("main", description='返回视图标识。'),
) -> list[dict[str, object]]:
    """批量查询多只股票的资金流数据"""
    return await run_data_task(_dump_item_list, stocks.get_money_flow_batch, (codes, trade_date, view))


@router.get("/api/stocks/indicators/money-flow/snapshot")
async def api_stock_money_flow_snapshot(
    trade_date: str = Query(..., description="交易日期。"),
    view: str = Query("main", description="返回视图标识。"),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_money_flow_snapshot, (trade_date, view))


@router.get("/api/stocks/indicators/margin/snapshot")
async def api_stock_margin_snapshot(trade_date: str = Query(..., description="交易日期。")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_margin_snapshot, (trade_date,))


@router.get("/api/stocks/finance/express/snapshot")
async def api_stock_express_snapshot(announce_date: str = Query(..., description="公告日期。")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_express_snapshot, (announce_date,))


@router.get("/api/stocks/finance/forecasts/snapshot")
async def api_stock_forecast_snapshot(announce_date: str = Query(..., description="公告日期。")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_forecast_snapshot, (announce_date,))

@router.get("/api/stocks/indicators/ah-comparisons", summary='返回 AH 股比价数据', description='`GET` 返回 AH 股比价数据。\n\n## 查询参数\n\n- `code`（类型：`str`）：股票代码。\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。\n- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。\n\n## 返回类型\n\n顶层返回 `list[StockAHComparisonItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `name`（`str`）：A 股简称。\n- `h_code`（`str`）：对应 H 股代码。\n- `trade_date`（`str`）：交易日期。\n- `a_close`（`float | None`）：A 股收盘价。\n- `h_close`（`float | None`）：H 股收盘价。\n- `premium_ratio`（`float | None`）：A/H 溢价率，单位 %。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_ah_comparisons(
    code: str = Query("", description='股票代码。'),
    trade_date: str = Query("", description='交易日期。'),
    start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'),
    end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。'),
    limit: int = Query(200, ge=1, le=5000, description='返回记录上限。'),
    offset: int = Query(0, ge=0, description='结果偏移量，从 `0` 开始。'),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_ah_comparisons, (code, trade_date, start_date, end_date, limit, offset))


@router.get("/api/stocks/indicators/daily-basic")
async def api_stock_daily_basic(code: str = Query(""), codes: str = Query(""), trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_daily_basic, (code, codes, trade_date, start_date, end_date))


@router.get("/api/stocks/indicators/daily-valuation")
async def api_stock_daily_valuation(code: str = Query(""), codes: str = Query(""), trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_daily_valuation, (code, codes, trade_date, start_date, end_date))


@router.get("/api/stocks/indicators/daily-market-value")
async def api_stock_daily_market_value(code: str = Query(""), codes: str = Query(""), trade_date: str = Query(""), start_date: str = Query(""), end_date: str = Query("")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_daily_market_value, (code, codes, trade_date, start_date, end_date))


@router.get("/api/stocks/indicators/risk-flags", summary='返回股票风险标识记录', description='`GET` 返回股票风险标识记录。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `flag_type`（类型：`str`）：风险标识类型筛选。\n- `status`（类型：`str`）：状态筛选。\n- `limit`（类型：`int`；默认：`200`；范围：`1-5000`）：返回记录上限。\n- `offset`（类型：`int`；默认：`0`；最小值：`0`）：结果偏移量，从 `0` 开始。\n\n## 返回类型\n\n顶层返回 `list[StockRiskFlagItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `name`（`str`）：名称。\n- `flag_type`（`str`）：风险标识类型。\n- `start_date`（`str`）：start日期。\n- `end_date`（`str`）：END日期。\n- `status`（`str`）：风险标识状态。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_risk_flags(
    trade_date: str = Query("", description='交易日期，格式 `YYYY-MM-DD`。'),
    start_date: str = Query("", description='start日期。'),
    end_date: str = Query("", description='END日期。'),
    flag_type: str = Query("", description='风险标识类型。'),
    status: str = Query("", description='风险标识状态。'),
    limit: int = Query(200, ge=1, le=5000, description='返回记录上限。'),
    offset: int = Query(0, ge=0, description='结果偏移量，从 `0` 开始。'),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_risk_flags, (trade_date, start_date, end_date, flag_type, status, limit, offset))


@router.get("/api/stocks/{code}/indicators/premarket", summary='返回单只股票的盘前指标数据', description='`GET` 返回单只股票的盘前指标数据。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[StockPremarketItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `total_share`（`float | None`）：总股本。\n- `float_share`（`float | None`）：流通股本。\n- `limit_up`（`float | None`）：涨停价。\n- `limit_down`（`float | None`）：跌停价。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_premarket(code: str, trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_premarket, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/indicators/chip-distribution", summary='返回单只股票的筹码分布数据', description='`GET` 返回单只股票的筹码分布数据。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[ChipDistributionItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `price`（`float | None`）：价格。\n- `chip_ratio`（`float | None`）：筹码占比，单位 %。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_chip_distribution(code: str, trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_chip_distribution, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/indicators/chip-performance", summary='返回单只股票的筹码盈亏分布数据', description='`GET` 返回单只股票的筹码盈亏分布数据。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[ChipPerformanceItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `profit_ratio`（`float | None`）：获利盘占比，单位 %。\n- `avg_cost`（`float | None`）：平均成本。\n- `cost_70`（`float | None`）：70% 成本位。\n- `cost_90`（`float | None`）：90% 成本位。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_chip_performance(code: str, trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_chip_performance, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/finance/statements", summary='返回股票财务报表数据', description='`GET` 返回股票财务报表数据。\n\n## 查询参数\n\n- `code`（类型：`str`）：单个股票代码；与 `codes` 至少传一个。\n- `codes`（类型：`str`）：多个股票代码，逗号分隔；与 `code` 至少传一个。\n- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。\n- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。\n- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。\n- `report_type`（类型：`str`；默认：`income_statement`）：报表类型，可选 `income_statement`、`balance_sheet`、`cash_flow_statement`。\n\n## 返回类型\n\n顶层返回 `list[StockFinancialStatementItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `report_period`（`str`）：报告期。\n- `report_type`（`str`）：报告类型。\n- `announce_date`（`str`）：公告日期。\n- `revenue`（`float | None`）：营业收入。\n- `operating_profit`（`float | None`）：营业利润。\n- `total_profit`（`float | None`）：利润总额。\n- `net_profit`（`float | None`）：净利润。\n- `total_assets`（`float | None`）：总资产。\n- `total_liabilities`（`float | None`）：总负债。\n- `equity`（`float | None`）：权益规模。\n\n## 补充说明\n\n- `code` 与 `codes` 至少需要传一个。\n- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。')
async def api_stock_financial_statements(code: str = Query("", description='股票代码。'), codes: str = Query("", description='多个股票代码，逗号分隔；与 `code` 至少传一个。'), report_period: str = Query("", description='报告期。'), start_period: str = Query("", description='报告期起始日期，格式 `YYYY-MM-DD`。'), end_period: str = Query("", description='报告期结束日期，格式 `YYYY-MM-DD`。'), report_type: str = Query("income_statement", description='报告类型。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_financial_statements, (code, codes, report_period, start_period, end_period, report_type))


@router.get("/api/stocks/finance/indicators", summary='返回股票财务指标数据', description='`GET` 返回股票财务指标数据。\n\n## 查询参数\n\n- `code`（类型：`str`）：单个股票代码；与 `codes` 至少传一个。\n- `codes`（类型：`str`）：多个股票代码，逗号分隔；与 `code` 至少传一个。\n- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。\n- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。\n- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[StockFinanceIndicatorItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `report_period`（`str`）：报告期。\n- `roe`（`float | None`）：净资产收益率，单位 %。\n- `roa`（`float | None`）：总资产收益率，单位 %。\n- `gross_margin`（`float | None`）：毛利率，单位 %。\n- `net_margin`（`float | None`）：净利率，单位 %。\n- `asset_turnover`（`float | None`）：总资产周转率。\n- `current_ratio`（`float | None`）：流动比率。\n- `debt_to_asset`（`float | None`）：资产负债率，单位 %。\n\n## 补充说明\n\n- `code` 与 `codes` 至少需要传一个。\n- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。')
async def api_stock_finance_indicators(code: str = Query("", description='股票代码。'), codes: str = Query("", description='多个股票代码，逗号分隔；与 `code` 至少传一个。'), report_period: str = Query("", description='报告期。'), start_period: str = Query("", description='报告期起始日期，格式 `YYYY-MM-DD`。'), end_period: str = Query("", description='报告期结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_finance_indicators, (code, codes, report_period, start_period, end_period))


@router.get("/api/stocks/finance/pit/raw-period", summary="返回报告期财务 PIT 原始事实", description="返回 Tushare `fina_indicator_vip` 与 `income_vip` 合并后的公告日可见原始财务事实。保留 `announcement_date`、`actual_announcement_date`、`report_period`、`report_type`、`company_type`、`update_flag`；该接口不宣称 BigQuant 组合因子等价。")
async def api_stock_financial_pit_period(report_period: str = Query(..., description="报告期，格式 YYYY-MM-DD。")) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_financial_pit_period, (report_period,))


@router.get("/api/stocks/{code}/finance/audits", summary='返回单只股票的审计意见记录', description='`GET` 返回单只股票的审计意见记录。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。\n- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。\n- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[AuditItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `report_period`（`str`）：报告期。\n- `audit_result`（`str`）：审计意见结论。\n- `auditor`（`str`）：审计机构。\n- `sign_accountant`（`str`）：签字会计师。\n- `announce_date`（`str`）：公告日期。\n\n## 补充说明\n\n- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。')
async def api_stock_audits(code: str, report_period: str = Query("", description='报告期。'), start_period: str = Query("", description='报告期起始日期，格式 `YYYY-MM-DD`。'), end_period: str = Query("", description='报告期结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_audits, (code, report_period, start_period, end_period))


@router.get("/api/stocks/{code}/finance/disclosure-dates", summary='返回单只股票的财报披露日期记录', description='`GET` 返回单只股票的财报披露日期记录。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。\n- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。\n- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[DisclosureDateItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `report_period`（`str`）：报告期。\n- `plan_date`（`str`）：计划披露日期。\n- `actual_date`（`str`）：实际披露日期。\n- `change_reason`（`str`）：变更原因。\n\n## 补充说明\n\n- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。')
async def api_stock_disclosure_dates(code: str, report_period: str = Query("", description='报告期。'), start_period: str = Query("", description='报告期起始日期，格式 `YYYY-MM-DD`。'), end_period: str = Query("", description='报告期结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_disclosure_dates, (code, report_period, start_period, end_period))


@router.get("/api/stocks/{code}/finance/express", summary='返回单只股票的业绩快报记录', description='`GET` 返回单只股票的业绩快报记录。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。\n- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。\n- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[ExpressItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `report_period`（`str`）：报告期。\n- `announce_date`（`str`）：公告日期。\n- `revenue`（`float | None`）：营业收入。\n- `operating_profit`（`float | None`）：营业利润。\n- `total_profit`（`float | None`）：利润总额。\n- `net_profit`（`float | None`）：净利润。\n- `eps`（`float | None`）：每股收益。\n- `roe`（`float | None`）：净资产收益率，单位 %。\n\n## 补充说明\n\n- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。')
async def api_stock_express(code: str, report_period: str = Query("", description='报告期。'), start_period: str = Query("", description='报告期起始日期，格式 `YYYY-MM-DD`。'), end_period: str = Query("", description='报告期结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_express, (code, report_period, start_period, end_period))


@router.get("/api/stocks/{code}/finance/forecasts", summary='返回单只股票的业绩预告记录', description='`GET` 返回单只股票的业绩预告记录。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。\n- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。\n- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[ForecastItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `report_period`（`str`）：报告期。\n- `forecast_type`（`str`）：业绩预告类型。\n- `forecast_summary`（`str`）：业绩预告摘要。\n- `net_profit_min`（`float | None`）：净利润下限。\n- `net_profit_max`（`float | None`）：净利润上限。\n- `pct_chg_min`（`float | None`）：业绩变动幅度下限，单位 %。\n- `pct_chg_max`（`float | None`）：业绩变动幅度上限，单位 %。\n\n## 补充说明\n\n- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。')
async def api_stock_forecasts(code: str, report_period: str = Query("", description='报告期。'), start_period: str = Query("", description='报告期起始日期，格式 `YYYY-MM-DD`。'), end_period: str = Query("", description='报告期结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_forecasts, (code, report_period, start_period, end_period))


@router.get("/api/stocks/{code}/finance/main-business", summary='返回单只股票的主营业务构成', description='`GET` 返回单只股票的主营业务构成。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。\n- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。\n- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。\n- `classification`（类型：`str`；默认：`industry`）：主营业务分类口径，默认 `industry`。\n\n## 返回类型\n\n顶层返回 `list[MainBusinessItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `report_period`（`str`）：报告期。\n- `classification`（`str`）：主营业务分类口径，如行业、地区或产品。\n- `segment_name`（`str`）：分部名称。\n- `revenue`（`float | None`）：营业收入。\n- `cost`（`float | None`）：成本。\n- `profit`（`float | None`）：利润。\n- `revenue_ratio`（`float | None`）：收入占比，单位 %。\n\n## 补充说明\n\n- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。')
async def api_stock_main_business(code: str, report_period: str = Query("", description='报告期。'), start_period: str = Query("", description='报告期起始日期，格式 `YYYY-MM-DD`。'), end_period: str = Query("", description='报告期结束日期，格式 `YYYY-MM-DD`。'), classification: str = Query("industry", description='主营业务分类口径，如行业、地区或产品。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_main_business, (code, report_period, start_period, end_period, classification))


@router.get("/api/stocks/{code}/corporate-actions/dividends", summary='返回单只股票的分红送转记录', description='`GET` 返回单只股票的分红送转记录。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[DividendItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `announce_date`（`str`）：公告日期。\n- `record_date`（`str`）：股权登记日。\n- `ex_date`（`str`）：除权除息日。\n- `pay_date`（`str`）：派息日期。\n- `cash_dividend_per_share`（`float | None`）：每股现金分红。\n- `stock_dividend_per_share`（`float | None`）：每股送股。\n- `capital_reserve_per_share`（`float | None`）：每股转增资本公积。')
async def api_stock_dividends(code: str, start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_dividends, (code, start_date, end_date))


@router.get("/api/stocks/{code}/corporate-actions/repurchases", summary='返回单只股票的回购记录', description='`GET` 返回单只股票的回购记录。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[RepurchaseItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `announce_date`（`str`）：公告日期。\n- `progress`（`str`）：进度状态。\n- `repurchase_volume`（`float | None`）：回购数量。\n- `repurchase_amount`（`float | None`）：回购金额。\n- `highest_price`（`float | None`）：最高回购价。\n- `lowest_price`（`float | None`）：最低回购价。')
async def api_stock_repurchases(code: str, start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_repurchases, (code, start_date, end_date))


@router.get("/api/stocks/{code}/corporate-actions/rights-issues", summary='返回单只股票的配股记录', description='`GET` 返回单只股票的配股记录。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[RightsIssueItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `announce_date`（`str`）：公告日期。\n- `rights_ratio`（`float | None`）：配股比例。\n- `rights_price`（`float | None`）：配股价格。\n- `record_date`（`str`）：股权登记日。\n- `ex_date`（`str`）：除权除息日。')
async def api_stock_rights_issues(code: str, start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_rights_issues, (code, start_date, end_date))


@router.get(
    "/api/stocks/corporate-actions/dividends",
    response_model=DividendPage,
    summary="分页查询全市场或指定股票的分红事件",
    description="按 announce_date 的闭区间读取统一公司行动缓存。codes 为空时返回全市场已有事实；字段和每股单位与单股分红接口一致。",
)
async def api_dividends_page(
    codes: str = Query("", description="逗号分隔的股票代码；为空时查询全市场。"),
    start_date: str = Query(..., description="公告起始日期，格式 YYYY-MM-DD。"),
    end_date: str = Query(..., description="公告结束日期，格式 YYYY-MM-DD。"),
    offset: int = Query(0, ge=0, description="分页偏移量。"),
    limit: int = Query(200, ge=1, le=1000, description="分页条数。"),
    data_version: str = Query(..., min_length=1, description="/api/health 返回的市场数据版本；版本不匹配会拒绝读取。"),
) -> dict[str, object]:
    try:
        return await run_data_task(_dump_page, stocks.get_dividends_page, (codes, start_date, end_date, offset, limit, data_version))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/api/stocks/corporate-actions/rights-issues",
    response_model=RightsIssuePage,
    summary="分页查询全市场或指定股票的配股事件",
    description="按 announce_date 的闭区间读取统一公司行动缓存。codes 为空时返回全市场已有事实；字段和单位与单股配股接口一致。",
)
async def api_rights_issues_page(
    codes: str = Query("", description="逗号分隔的股票代码；为空时查询全市场。"),
    start_date: str = Query(..., description="公告起始日期，格式 YYYY-MM-DD。"),
    end_date: str = Query(..., description="公告结束日期，格式 YYYY-MM-DD。"),
    offset: int = Query(0, ge=0, description="分页偏移量。"),
    limit: int = Query(200, ge=1, le=1000, description="分页条数。"),
    data_version: str = Query(..., min_length=1, description="/api/health 返回的市场数据版本；版本不匹配会拒绝读取。"),
) -> dict[str, object]:
    try:
        return await run_data_task(_dump_page, stocks.get_rights_issues_page, (codes, start_date, end_date, offset, limit, data_version))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/stocks/{code}/corporate-actions/share-changes", summary='返回单只股票的股本变动记录', description='`GET` 返回单只股票的股本变动记录。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[ShareChangeItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `change_date`（`str`）：股本变动日期。\n- `reason`（`str`）：股本变动原因。\n- `total_share`（`float | None`）：总股本。\n- `float_share`（`float | None`）：流通股本。\n- `restricted_share`（`float | None`）：限售股本。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_share_changes(code: str, trade_date: str = Query("", description='交易日期，格式 `YYYY-MM-DD`。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_share_changes, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/corporate-actions/unlock-schedules", summary='返回单只股票的限售解禁安排', description='`GET` 返回单只股票的限售解禁安排。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `unlock_date`（类型：`str`）：解禁日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[UnlockScheduleItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `unlock_date`（`str`）：解禁日期。\n- `holder_type`（`str`）：持有人类型。\n- `unlock_volume`（`float | None`）：解禁数量。\n- `unlock_ratio`（`float | None`）：解禁比例，单位 %。\n- `share_type`（`str`）：股份类型。')
async def api_stock_unlock_schedules(code: str, unlock_date: str = Query("", description='解禁日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_unlock_schedules, (code, unlock_date, start_date, end_date))


@router.get("/api/stocks/{code}/ownership/ccass-holdings", summary='返回单只股票的中央结算持股汇总', description='`GET` 返回单只股票的中央结算持股汇总。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[CcassHoldingItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `participant_count`（`int | None`）：参与者数量。\n- `holding_volume`（`float | None`）：持有数量。\n- `holding_ratio`（`float | None`）：持有占比，单位 %。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_ccass_holdings(code: str, trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_ccass_holdings, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/ownership/ccass-holding-details", summary='返回单只股票的中央结算持股明细', description='`GET` 返回单只股票的中央结算持股明细。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[CcassHoldingDetailItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `participant_id`（`str`）：参与者编号。\n- `participant_name`（`str`）：参与者名称。\n- `holding_volume`（`float | None`）：持有数量。\n- `holding_ratio`（`float | None`）：持有占比，单位 %。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_ccass_holding_details(code: str, trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_ccass_holding_details, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/ownership/hk-connect-holdings", summary='返回单只股票的沪深港通持股数据', description='`GET` 返回单只股票的沪深港通持股数据。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[HKConnectHoldingItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `holding_volume`（`float | None`）：持有数量。\n- `holding_ratio`（`float | None`）：持有占比，单位 %。\n- `change_volume`（`float | None`）：变动数量。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_hk_connect_holdings(code: str, trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_hk_connect_holdings, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/ownership/pledges/stats", summary='返回单只股票的股权质押统计', description='`GET` 返回单只股票的股权质押统计。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[PledgeStatItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `pledge_volume`（`float | None`）：质押数量。\n- `pledge_ratio`（`float | None`）：质押比例，单位 %。\n- `unrestricted_pledge_volume`（`float | None`）：无限售股质押数量。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_pledge_stats(code: str, trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_pledge_stats, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/ownership/pledges/details", summary='返回单只股票的股权质押明细', description='`GET` 返回单只股票的股权质押明细。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n- `status`（类型：`str`）：质押状态筛选。\n\n## 返回类型\n\n顶层返回 `list[PledgeDetailItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `holder_name`（`str`）：持有人名称。\n- `start_date`（`str`）：start日期。\n- `end_date`（`str`）：END日期。\n- `pledge_volume`（`float | None`）：质押数量。\n- `pledge_ratio`（`float | None`）：质押比例，单位 %。\n- `status`（`str`）：质押状态。')
async def api_stock_pledge_details(code: str, start_date: str = Query("", description='start日期。'), end_date: str = Query("", description='END日期。'), status: str = Query("", description='质押状态。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_pledge_details, (code, start_date, end_date, status))


@router.get("/api/stocks/{code}/ownership/shareholders/count", summary='返回单只股票的股东户数记录', description='`GET` 返回单只股票的股东户数记录。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[ShareholderCountItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `holder_count`（`int | None`）：股东户数。\n- `avg_holding`（`float | None`）：户均持股数量。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_shareholder_count(code: str, trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_shareholder_count, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/ownership/shareholders/changes", summary='返回单只股票的股东户数变动记录', description='`GET` 返回单只股票的股东户数变动记录。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[ShareholderChangeItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `holder_count`（`int | None`）：股东户数。\n- `change_count`（`int | None`）：较上一期变动的户数。\n- `change_pct`（`float | None`）：变动PCT。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_shareholder_changes(code: str, trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_shareholder_changes, (code, trade_date, start_date, end_date))


@router.get("/api/stocks/{code}/ownership/shareholders/top10", summary='返回单只股票的前十大股东', description='`GET` 返回单只股票的前十大股东。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。\n- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。\n- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[ShareholderTop10Item]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `report_period`（`str`）：报告期。\n- `rank`（`int | None`）：排名。\n- `shareholder_name`（`str`）：股东名称。\n- `holding_volume`（`float | None`）：持有数量。\n- `holding_ratio`（`float | None`）：持有占比，单位 %。\n- `change_volume`（`float | None`）：相对上一报告期的持股变动数量。\n\n## 补充说明\n\n- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。')
async def api_stock_shareholder_top10(code: str, report_period: str = Query("", description='报告期。'), start_period: str = Query("", description='报告期起始日期，格式 `YYYY-MM-DD`。'), end_period: str = Query("", description='报告期结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_shareholder_top10, (code, report_period, start_period, end_period))


@router.get("/api/stocks/{code}/ownership/shareholders/top10-float", summary='返回单只股票的前十大流通股东', description='`GET` 返回单只股票的前十大流通股东。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `report_period`（类型：`str`）：单个报告期，格式 `YYYY-MM-DD`。\n- `start_period`（类型：`str`）：报告期起始日期，格式 `YYYY-MM-DD`。\n- `end_period`（类型：`str`）：报告期结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[ShareholderTop10Item]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `report_period`（`str`）：报告期。\n- `rank`（`int | None`）：排名。\n- `shareholder_name`（`str`）：股东名称。\n- `holding_volume`（`float | None`）：持有数量。\n- `holding_ratio`（`float | None`）：持有占比，单位 %。\n- `change_volume`（`float | None`）：相对上一报告期的持股变动数量。\n\n## 补充说明\n\n- `report_period` 用于精确查询单个报告期，`start_period` 与 `end_period` 用于区间筛选。')
async def api_stock_shareholder_top10_float(code: str, report_period: str = Query("", description='报告期。'), start_period: str = Query("", description='报告期起始日期，格式 `YYYY-MM-DD`。'), end_period: str = Query("", description='报告期结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_shareholder_top10_float, (code, report_period, start_period, end_period))


@router.get("/api/stocks/{code}/research/reports", summary='返回单只股票的研报记录', description='`GET` 返回单只股票的研报记录。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `report_date`（类型：`str`）：研报日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[ResearchReportItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `report_date`（`str`）：研报日期。\n- `institution`（`str`）：发布研报的机构。\n- `analyst`（`str`）：分析师。\n- `rating`（`str`）：评级。\n- `target_price`（`float | None`）：目标价。\n- `title`（`str`）：研报标题。')
async def api_stock_research_reports(code: str, report_date: str = Query("", description='研报日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_research_reports, (code, report_date, start_date, end_date))


@router.get("/api/stocks/{code}/research/surveys", summary='返回单只股票的调研记录', description='`GET` 返回单只股票的调研记录。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `survey_date`（类型：`str`）：调研日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[SurveyItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `survey_date`（`str`）：调研日期。\n- `org_name`（`str`）：调研机构名称。\n- `survey_method`（`str`）：调研方式。\n- `topic`（`str`）：调研主题。\n- `announcement_date`（`str`）：调研结果公告日期。')
async def api_stock_surveys(code: str, survey_date: str = Query("", description='调研日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_surveys, (code, survey_date, start_date, end_date))


@router.get("/api/stocks/reference/bse-code-mappings", summary='返回北交所证券代码映射关系', description='`GET` 返回北交所证券代码映射关系。\n\n## 查询参数\n\n- `old_code`（类型：`str`）：旧证券代码筛选。\n- `new_code`（类型：`str`）：新证券代码筛选。\n- `status`（类型：`str`）：代码映射状态筛选。\n\n## 返回类型\n\n顶层返回 `list[BSECodeMappingItem]`。\n\n## 返回字段\n\n- `old_code`（`str`）：旧代码。\n- `new_code`（`str`）：新代码。\n- `effective_date`（`str`）：生效日期。\n- `status`（`str`）：代码映射状态，如生效或停用。')
async def api_stock_bse_code_mappings(old_code: str = Query("", description='旧代码。'), new_code: str = Query("", description='新代码。'), status: str = Query("", description='代码映射状态，如生效或停用。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_bse_code_mappings, (old_code, new_code, status))


@router.get("/api/stocks/reference/hk-connect-targets", summary='返回沪深港通标的范围', description='`GET` 返回沪深港通标的范围。\n\n## 查询参数\n\n- `direction`（类型：`str`）：互联互通方向筛选，如 `north` 或 `south`。\n- `status`（类型：`str`）：标的状态筛选。\n- `effective_date`（类型：`str`）：生效日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[HKConnectTargetItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `name`（`str`）：名称。\n- `direction`（`str`）：互联互通方向，如 `north` 或 `south`。\n- `status`（`str`）：标的状态，如调入、调出或有效状态。\n- `effective_date`（`str`）：生效日期。')
async def api_stock_hk_connect_targets(direction: str = Query("", description='互联互通方向，如 `north` 或 `south`。'), status: str = Query("", description='标的状态，如调入、调出或有效状态。'), effective_date: str = Query("", description='生效日期。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_hk_connect_targets, (direction, status, effective_date))


@router.get("/api/stocks/{code}/quotes/auctions", summary='返回单只股票的竞价行情数据', description='`GET` 返回单只股票的竞价行情数据。\n\n## 路径参数\n\n- `code`（类型：`str`）：股票代码。\n\n## 查询参数\n\n- `session`（类型：`str`；默认：`open`）：竞价时段。\n- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。\n- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。\n- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。\n\n## 返回类型\n\n顶层返回 `list[AuctionItem]`。\n\n## 返回字段\n\n- `code`（`str`）：股票代码。\n- `trade_date`（`str`）：交易日期。\n- `auction_time`（`str`）：竞价时间。\n- `price`（`float | None`）：价格。\n- `volume`（`float | None`）：成交量。\n- `amount`（`float | None`）：成交额。\n- `session`（`str`）：竞价时段，如开盘竞价。\n\n## 补充说明\n\n- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。')
async def api_stock_auctions(code: str, session: str = Query("open", description='竞价时段，如开盘竞价。'), trade_date: str = Query("", description='交易日期。'), start_date: str = Query("", description='起始日期，格式 `YYYY-MM-DD`。'), end_date: str = Query("", description='结束日期，格式 `YYYY-MM-DD`。')) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, stocks.get_auctions, (code, session, trade_date, start_date, end_date))
