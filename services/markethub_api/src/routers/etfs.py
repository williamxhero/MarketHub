from __future__ import annotations

from fastapi import APIRouter, Query

from quotemux.models import EtfCatalogItem, EtfDailyQuotesQueryResult

from data_threads import run_quote_task
from services import etfs


router = APIRouter()


@router.get(
    "/api/funds/etfs",
    response_model=list[EtfCatalogItem],
    summary="返回场内 ETF 目录",
    description="""返回 MarketHub 已接入的中国场内 ETF 目录。

## 数据来源与一致性

- 目录唯一 source 是 Tushare `fund_basic(market=\"E\")`。
- 结果先读取 `ref.etf`；本地不存在时才请求 Tushare 并写回。
- `ts_code` 是唯一外部标识，必须包含交易所后缀，例如 `510300.SH`、`159915.SZ`。
- AKShare、efinance、mootdx 与 opentdx 未注册 ETF 目录能力，不能混入 ETF 目录。

## 查询参数

- `ts_codes`：可选，逗号分隔的完整 `ts_code`。
- `name`：可选，ETF 名称关键字。
- `include_delisted`：是否包含已退市 ETF，默认 `false`。
- `limit`、`offset`：分页参数。

## 返回字段

- `ts_code`：Tushare ETF 唯一代码。
- `code`、`market`：数据库主键组成部分，分别为六位证券代码与 `SHSE`/`SZSE`。
- `name`、`fund_type`、`management`、`custodian`：Tushare 目录原始字段。
- `list_date`、`delist_date`：上市与退市日期；没有退市日期时为空字符串。""",
)
async def api_etf_catalog(
    ts_codes: str = Query("", description="逗号分隔的完整 ETF ts_code，例如 510300.SH,159915.SZ。"),
    name: str = Query("", description="ETF 名称关键字。"),
    include_delisted: bool = Query(False, description="是否包含已退市 ETF。"),
    limit: int = Query(5000, ge=1, le=5000, description="返回记录上限。"),
    offset: int = Query(0, ge=0, description="结果偏移量。"),
) -> list[EtfCatalogItem]:
    return await run_quote_task(etfs.get_catalog, ts_codes, name, include_delisted, limit, offset)


@router.get(
    "/api/funds/etfs/quotes/daily",
    response_model=EtfDailyQuotesQueryResult,
    summary="返回场内 ETF 未复权日线及完整性元数据",
    description="""返回中国场内 ETF 的未复权日线，供可复现回测使用。

## 数据合同

- 日线 source 按运行时策略依次为 Tushare `fund_daily`、AKShare `fund_etf_hist_em(adjust=\"\")`、efinance `stock.get_quote_history`；仅对前一 source 未覆盖的交易日继续请求下一 source。
- 数据先读 `fact.etf_daily_1d`；仅对本地缺失交易日按上述顺序补齐并写回。每次 provider 调用、命中、冲突与错误均记录在运行时审计事件中。
- Tushare 是 ETF 目录的唯一 source；AKShare 与 efinance 不提供目录能力。mootdx 与 opentdx 对 ETF 实测返回 0 行，未注册为 ETF source。
- 只支持未复权价格；接口没有 `adjust` 参数，也不伪造复权价。
- 调用方必须以完整 `ts_code` 请求，不能只传六位代码。

## 查询参数

- `ts_codes`：必填，逗号分隔的完整 ETF 代码，例如 `510300.SH,510500.SH,159915.SZ`。
- `trade_date`：单个交易日，格式 `YYYY-MM-DD`；与日期范围参数二选一。
- `start_date`、`end_date`：闭区间交易日范围，格式 `YYYY-MM-DD`。
- `limit`：对整个 `items` 集合的硬裁剪上限；裁剪后 `meta.complete=false`。
- `meta_detail`：`summary` 只返回缺失数量；`full` 额外返回每只 ETF 的 `missing_trade_dates`。

## 返回类型

顶层返回 `EtfDailyQuotesQueryResult`，由 `items` 和不可省略的 `meta` 组成。

## 完整性规则

- `meta.codes[*].expected_bar_count` 是 SSE 交易日历在请求范围内的应有日线数量。
- `actual_bar_count` 只计入有收盘价的事实行。
- 任一 ETF 缺失任一应有交易日，或结果被 `limit` 裁剪，相关 `complete=false`，顶层 `meta.complete` 同样为 `false`。
- `fields` 不存在，避免调用方意外裁掉完整性元数据。

## 字段口径

`open`、`high`、`low`、`close`、`pre_close`、`change`、`pct_chg`、`volume`、`amount` 均按统一 ETF 日线口径返回；`pct_chg` 单位为百分比，`volume` 单位为手，`amount` 单位为千元。""",
)
async def api_etf_daily_quotes(
    ts_codes: str = Query(..., description="逗号分隔的完整 ETF ts_code；六位代码会被拒绝。"),
    trade_date: str = Query("", description="单个交易日，格式 YYYY-MM-DD。"),
    start_date: str = Query("", description="起始交易日，格式 YYYY-MM-DD。"),
    end_date: str = Query("", description="结束交易日，格式 YYYY-MM-DD。"),
    limit: int | None = Query(None, ge=1, description="整个响应 items 的硬裁剪上限。"),
    meta_detail: str = Query("summary", description="完整性明细级别：summary 或 full。"),
) -> EtfDailyQuotesQueryResult:
    return await run_quote_task(etfs.get_daily_quotes, ts_codes, trade_date, start_date, end_date, limit, meta_detail)
