from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

DDL = """
create schema if not exists readmodel;
create table if not exists readmodel.stock_name_history_version (
    catalog_version text primary key
        references readmodel.stock_catalog_version(catalog_version),
    content_sha256 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    row_count integer not null check (row_count >= 0),
    distinct_code_count integer not null check (distinct_code_count >= 0),
    catalog_row_count integer not null check (catalog_row_count > 0),
    source_row_count integer not null check (source_row_count >= 0),
    excluded_row_count integer not null check (excluded_row_count >= 0),
    excluded_content_sha256 text not null check (excluded_content_sha256 ~ '^[0-9a-f]{64}$'),
    status text not null check (status in ('healthy','quarantined')),
    created_at_utc timestamptz not null default clock_timestamp(),
    check (distinct_code_count <= row_count),
    check (distinct_code_count <= catalog_row_count),
    check (source_row_count = row_count + excluded_row_count)
);
create table if not exists readmodel.stock_name_history_item (
    catalog_version text not null
        references readmodel.stock_name_history_version(catalog_version),
    market text not null,
    code text not null check (code ~ '^[0-9]{6}$'),
    name text not null check (btrim(name) <> ''),
    start_date date not null,
    end_date date,
    ann_date date,
    primary key (catalog_version,market,code,name,start_date),
    foreign key (catalog_version,code)
        references readmodel.stock_catalog_item(catalog_version,code),
    check (end_date is null or end_date >= start_date)
);
create index if not exists stock_name_history_item_page_idx
    on readmodel.stock_name_history_item(
        catalog_version,code,start_date,end_date,name,market
    );
"""


class StockNameHistoryPublicationRejected(RuntimeError):
    pass


def _fetch_rows(result: Any) -> list[Any]:
    fetchall = getattr(result, "fetchall", None)
    if callable(fetchall):
        return list(fetchall())
    first = result.fetchone()
    return [] if first is None else [first]


@dataclass(frozen=True)
class StockNameHistorySnapshot:
    catalog_version: str
    content_sha256: str
    row_count: int
    distinct_code_count: int
    catalog_row_count: int
    source_row_count: int
    excluded_row_count: int
    excluded_content_sha256: str
    rows: tuple[tuple[str, str, str, date, date | None, date | None], ...]


def _date(value: object, field: str, *, optional: bool = False) -> date | None:
    if value is None or value == "":
        if optional:
            return None
        raise StockNameHistoryPublicationRejected(f"{field} is required")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise StockNameHistoryPublicationRejected(f"invalid {field}") from exc


def build_stock_name_history_snapshot(
    catalog_version: str,
    rows: Iterable[dict[str, object]],
    *,
    source_row_count: int,
    catalog_row_count: int,
    excluded_rows: Iterable[dict[str, object]] = (),
) -> StockNameHistorySnapshot:
    normalized: list[tuple[str, str, str, date, date | None, date | None]] = []
    for row in rows:
        market = str(row.get("market", "") or "").strip()
        code = str(row.get("code", "") or "").strip()
        name = str(row.get("name", "") or "").strip()
        start_date = _date(row.get("start_date"), "start_date")
        end_date = _date(row.get("end_date"), "end_date", optional=True)
        ann_date = _date(row.get("ann_date"), "ann_date", optional=True)
        if not market or len(code) != 6 or not code.isascii() or not code.isdigit() or not name:
            raise StockNameHistoryPublicationRejected("invalid stock name history identity")
        assert start_date is not None
        if end_date is not None and end_date < start_date:
            raise StockNameHistoryPublicationRejected("end_date precedes start_date")
        normalized.append((market, code, name, start_date, end_date, ann_date))
    normalized.sort(
        key=lambda row: (
            row[1],
            row[3],
            date.max if row[4] is None else row[4],
            row[2],
            row[0],
        )
    )
    excluded = [
        {
            "market": str(row.get("market", "") or ""),
            "code": str(row.get("code", "") or ""),
            "name": str(row.get("name", "") or ""),
            "start_date": str(row.get("start_date", "") or ""),
            "end_date": str(row.get("end_date", "") or ""),
            "ann_date": str(row.get("ann_date", "") or ""),
        }
        for row in excluded_rows
    ]
    excluded.sort(
        key=lambda row: (
            row["code"],
            row["start_date"],
            row["end_date"],
            row["name"],
            row["market"],
        )
    )
    if source_row_count != len(normalized) + len(excluded):
        raise StockNameHistoryPublicationRejected(
            "stock name history source accounting mismatch: "
            f"source={source_row_count} accepted={len(normalized)} excluded={len(excluded)}"
        )
    canonical = [
        {
            "market": row[0],
            "code": row[1],
            "name": row[2],
            "start_date": row[3].isoformat(),
            "end_date": "" if row[4] is None else row[4].isoformat(),
            "ann_date": "" if row[5] is None else row[5].isoformat(),
        }
        for row in normalized
    ]
    encoded = json.dumps(
        canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    excluded_encoded = json.dumps(
        excluded, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return StockNameHistorySnapshot(
        catalog_version=catalog_version,
        content_sha256=hashlib.sha256(encoded).hexdigest(),
        row_count=len(normalized),
        distinct_code_count=len({row[1] for row in normalized}),
        catalog_row_count=catalog_row_count,
        source_row_count=source_row_count,
        excluded_row_count=len(excluded),
        excluded_content_sha256=hashlib.sha256(excluded_encoded).hexdigest(),
        rows=tuple(normalized),
    )


def publish_stock_name_history_snapshot(
    connection: Any, catalog_version: str
) -> StockNameHistorySnapshot:
    existing = connection.execute(
        "select content_sha256,row_count,distinct_code_count,catalog_row_count,source_row_count,"
        "excluded_row_count,excluded_content_sha256,status "
        "from readmodel.stock_name_history_version where catalog_version=%s",
        (catalog_version,),
    ).fetchone()
    catalog_count_row = connection.execute(
        "select count(*)::int as row_count from readmodel.stock_catalog_item "
        "where catalog_version=%s",
        (catalog_version,),
    ).fetchone()
    source_rows = _fetch_rows(
        connection.execute(
            "select history.market,history.code,history.name,history.valid_from as start_date,"
            "history.valid_to as end_date,history.ann_date,"
            "catalog.code is not null as in_catalog "
            "from ref.stock_name_history history "
            "left join readmodel.stock_catalog_item catalog "
            "on catalog.catalog_version=%s and catalog.code=history.code "
            "order by history.code,history.valid_from,history.valid_to nulls last,"
            "history.name,history.market",
            (catalog_version,),
        )
    )
    if catalog_count_row is None:
        raise StockNameHistoryPublicationRejected("name history publication counts unavailable")
    accepted_rows = [row for row in source_rows if bool(row["in_catalog"])]
    excluded_rows = [row for row in source_rows if not bool(row["in_catalog"])]
    snapshot = build_stock_name_history_snapshot(
        catalog_version,
        accepted_rows,
        source_row_count=len(source_rows),
        catalog_row_count=int(catalog_count_row["row_count"]),
        excluded_rows=excluded_rows,
    )
    if existing is not None:
        if str(existing["status"]) != "healthy":
            raise StockNameHistoryPublicationRejected(
                "existing stock name history snapshot is not healthy"
            )
        expected = (
            snapshot.content_sha256,
            snapshot.row_count,
            snapshot.distinct_code_count,
            snapshot.catalog_row_count,
            snapshot.source_row_count,
            snapshot.excluded_row_count,
            snapshot.excluded_content_sha256,
        )
        actual = (
            str(existing["content_sha256"]),
            int(existing["row_count"]),
            int(existing["distinct_code_count"]),
            int(existing["catalog_row_count"]),
            int(existing["source_row_count"]),
            int(existing["excluded_row_count"]),
            str(existing["excluded_content_sha256"]),
        )
        if actual != expected:
            raise StockNameHistoryPublicationRejected(
                "immutable stock name history snapshot conflicts with source"
            )
        published_rows = _fetch_rows(
            connection.execute(
                "select market,code,name,start_date,end_date,ann_date "
                "from readmodel.stock_name_history_item where catalog_version=%s "
                "order by code,start_date,end_date nulls last,name,market",
                (catalog_version,),
            )
        )
        actual_rows = tuple(
            (
                str(row["market"]),
                str(row["code"]),
                str(row["name"]),
                row["start_date"],
                row["end_date"],
                row["ann_date"],
            )
            for row in published_rows
        )
        if actual_rows != snapshot.rows:
            raise StockNameHistoryPublicationRejected(
                "immutable stock name history rows conflict with source"
            )
        return snapshot
    connection.execute(
        "insert into readmodel.stock_name_history_version("
        "catalog_version,content_sha256,row_count,distinct_code_count,catalog_row_count,"
        "source_row_count,excluded_row_count,excluded_content_sha256,status) "
        "values(%s,%s,%s,%s,%s,%s,%s,%s,'healthy')",
        (
            snapshot.catalog_version,
            snapshot.content_sha256,
            snapshot.row_count,
            snapshot.distinct_code_count,
            snapshot.catalog_row_count,
            snapshot.source_row_count,
            snapshot.excluded_row_count,
            snapshot.excluded_content_sha256,
        ),
    )
    if snapshot.rows:
        with connection.cursor() as cursor:
            cursor.executemany(
                "insert into readmodel.stock_name_history_item("
                "catalog_version,market,code,name,start_date,end_date,ann_date) "
                "values(%s,%s,%s,%s,%s,%s,%s)",
                [
                    (snapshot.catalog_version, *row)
                    for row in snapshot.rows
                ],
            )
    return snapshot
