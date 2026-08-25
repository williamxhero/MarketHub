from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ConceptLineageItem(BaseModel):
    source: str
    row_count: int
    watermark: str = ""
    mutation: str = "0"
    approximate: bool = False
    membership_basis: Literal["knowledge-time", "effective-date"] = "knowledge-time"
    pit_quality: Literal["strict_knowledge_time", "effective_date_approximation"] = "strict_knowledge_time"


class ConceptResearchMeta(BaseModel):
    contract: Literal["research-v1"] = "research-v1"
    pit_mode: Literal["strict", "approx-historical", "effective-date"] = "strict"
    approximate: bool = False
    research_profile: Literal["research-v1", "damxj-approx-historical-v1", "damxj-effective-date-v1"] = "research-v1"
    data_version: str
    complete: bool
    capability: Literal["available", "incomplete", "unavailable"]
    total_rows: int
    returned_rows: int
    issues: list[str] = Field(default_factory=list)
    lineage: list[ConceptLineageItem] = Field(default_factory=list)
    source_semantics: dict[str, str] = Field(default_factory=dict)


class ConceptCatalogResearchItem(BaseModel):
    concept_id: str
    concept_type: str = ""
    name: str
    market: str = ""
    status: str = ""


class ConceptCatalogEnvelope(BaseModel):
    items: list[ConceptCatalogResearchItem]
    meta: ConceptResearchMeta


class ConceptDailyBarResearchItem(BaseModel):
    concept_id: str
    concept_name: str = ""
    trade_date: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    amount: float | None = None


class ConceptMembershipResearchItem(BaseModel):
    concept_id: str
    code: str
    name: str = ""
    valid_from: str
    valid_to: str | None = None
    knowledge_time: str | None = None
    knowledge_time_status: Literal["known", "unknown", "unavailable"] = "unknown"


class ConceptMembershipEnvelope(BaseModel):
    items: list[ConceptMembershipResearchItem]
    meta: ConceptResearchMeta


class ConceptMemberHistoryResearchItem(ConceptMembershipResearchItem):
    effective_date: str
    action: Literal["in", "out"]
    action_timing: Literal["start_of_day", "end_of_day"]


class ConceptMemberHistoryEnvelope(BaseModel):
    items: list[ConceptMemberHistoryResearchItem]
    meta: ConceptResearchMeta


class ConceptMoneyFlowResearchItem(BaseModel):
    concept_id: str
    trade_date: str
    scope: Literal["concept"] = "concept"
    inflow: float | None = None
    outflow: float | None = None
    net_inflow: float | None = None


class ConceptMoneyFlowEnvelope(BaseModel):
    items: list[ConceptMoneyFlowResearchItem]
    meta: ConceptResearchMeta


class ConceptDailyStatsResearchItem(BaseModel):
    concept_id: str
    trade_date: str
    member_count: int
    limit_up_count: int
    main_net_inflow_amount: float | None = None
    turnover_amount: float | None = None
    missing_price_band_count: int = 0
    missing_money_flow_count: int = 0
    unknown_knowledge_count: int = 0


class ConceptDailyStatsEnvelope(BaseModel):
    items: list[ConceptDailyStatsResearchItem]
    meta: ConceptResearchMeta


class ConceptDailyBarsEnvelope(BaseModel):
    items: list[ConceptDailyBarResearchItem]
    meta: ConceptResearchMeta
    daily_stats: list[ConceptDailyStatsResearchItem] = Field(default_factory=list)
    daily_stats_meta: ConceptResearchMeta | None = None
