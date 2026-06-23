from __future__ import annotations

from fastapi import APIRouter, Query

from data_threads import run_data_task
from services import concepts


router = APIRouter()


def _dump_item(loader, args: tuple[object, ...]) -> dict[str, object]:
    return loader(*args).model_dump()


@router.get("/api/concepts/alias/resolve")
async def api_concept_alias_resolve(
    provider: str = Query(...),
    board_code: str = Query(...),
    trade_date: str = Query(""),
) -> dict[str, object]:
    return await run_data_task(_dump_item, concepts.resolve_alias, (provider, board_code, trade_date))


@router.get("/api/concepts/alias/groups/{concept_id}")
async def api_concept_alias_group(
    concept_id: str,
    trade_date: str = Query(""),
) -> dict[str, object]:
    return await run_data_task(_dump_item, concepts.get_alias_group, (concept_id, trade_date))
