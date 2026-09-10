from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from services.stock_catalog_candidate import (
    CatalogCandidateRejected,
    StockCatalogCandidate,
    build_current_stock_catalog_candidate,
)
from services.stock_name_history_publication import (
    DDL as _STOCK_NAME_HISTORY_DDL,
    publish_stock_name_history_snapshot,
)

RETAIN_HEALTHY_VERSION_FOR_HOURS = 24
KNOWN_DIRTY_DATA_VERSION = "mhf-v1-02f1aa9d6e2eb0553d88c53e0d0023a070a786e94896a66fb4696e407a00065f"
_DATA_VERSION = re.compile(r"^mhf-v1-[0-9a-f]{64}$")

_DDL = """
create schema if not exists readmodel;
create schema if not exists audit;
create table if not exists readmodel.stock_catalog_version (
    catalog_version text primary key check (catalog_version ~ '^mhc-v1-[0-9a-f]{64}$'),
    content_sha256 text not null unique check (content_sha256 ~ '^[0-9a-f]{64}$'),
    authority_input_id text not null check (authority_input_id ~ '^[0-9a-f]{64}$'),
    authority_content_sha256 text not null check (authority_content_sha256 ~ '^[0-9a-f]{64}$'),
    authority_provider text not null check (authority_provider = 'tushare'),
    source_refreshed_at_utc timestamp with time zone not null,
    fresh_through date not null,
    provisional_count integer not null check (provisional_count >= 0),
    conflict_count integer not null check (conflict_count = 0),
    row_count integer not null check (row_count > 0),
    status text not null check (status in ('healthy','quarantined')),
    created_at_utc timestamp with time zone not null default clock_timestamp()
);
create table if not exists readmodel.stock_catalog_item (
    catalog_version text not null references readmodel.stock_catalog_version(catalog_version),
    code text not null check (code ~ '^[0-9]{6}$'),
    name text not null check (btrim(name) <> ''),
    exchange text not null,
    market text not null,
    list_status text not null check (list_status in ('L','P','D')),
    list_date date,
    delist_date date,
    industry text not null default '',
    listing_board text not null default '',
    area text not null default '',
    primary key (catalog_version,code),
    check (list_status <> 'D' or delist_date is not null)
);
create index if not exists stock_catalog_item_page_idx
    on readmodel.stock_catalog_item(catalog_version,code,exchange);
create table if not exists readmodel.stock_catalog_current (
    singleton boolean primary key default true check (singleton),
    catalog_version text not null references readmodel.stock_catalog_version(catalog_version),
    activated_at_utc timestamp with time zone not null default clock_timestamp()
);
create table if not exists readmodel.stock_catalog_data_version (
    data_version text primary key,
    catalog_version text references readmodel.stock_catalog_version(catalog_version),
    status text not null check (status in ('healthy','quarantined')),
    serve_until_utc timestamp with time zone not null,
    reason text not null default '',
    created_at_utc timestamp with time zone not null default clock_timestamp(),
    check (
        (status = 'healthy' and catalog_version is not null)
        or (status = 'quarantined' and catalog_version is null)
    )
);
create index if not exists stock_catalog_data_version_retention_idx
    on readmodel.stock_catalog_data_version(status,serve_until_utc);
create table if not exists audit.stock_catalog_publication_attempt (
    attempt_id bigserial primary key,
    catalog_version text not null,
    content_sha256 text not null,
    authority_input_id text not null,
    authority_content_sha256 text not null,
    fresh_through date,
    source_refreshed_at_utc timestamp with time zone,
    candidate_count integer not null check (candidate_count >= 0),
    provisional_count integer not null check (provisional_count >= 0),
    conflict_count integer not null check (conflict_count >= 0),
    name_gate_passed boolean not null,
    prior_catalog_version text not null default '',
    result text not null check (result in ('published','rejected')),
    reason text not null default '',
    created_at_utc timestamp with time zone not null default clock_timestamp()
);
""" + _STOCK_NAME_HISTORY_DDL


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        connect_timeout=10,
        row_factory=dict_row,
        application_name="markethub-stock-catalog-publication",
    )


@dataclass(frozen=True)
class CatalogPublicationResult:
    catalog_version: str
    previous_catalog_version: str
    data_version: str
    row_count: int


@dataclass(frozen=True)
class CatalogCurrentVersion:
    catalog_version: str
    activated_at_utc: str


@dataclass(frozen=True)
class ResolvedCatalogVersion:
    data_version: str
    catalog_version: str


@dataclass(frozen=True)
class CatalogPublicationReadiness:
    registry_active: bool
    current: CatalogCurrentVersion | None


def catalog_publication_readiness(
    *,
    connection_factory: Callable[[], Any] = _connect,
) -> CatalogPublicationReadiness:
    """Identify migration activation separately from an empty current pointer.

    An unconfigured local process keeps the legacy reader for development.
    A process configured for the MarketHub database treats a missing registry
    exactly like an empty registry: it is not ready and never falls back to a
    live source read.
    """
    try:
        connection = connection_factory()
    except KeyError:
        return CatalogPublicationReadiness(registry_active=False, current=None)
    with connection:
        registry = connection.execute(
            "select to_regclass('readmodel.stock_catalog_current')::text as relation_name"
        ).fetchone()
        if registry is None or not registry.get("relation_name"):
            return CatalogPublicationReadiness(registry_active=True, current=None)
        row = connection.execute(
            "select current.catalog_version,current.activated_at_utc::text as activated_at_utc "
            "from readmodel.stock_catalog_current current "
            "join readmodel.stock_catalog_version version "
            "on version.catalog_version=current.catalog_version "
            "where current.singleton=true and version.status='healthy'"
        ).fetchone()
    if row is None:
        return CatalogPublicationReadiness(registry_active=True, current=None)
    return CatalogPublicationReadiness(
        registry_active=True,
        current=CatalogCurrentVersion(str(row["catalog_version"]), str(row["activated_at_utc"])),
    )


def current_stock_catalog_version(
    *,
    connection_factory: Callable[[], Any] = _connect,
) -> CatalogCurrentVersion | None:
    """Return only an integrity-validated current snapshot, never a fallback."""
    with connection_factory() as connection:
        row = connection.execute(
            "select current.catalog_version,current.activated_at_utc::text as activated_at_utc "
            "from readmodel.stock_catalog_current current "
            "join readmodel.stock_catalog_version version "
            "on version.catalog_version=current.catalog_version "
            "where current.singleton=true and version.status='healthy'"
        ).fetchone()
    if row is None:
        return None
    return CatalogCurrentVersion(str(row["catalog_version"]), str(row["activated_at_utc"]))


def _record_candidate_rejection(
    reason: str,
    *,
    connection_factory: Callable[[], Any],
) -> None:
    """Persist a failed refresh attempt without mutating any serving state.

    Validation can fail before source evidence is safe to normalize.  The
    nullable evidence columns intentionally distinguish that case from a
    successfully built candidate that was later rejected by publication.
    """
    with connection_factory() as connection:
        connection.execute(_DDL)
        current = connection.execute(
            "select catalog_version from readmodel.stock_catalog_current where singleton=true"
        ).fetchone()
        previous_version = "" if current is None else str(current["catalog_version"])
        connection.execute(
            "insert into audit.stock_catalog_publication_attempt("
            "catalog_version,content_sha256,authority_input_id,authority_content_sha256,fresh_through,"
            "source_refreshed_at_utc,candidate_count,provisional_count,conflict_count,name_gate_passed,"
            "prior_catalog_version,result,reason) "
            "values('','','','',null,null,0,0,0,false,%s,'rejected',%s)",
            (previous_version, reason),
        )


def refresh_stock_catalog_publication(
    *,
    candidate_connection_factory: Callable[[], Any] | None = None,
    publication_connection_factory: Callable[[], Any] | None = None,
) -> CatalogPublicationResult:
    """Build and publish the latest authority-backed snapshot as one release action."""
    publication_factory = (
        _connect if publication_connection_factory is None else publication_connection_factory
    )
    try:
        candidate = build_current_stock_catalog_candidate(
            connection_factory=_connect
            if candidate_connection_factory is None
            else candidate_connection_factory
        )
    except CatalogCandidateRejected as error:
        _record_candidate_rejection(str(error), connection_factory=publication_factory)
        raise
    from services.market_data_version import market_data_version_for_stock_catalog

    data_version = market_data_version_for_stock_catalog(candidate.version)
    if data_version == "":
        raise CatalogCandidateRejected("current market facts cannot mint a catalog data_version")
    return publish_stock_catalog_candidate(
        candidate,
        data_version=data_version,
        connection_factory=publication_factory,
    )


def resolve_stock_catalog_data_version(
    requested_data_version: str,
    *,
    connection_factory: Callable[[], Any] = _connect,
    current_data_version_factory: Callable[[], str] | None = None,
) -> ResolvedCatalogVersion:
    """Resolve a retained healthy snapshot or return a stable re-pin error."""
    requested = requested_data_version.strip()
    if requested == "":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CATALOG_DATA_VERSION_REQUIRED",
                "message": "目录读取必须携带 /api/health 返回的 data_version",
            },
        )
    with connection_factory() as connection:
        row = connection.execute(
            "select mapping.data_version,mapping.catalog_version,mapping.status,mapping.reason "
            "from readmodel.stock_catalog_data_version mapping "
            "join readmodel.stock_catalog_version version "
            "on version.catalog_version=mapping.catalog_version "
            "where mapping.data_version=%s and mapping.status='healthy' "
            "and mapping.serve_until_utc >= clock_timestamp() and version.status='healthy'",
            (requested,),
        ).fetchone()
        if row is not None:
            return ResolvedCatalogVersion(requested, str(row["catalog_version"]))
        row = connection.execute(
            "select data_version,status,reason from readmodel.stock_catalog_data_version "
            "where data_version=%s",
            (requested,),
        ).fetchone()
    if row is not None and str(row["status"]) == "quarantined":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CATALOG_DATA_VERSION_QUARANTINED",
                "message": "请求的目录版本已隔离，请重新读取 /api/health 并从 offset=0 重新分页",  # noqa: RUF001
                "details": {
                    "requested_version": requested,
                    "reason": str(row.get("reason", "") or ""),
                },
            },
        )
    if _DATA_VERSION.fullmatch(requested):
        if current_data_version_factory is None:
            from services.market_data_version import current_market_data_version

            version_factory = current_market_data_version
        else:
            version_factory = current_data_version_factory
        with connection_factory() as connection:
            current = connection.execute(
                "select current.catalog_version "
                "from readmodel.stock_catalog_current current "
                "join readmodel.stock_catalog_version version "
                "on version.catalog_version=current.catalog_version "
                "where current.singleton=true and version.status='healthy' "
                "for share of current"
            ).fetchone()
            if current is not None and requested == version_factory():
                catalog_version = str(current["catalog_version"])
                connection.execute(
                    "insert into readmodel.stock_catalog_data_version("
                    "data_version,catalog_version,status,serve_until_utc,reason) "
                    "values(%s,%s,'healthy',clock_timestamp() + interval '24 hours','') "
                    "on conflict(data_version) do update set "
                    "catalog_version=excluded.catalog_version,status='healthy',"
                    "serve_until_utc=excluded.serve_until_utc,reason=''",
                    (requested, catalog_version),
                )
                return ResolvedCatalogVersion(requested, catalog_version)
    raise HTTPException(
        status_code=409,
        detail={
            "code": "CATALOG_DATA_VERSION_STALE",
            "message": "请求的目录版本不可服务，请重新读取 /api/health 并从 offset=0 重新分页",  # noqa: RUF001
            "details": {"requested_version": requested},
        },
    )


def read_stock_catalog_page(
    version: ResolvedCatalogVersion,
    *,
    codes: list[str],
    name: str,
    exchange: str,
    list_status: str,
    include_delisted: bool,
    limit: int,
    offset: int,
    connection_factory: Callable[[], Any] = _connect,
) -> list[dict[str, str]]:
    """Read one immutable page; its scope is part of the caller's cache key."""
    clauses = ["catalog_version=%s"]
    params: list[object] = [version.catalog_version]
    if codes:
        clauses.append("code=any(%s::text[])")
        params.append(codes)
    if name:
        clauses.append("name ilike %s")
        params.append(f"%{name}%")
    if exchange:
        clauses.append("exchange=%s")
        params.append(exchange)
    if list_status:
        clauses.append("list_status=%s")
        params.append(list_status.upper())
    elif not include_delisted:
        clauses.append("(delist_date is null or delist_date >= current_date)")
    params.extend((limit, offset))
    query = (
        "select code,name,exchange,market,list_status,coalesce(list_date::text,'') as list_date,"
        "coalesce(delist_date::text,'') as delist_date,industry,listing_board,area "
        "from readmodel.stock_catalog_item where "
        + " and ".join(clauses)
        + " order by code,exchange limit %s offset %s"
    )
    with connection_factory() as connection:
        rows = connection.execute(query, tuple(params)).fetchall()
    return [
        {
            "code": str(row["code"]),
            "name": str(row["name"]),
            "exchange": str(row["exchange"]),
            "market": str(row["market"]),
            "list_status": str(row["list_status"]),
            "list_date": str(row["list_date"] or ""),
            "delist_date": str(row["delist_date"] or ""),
            "industry": str(row["industry"] or ""),
            "listing_board": str(row["listing_board"] or ""),
            "area": str(row["area"] or ""),
        }
        for row in rows
    ]


def _catalog_item_values(candidate: StockCatalogCandidate) -> list[tuple[object, ...]]:
    return [
        (
            candidate.version,
            item.code,
            item.name,
            item.exchange,
            item.market,
            item.list_status,
            item.list_date or None,
            item.delist_date or None,
            item.industry,
            item.listing_board,
            item.area,
        )
        for item in candidate.items
    ]


def publish_stock_catalog_candidate(
    candidate: StockCatalogCandidate,
    *,
    data_version: str,
    known_dirty_data_versions: Iterable[str] = (KNOWN_DIRTY_DATA_VERSION,),
    connection_factory: Callable[[], Any] = _connect,
) -> CatalogPublicationResult:
    """Atomically register a complete candidate and switch only after its audit.

    The advisory lock serializes publishers. The audit and all snapshot rows are
    written before the only current-pointer mutation, so a failure never makes a
    partial or unaudited candidate serviceable.
    """
    normalized_data_version = data_version.strip()
    if normalized_data_version == "":
        raise CatalogCandidateRejected("published market data_version is required")
    dirty_versions = tuple(
        sorted({version.strip() for version in known_dirty_data_versions if version.strip()})
    )
    if normalized_data_version in dirty_versions:
        raise CatalogCandidateRejected("a quarantined data_version cannot become current")
    if candidate.source.conflict_count != 0 or not candidate.items:
        raise CatalogCandidateRejected("only a complete, conflict-free candidate can be published")
    with connection_factory() as connection:
        connection.execute(_DDL)
        connection.execute(
            "select pg_advisory_xact_lock(hashtext('markethub:stock-catalog-publication'))"
        )
        current = connection.execute(
            "select catalog_version from readmodel.stock_catalog_current "
            "where singleton=true for update"
        ).fetchone()
        previous_version = "" if current is None else str(current["catalog_version"])
        connection.execute(
            "insert into readmodel.stock_catalog_version("
            "catalog_version,content_sha256,authority_input_id,authority_content_sha256,"
            "authority_provider,"
            "source_refreshed_at_utc,fresh_through,provisional_count,conflict_count,"
            "row_count,status) "
            "values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'healthy') "
            "on conflict(catalog_version) do nothing",
            (
                candidate.version,
                candidate.content_sha256,
                candidate.source.input_id,
                candidate.source.content_sha256,
                candidate.source.provider,
                candidate.source.source_refreshed_at,
                candidate.source.fresh_through,
                candidate.source.provisional_count,
                candidate.source.conflict_count,
                len(candidate.items),
            ),
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                "insert into readmodel.stock_catalog_item("
                "catalog_version,code,name,exchange,market,list_status,list_date,delist_date,"
                "industry,listing_board,area) "
                "values(%s,%s,%s,%s,%s,%s,%s::date,%s::date,%s,%s,%s) "
                "on conflict(catalog_version,code) do nothing",
                _catalog_item_values(candidate),
            )
        count_row = connection.execute(
            "select count(*)::int as row_count from readmodel.stock_catalog_item "
            "where catalog_version=%s",
            (candidate.version,),
        ).fetchone()
        if count_row is None or int(count_row["row_count"]) != len(candidate.items):
            raise RuntimeError(
                "published catalog item count does not match its immutable candidate"
            )
        publish_stock_name_history_snapshot(connection, candidate.version)
        connection.execute(
            "insert into audit.stock_catalog_publication_attempt("
            "catalog_version,content_sha256,authority_input_id,authority_content_sha256,fresh_through,"
            "source_refreshed_at_utc,candidate_count,provisional_count,conflict_count,name_gate_passed,"
            "prior_catalog_version,result) "
            "values(%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s,'published')",
            (
                candidate.version,
                candidate.content_sha256,
                candidate.source.input_id,
                candidate.source.content_sha256,
                candidate.source.fresh_through,
                candidate.source.source_refreshed_at,
                len(candidate.items),
                candidate.source.provisional_count,
                candidate.source.conflict_count,
                previous_version,
            ),
        )
        connection.execute(
            "insert into readmodel.stock_catalog_data_version("
            "data_version,catalog_version,status,serve_until_utc) "
            "values(%s,%s,'healthy',clock_timestamp() + interval '24 hours') "
            "on conflict(data_version) do update set "
            "catalog_version=excluded.catalog_version,status='healthy',"
            "serve_until_utc=excluded.serve_until_utc,reason=''",
            (normalized_data_version, candidate.version),
        )
        for dirty_version in dirty_versions:
            connection.execute(
                "insert into readmodel.stock_catalog_data_version("
                "data_version,catalog_version,status,serve_until_utc,reason) "
                "values(%s,null,'quarantined','infinity'::timestamptz,"
                "'catalog integrity gate failed') "
                "on conflict(data_version) do update set "
                "catalog_version=null,status='quarantined',"
                "serve_until_utc='infinity'::timestamptz,reason=excluded.reason",
                (dirty_version,),
            )
        connection.execute(
            "insert into readmodel.stock_catalog_current(singleton,catalog_version) "
            "values(true,%s) "
            "on conflict(singleton) do update set "
            "catalog_version=excluded.catalog_version,activated_at_utc=clock_timestamp()",
            (candidate.version,),
        )
    return CatalogPublicationResult(
        candidate.version, previous_version, normalized_data_version, len(candidate.items)
    )
