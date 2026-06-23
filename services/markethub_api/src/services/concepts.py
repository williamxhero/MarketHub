from __future__ import annotations

from fastapi import HTTPException

from quotemux import QuoteMux
from quotemux.models import ConceptAliasGroupItem, ConceptAliasResolveItem


_QUOTEMUX = QuoteMux()


def resolve_alias(provider: str, board_code: str, trade_date: str) -> ConceptAliasResolveItem:
    if provider == "":
        raise HTTPException(status_code=400, detail="provider 不能为空")
    if board_code == "":
        raise HTTPException(status_code=400, detail="board_code 不能为空")
    return _QUOTEMUX.concepts.resolve_alias(provider, board_code, trade_date)


def get_alias_group(concept_id: str, trade_date: str) -> ConceptAliasGroupItem:
    if concept_id == "":
        raise HTTPException(status_code=400, detail="concept_id 不能为空")
    return _QUOTEMUX.concepts.get_alias_group(concept_id, trade_date)
