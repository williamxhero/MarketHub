from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from quotemux.models import ApiError
from quotemux.futures import FutureContractCatalogIncompleteError
from platform_models import FutureContractCatalogItem, FutureContractRealtimeQuoteItem, FutureMainContractMappingItem

from data_threads import run_data_task
from services import futures
from services.dataset_versions import require_dataset_version


router = APIRouter()


_LIVE_FUTURES_RESPONSES = {
    400: {"model": ApiError, "description": "品种代码或实际合约符号不合法。"},
    422: {"model": ApiError, "description": "查询参数缺失或不符合接口约束。"},
    503: {"model": ApiError, "description": "未启用 TqSdk source instance，或实时上游暂不可用。"},
}

_CATALOG_DATASET_ID = "future_contract_reference"
_CATALOG_RESPONSES = {
    **_LIVE_FUTURES_RESPONSES,
    409: {"model": ApiError, "description": "本地已发布合约目录不完整；普通查询不会调用 provider。"},
}


def _load_and_dump(loader: Callable[..., list[object]], args: tuple[object, ...]) -> list[dict[str, object]]:
    return [item.model_dump() for item in loader(*args) if hasattr(item, "model_dump")]


@router.get(
    "/api/futures/quotes/1m",
    summary="查询中国期货 1 分钟行情",
    description=(
        "统一查询已落库的期货 1m 数据。`back_adjusted_continuous` 是 L0 后复权连续序列；"
        "`main_continuous` 是主力连续原始序列。两个口径不会自动拼接。"
    ),
)
async def api_future_quotes_1m(
    codes: str = Query(..., min_length=1, description="期货品种代码，逗号分隔，如 IF,au。"),
    series_type: Literal["back_adjusted_continuous", "main_continuous"] = Query("main_continuous", description="明确的数据口径。"),
    start_time: str = Query("", description="起始 bar 结束时间。"),
    end_time: str = Query("", description="结束 bar 结束时间。"),
    limit: int = Query(10000, ge=1, le=500000),
) -> list[dict[str, object]]:
    return await run_data_task(_load_and_dump, futures.get_quotes_1m, (codes, series_type, start_time, end_time, limit))


@router.get(
    "/api/futures/quotes/realtime",
    summary="查询中国期货主力连续实时快照",
    description=(
        "通过 TqSdk 获取期货主力连续合约的实时快照，仅反映调用时刻的行情，且不落库。"
        "该数据不会与 EDB T+1 历史数据或 Apex L0 序列拼接、混写。"
    ),
)
async def api_future_realtime_quotes(
    codes: str = Query(..., min_length=1, description="期货品种代码，逗号分隔，如 IF,au。"),
) -> list[dict[str, object]]:
    return await run_data_task(_load_and_dump, futures.get_main_continuous_realtime, (codes,))


@router.get(
    "/api/futures/contracts",
    response_model=list[FutureContractCatalogItem],
    summary="查询中国期货实际合约目录与规格",
    description="""返回已持久化的国内期货实际合约目录与规格。

## 数据来源与新鲜度

- 数据由管理员 repair/capture 从 `shinny_tqsdk` 持久化并原子发布；普通 `GET` 只读 QuoteMux 本地快照，绝不调用 provider、安装依赖或写库。
- `raw_metadata` 保留采集时的上游原始元数据；`snapshot_id`、`captured_at`、`source`、`provenance` 和 `availability` 使引用数据可审计。
- `product_code`、`exchange`、`currency`、`tick_size`、`price_precision` 与 `multiplier` 来自已发布 native catalog。`asset_class`、`lot_size`、手续费与保证金属于 Quant Runtime execution profile；若 profile 未冻结，字段为 null 且 `availability`/`provenance` 明确说明，绝不以零伪造。
- native provider symbol 与 contract symbol 保持原样，不由 MarketHub 猜测或拼接别名。

## 查询参数

- `codes`：可选，逗号分隔的期货品种代码；为空时返回全部 84 个国内品种可见的未过期合约，例如 `IF,au`。
- `include_expired`：是否包含上游标记为过期的合约，默认 `false`。
- `dataset_version`：可选版本钉住；与 `/api/health` 的 `future_contract_reference` 不一致时返回 `DATASET_VERSION_STALE`。

若未发布完整快照或 scope 缺失，返回 `DATA_INCOMPLETE` 及 `/api/admin/data-repairs` repair 模板。该接口不返回行情，也不会与 EDB T+1 或 Apex L0 历史序列拼接、混写。""",
    responses=_CATALOG_RESPONSES,
)
async def api_future_contract_catalog(
    response: Response,
    codes: str = Query("", description="可选的期货品种代码，逗号分隔；留空查询全部国内品种。", examples=["IF,au"]),
    include_expired: bool = Query(False, description="是否包含上游标记为过期的实际合约。"),
    dataset_version: str = Query("", description="可选的 future_contract_reference dataset version 钉住。"),
) -> list[FutureContractCatalogItem]:
    current_version = require_dataset_version(_CATALOG_DATASET_ID, dataset_version)
    try:
        items = await run_data_task(futures.get_contract_catalog, codes, include_expired, current_version)
    except FutureContractCatalogIncompleteError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DATA_INCOMPLETE",
                "message": "本地期货合约目录不完整；普通查询禁止调用 provider 或写库",
                "details": exc.details,
            },
        ) from exc
    response.headers["X-MarketHub-Dataset-Version"] = current_version
    return items


@router.get(
    "/api/futures/contracts/main-mapping",
    response_model=list[FutureMainContractMappingItem],
    summary="查询期货品种当前主力实际合约映射",
    description="""返回品种代码到当前主力**实际交割合约**的映射，而不是主连行情序列。

## 数据来源与新鲜度

- 唯一 source 是 TqSdk；每次请求即时读取，不写入任何本地事实表或参考表。
- `updated_time` 是上游主力映射的观测/更新时间；调用方应以它判断映射新鲜度。
- `codes` 留空时查询完整的 84 个国内期货品种；传值时按 QuoteMux 的大小写无关品种宇宙校验。

映射结果可用于构造 `/api/futures/contracts/realtime` 的 `symbols`。它不会与 EDB T+1 主连 1 分钟数据、Apex L0 或 `/api/futures/quotes/realtime` 的主连快照拼接、混写。""",
    responses=_LIVE_FUTURES_RESPONSES,
)
async def api_future_main_contract_mappings(
    codes: str = Query("", description="可选的期货品种代码，逗号分隔；留空查询全部 84 个国内品种。", examples=["IF,au"]),
) -> list[FutureMainContractMappingItem]:
    return await run_data_task(futures.get_main_contract_mappings, codes)


@router.get(
    "/api/futures/contracts/realtime",
    response_model=list[FutureContractRealtimeQuoteItem],
    summary="查询指定实际交割合约的实时快照",
    description="""返回指定 TqSdk 实际交割合约的完整实时 quote 快照。

## 数据来源与新鲜度

- `symbols` 必须是 TqSdk 实际交割合约符号，例如 `CFFEX.IF2609,SHFE.au2608`；服务只去空白和去重，不将其猜测、改写为主连符号。
- 唯一 source 是 TqSdk，调用时即时读取且不落库。`quote_time` 是上游行情时间；报价、盘口或状态为空表示 source 当时未提供该字段，不代表历史补齐。
- 响应提供 OHLC、昨结/结算、涨跌停、成交额/量、持仓量、五档买卖价量及交易状态。

本接口与 `/api/futures/quotes/realtime` 的主连快照严格分离；不读取、拼接或混写 EDB T+1 与 Apex L0 历史序列。""",
    responses=_LIVE_FUTURES_RESPONSES,
)
async def api_future_contract_realtime(
    symbols: str = Query(..., min_length=1, description="必填的 TqSdk 实际交割合约符号，逗号分隔。", examples=["CFFEX.IF2609,SHFE.au2608"]),
) -> list[FutureContractRealtimeQuoteItem]:
    return await run_data_task(futures.get_contract_realtime, symbols)


@router.get("/api/futures/coverage", summary="查询期货 1 分钟覆盖范围")
async def api_future_coverage(
    series_type: Literal["", "back_adjusted_continuous", "main_continuous"] = Query("", description="留空返回全部口径。"),
) -> list[dict[str, object]]:
    return await run_data_task(futures.list_coverage, series_type)


@router.post("/api/admin/futures/quotes/main-continuous/update", summary="增量更新期货主力连续 1 分钟数据")
async def api_update_future_main_continuous(
    overlap_days: int = Query(2, ge=1, le=30, description="为幂等修订保留的重叠抓取天数。"),
) -> dict[str, object]:
    return await run_data_task(futures.update_main_continuous, overlap_days)
