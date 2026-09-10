from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[0-9]{6}$")
_PUBLIC_FIELDS = (
    "code",
    "name",
    "exchange",
    "market",
    "list_status",
    "list_date",
    "delist_date",
    "industry",
    "listing_board",
    "area",
)

_AUTHORITATIVE_SOURCE_QUERY = """
select stock.identity_status, stock.identity_source, stock.market, stock.code, stock.name,
       stock.industry, stock.listing_board, stock.listed_date::text as listed_date,
       stock.delisted_date::text as delisted_date, item.area, item.list_status
from ref.stock stock
left join audit.stock_authority_input_item item
  on item.input_id = stock.authority_input_id
 and item.market = stock.market
 and item.code = stock.code
where stock.code <> '000000'
order by stock.code, stock.market
"""

_LATEST_AUTHORITY_EVIDENCE_QUERY = """
select reconciliation.input_id, authority_input.content_sha256, authority_input.provider,
       authority_input.source_refreshed_at, authority_input.fresh_through,
       reconciliation.provisional_count, reconciliation.conflict_count
from audit.stock_reference_reconciliation reconciliation
join audit.stock_authority_input authority_input on authority_input.input_id = reconciliation.input_id
where authority_input.request_status = 'accepted'
  and reconciliation.transaction_result = 'committed'
order by reconciliation.committed_at desc
limit 1
"""

_LATEST_COMPLETED_TRADING_DAY_QUERY = """
with local_time as (
    select now() at time zone 'Asia/Shanghai' as value
)
select max(calendar.trade_date)::text as trade_date
from ref.trade_calendar calendar, local_time
where calendar.exchange in ('SSE','SHSE','SZSE','BSE','BJSE')
  and calendar.is_open
  and calendar.trade_date <= case
      when local_time.value::time < time '15:30' then local_time.value::date - 1
      else local_time.value::date
  end
"""


class CatalogCandidateRejected(ValueError):
    """The input cannot become a client-visible catalog version."""


@dataclass(frozen=True)
class CatalogSourceEvidence:
    input_id: str
    content_sha256: str
    provider: str
    source_refreshed_at: datetime
    fresh_through: date
    provisional_count: int
    conflict_count: int


@dataclass(frozen=True)
class StockCatalogItem:
    code: str
    name: str
    exchange: str
    market: str
    list_status: str
    list_date: str
    delist_date: str
    industry: str = ""
    listing_board: str = ""
    area: str = ""

    def public_payload(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in _PUBLIC_FIELDS}


@dataclass(frozen=True)
class StockCatalogCandidate:
    version: str
    content_sha256: str
    items: tuple[StockCatalogItem, ...]
    source: CatalogSourceEvidence

    @property
    def provisional_count(self) -> int:
        return self.source.provisional_count


def _text(value: object, field: str, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise CatalogCandidateRejected(f"{field} must be a string")
    return value.strip()


def _normalize_date(value: object, field: str) -> str:
    if value is None:
        return ""
    text = _text(value, field)
    if text == "":
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise CatalogCandidateRejected(f"{field} must be an ISO date") from exc


def _validated_evidence(
    evidence: CatalogSourceEvidence, expected_fresh_through: date
) -> CatalogSourceEvidence:
    if evidence.provider != "tushare":
        raise CatalogCandidateRejected("authoritative provider must be tushare")
    if (
        _SHA256.fullmatch(evidence.input_id) is None
        or _SHA256.fullmatch(evidence.content_sha256) is None
    ):
        raise CatalogCandidateRejected("authority input identity must be SHA-256")
    if (
        evidence.source_refreshed_at.tzinfo is None
        or evidence.source_refreshed_at.utcoffset() is None
    ):
        raise CatalogCandidateRejected("source_refreshed_at must be timezone-aware")
    if evidence.fresh_through < expected_fresh_through:
        raise CatalogCandidateRejected(
            "authority input is not fresh through the required trading day"
        )
    if evidence.provisional_count < 0 or evidence.conflict_count < 0:
        raise CatalogCandidateRejected("authority audit counts must be non-negative")
    if evidence.conflict_count != 0:
        raise CatalogCandidateRejected("authority input has unresolved conflict count")
    return evidence


def _normalize_item(row: Mapping[str, object]) -> StockCatalogItem:
    identity_source = _text(row.get("identity_source"), "identity_source", required=True)
    if identity_source != "tushare_catalog":
        raise CatalogCandidateRejected("authoritative identity has an unsupported provenance")
    code = _text(row.get("code"), "code", required=True)
    if _CODE.fullmatch(code) is None:
        raise CatalogCandidateRejected("code must be a six-digit stock code")
    name = _text(row.get("name"), "name", required=True)
    if name == "":
        raise CatalogCandidateRejected("name must be non-empty after Unicode trim")
    exchange = _text(row.get("market"), "market", required=True)
    if exchange == "":
        raise CatalogCandidateRejected("market must be non-empty")
    listing_board = _text(row.get("listing_board"), "listing_board")
    list_status = _text(row.get("list_status"), "list_status")
    delist_date = _normalize_date(row.get("delisted_date"), "delisted_date")
    if list_status == "":
        list_status = "D" if delist_date else "L"
    if list_status not in {"L", "P", "D"}:
        raise CatalogCandidateRejected("list_status must be L, P, or D")
    if list_status == "D" and delist_date == "":
        raise CatalogCandidateRejected("delisted catalog item requires delisted_date")
    return StockCatalogItem(
        code=code,
        name=name,
        exchange=exchange,
        market=listing_board,
        list_status=list_status,
        list_date=_normalize_date(row.get("listed_date"), "listed_date"),
        delist_date=delist_date,
        industry=_text(row.get("industry"), "industry"),
        listing_board=listing_board,
        area=_text(row.get("area"), "area"),
    )


def build_stock_catalog_candidate(
    rows: Iterable[Mapping[str, object]],
    evidence: CatalogSourceEvidence,
    *,
    expected_fresh_through: date,
) -> StockCatalogCandidate:
    """Freeze a full, authoritative public catalog before any version is published."""
    source = _validated_evidence(evidence, expected_fresh_through)
    items: list[StockCatalogItem] = []
    seen_codes: set[str] = set()
    for row in rows:
        identity_status = _text(row.get("identity_status"), "identity_status", required=True)
        if identity_status == "provisional":
            continue
        if identity_status != "authoritative":
            raise CatalogCandidateRejected("identity_status must be authoritative or provisional")
        item = _normalize_item(row)
        if item.code in seen_codes:
            raise CatalogCandidateRejected("authoritative catalog contains duplicate code")
        seen_codes.add(item.code)
        items.append(item)
    if not items:
        raise CatalogCandidateRejected("authoritative catalog candidate is empty")
    frozen_items = tuple(sorted(items, key=lambda item: (item.code, item.exchange)))
    encoded = json.dumps(
        [item.public_payload() for item in frozen_items],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    content_sha256 = hashlib.sha256(encoded).hexdigest()
    return StockCatalogCandidate(f"mhc-v1-{content_sha256}", content_sha256, frozen_items, source)


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        connect_timeout=10,
        row_factory=dict_row,
        application_name="markethub-stock-catalog-candidate",
    )


def _record_value(row: Mapping[str, object], field: str) -> object:
    try:
        return row[field]
    except KeyError as exc:
        raise CatalogCandidateRejected(f"authority query omitted {field}") from exc


def _parse_date(value: object, field: str) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise CatalogCandidateRejected(f"{field} must be a date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CatalogCandidateRejected(f"{field} must be an ISO date") from exc


def _parse_timestamp(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise CatalogCandidateRejected(f"{field} must be an ISO timestamp") from exc
    else:
        raise CatalogCandidateRejected(f"{field} must be a timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CatalogCandidateRejected(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def build_current_stock_catalog_candidate(
    *, connection_factory: Callable[[], Any] = _connect
) -> StockCatalogCandidate:
    """Read the completed authoritative reconciliation into an immutable candidate."""
    with connection_factory() as connection:
        expected_row = connection.execute(_LATEST_COMPLETED_TRADING_DAY_QUERY).fetchone()
        evidence_row = connection.execute(_LATEST_AUTHORITY_EVIDENCE_QUERY).fetchone()
        rows = connection.execute(_AUTHORITATIVE_SOURCE_QUERY).fetchall()
    if expected_row is None or evidence_row is None:
        raise CatalogCandidateRejected("fresh authoritative source evidence is unavailable")
    expected_fresh_through = _parse_date(
        _record_value(expected_row, "trade_date"), "latest completed trading day"
    )
    evidence = CatalogSourceEvidence(
        input_id=str(_record_value(evidence_row, "input_id") or ""),
        content_sha256=str(_record_value(evidence_row, "content_sha256") or ""),
        provider=str(_record_value(evidence_row, "provider") or ""),
        source_refreshed_at=_parse_timestamp(
            _record_value(evidence_row, "source_refreshed_at"), "source_refreshed_at"
        ),
        fresh_through=_parse_date(_record_value(evidence_row, "fresh_through"), "fresh_through"),
        provisional_count=int(_record_value(evidence_row, "provisional_count") or 0),
        conflict_count=int(_record_value(evidence_row, "conflict_count") or 0),
    )
    return build_stock_catalog_candidate(
        rows, evidence, expected_fresh_through=expected_fresh_through
    )
