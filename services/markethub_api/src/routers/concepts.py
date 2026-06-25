from __future__ import annotations

from fastapi import APIRouter, Query

from data_threads import run_data_task
from services import concepts


router = APIRouter()

ALIAS_RESOLVE_DESCRIPTION = """按 provider 原始题材概念标识解析系统 Concept ID。

调用方只有 `provider + provider_concept_type + provider_concept_code` 时，先用本接口解析出系统 `concept_id`，再调用 `/api/concepts/*` 系统题材概念接口。
"""

ALIAS_GROUPS_DESCRIPTION = """列出系统 Concept ID 与 provider 原始题材概念标识的完整映射超集。

`trade_date` 为空时返回全量映射；传入交易日时，只返回在该日期有效的 provider 原始题材概念成员。
"""

ALIAS_GROUP_DESCRIPTION = """查询单个系统 Concept ID 下的 provider 原始题材概念成员。

`trade_date` 为空时返回该系统题材概念的全量成员；传入交易日时按成员 `start_date/end_date` 过滤。
"""


def _dump_item(loader, args: tuple[object, ...]) -> dict[str, object]:
    return loader(*args).model_dump()


def _dump_item_list(loader, args: tuple[object, ...]) -> list[dict[str, object]]:
    return [item.model_dump() for item in loader(*args)]


@router.get("/api/concepts/alias/resolve", summary="按 provider 原始题材概念标识解析系统 Concept ID", description=ALIAS_RESOLVE_DESCRIPTION)
async def api_concept_alias_resolve(
    provider: str = Query(..., description="provider ID，例如 tushare 或 akshare。"),
    provider_concept_type: str = Query("", description="provider 原始题材概念类型，例如 ths、dc、tdx、kpl。"),
    provider_concept_code: str = Query(..., description="provider 原始题材概念代码，例如 885806、BK0854、301459。"),
    trade_date: str = Query("", description="交易日，支持 YYYYMMDD 或 YYYY-MM-DD；为空时不做有效期过滤。"),
) -> dict[str, object]:
    return await run_data_task(_dump_item, concepts.resolve_alias, (provider, provider_concept_type, provider_concept_code, trade_date))


@router.get("/api/concepts/alias/groups", summary="列出系统题材概念 provider 映射", description=ALIAS_GROUPS_DESCRIPTION)
async def api_concept_alias_groups(
    trade_date: str = Query("", description="交易日，支持 YYYYMMDD 或 YYYY-MM-DD；为空时返回全量映射。"),
) -> list[dict[str, object]]:
    return await run_data_task(_dump_item_list, concepts.list_alias_groups, (trade_date,))


@router.get("/api/concepts/alias/groups/{concept_id}", summary="查询单个系统题材概念 provider 映射", description=ALIAS_GROUP_DESCRIPTION)
async def api_concept_alias_group(
    concept_id: str,
    trade_date: str = Query("", description="交易日，支持 YYYYMMDD 或 YYYY-MM-DD；为空时返回全量成员。"),
) -> dict[str, object]:
    return await run_data_task(_dump_item, concepts.get_alias_group, (concept_id, trade_date))
