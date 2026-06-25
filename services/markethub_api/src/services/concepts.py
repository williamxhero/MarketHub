from __future__ import annotations

from fastapi import HTTPException

from quotemux import QuoteMux
from quotemux.models import ConceptAliasGroupItem, ConceptAliasResolveItem


_QUOTEMUX = QuoteMux()


def list_alias_groups(trade_date: str) -> list[ConceptAliasGroupItem]:
    return _QUOTEMUX.concept_aliases.list_alias_groups(trade_date)


def resolve_alias(provider: str, provider_concept_type: str, provider_concept_code: str, trade_date: str) -> ConceptAliasResolveItem:
    if provider == "":
        raise HTTPException(status_code=400, detail="provider 必填")
    if provider_concept_code == "":
        raise HTTPException(status_code=400, detail="provider_concept_code 必填")
    return _QUOTEMUX.concept_aliases.resolve_alias(provider, provider_concept_type, provider_concept_code, trade_date)


def get_alias_group(concept_id: str, trade_date: str) -> ConceptAliasGroupItem:
    if concept_id == "":
        raise HTTPException(status_code=400, detail="concept_id 必填")
    return _QUOTEMUX.concept_aliases.get_alias_group(concept_id, trade_date)
