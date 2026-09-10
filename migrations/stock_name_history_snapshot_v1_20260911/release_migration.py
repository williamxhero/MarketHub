from __future__ import annotations

import argparse
import json
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from services.stock_name_history_publication import (
    DDL,
    publish_stock_name_history_snapshot,
)


def _connect(args: argparse.Namespace) -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        connect_timeout=10,
        row_factory=dict_row,
        application_name="markethub-stock-name-history-migration",
    )


def _set_ownership_and_grants(
    connection: psycopg.Connection[Any], owner_role: str, reader_role: str
) -> None:
    for relation in (
        "stock_name_history_item",
        "stock_name_history_version",
    ):
        connection.execute(
            sql.SQL("alter table readmodel.{} owner to {}").format(
                sql.Identifier(relation), sql.Identifier(owner_role)
            )
        )
        connection.execute(
            sql.SQL("grant select on readmodel.{} to {}").format(
                sql.Identifier(relation), sql.Identifier(reader_role)
            )
        )
    connection.execute(
        sql.SQL("grant usage on schema readmodel to {}").format(sql.Identifier(reader_role))
    )


def _current_catalog(connection: psycopg.Connection[Any]) -> str:
    row = connection.execute(
        "select current.catalog_version "
        "from readmodel.stock_catalog_current current "
        "join readmodel.stock_catalog_version version "
        "on version.catalog_version=current.catalog_version "
        "where current.singleton=true and version.status='healthy' for share of current"
    ).fetchone()
    if row is None:
        raise RuntimeError("healthy current stock catalog unavailable")
    return str(row["catalog_version"])


def apply(args: argparse.Namespace) -> dict[str, object]:
    with _connect(args) as connection:
        connection.execute("select pg_advisory_xact_lock(hashtext('markethub:stock-name-history'))")
        connection.execute(DDL)
        catalog_version = _current_catalog(connection)
        snapshot = publish_stock_name_history_snapshot(connection, catalog_version)
        _set_ownership_and_grants(connection, args.owner_role, args.reader_role)
    return {
        "status": "applied",
        "catalog_version": snapshot.catalog_version,
        "content_sha256": snapshot.content_sha256,
        "total": snapshot.row_count,
        "distinct_code": snapshot.distinct_code_count,
        "catalog_total": snapshot.catalog_row_count,
        "source_total": snapshot.source_row_count,
        "excluded_source_rows": snapshot.excluded_row_count,
        "excluded_source_sha256": snapshot.excluded_content_sha256,
    }


def verify(args: argparse.Namespace) -> dict[str, object]:
    with _connect(args) as connection:
        catalog_version = _current_catalog(connection)
        row = connection.execute(
            "select history.content_sha256,history.row_count,history.distinct_code_count,"
            "history.catalog_row_count,history.source_row_count,history.excluded_row_count,"
            "history.excluded_content_sha256,"
            "(select count(*)::int from readmodel.stock_name_history_item item "
            " where item.catalog_version=history.catalog_version) as actual_rows,"
            "(select count(distinct item.code)::int from readmodel.stock_name_history_item item "
            " where item.catalog_version=history.catalog_version) as actual_codes,"
            "(select count(*)::int from readmodel.stock_name_history_item item "
            " left join readmodel.stock_catalog_item catalog "
            " on catalog.catalog_version=item.catalog_version and catalog.code=item.code "
            " where item.catalog_version=history.catalog_version and catalog.code is null) "
            "as outside_catalog "
            "from readmodel.stock_name_history_version history "
            "where history.catalog_version=%s and history.status='healthy'",
            (catalog_version,),
        ).fetchone()
    if row is None:
        raise RuntimeError("current stock name history snapshot unavailable")
    if (
        int(row["row_count"]) != int(row["actual_rows"])
        or int(row["distinct_code_count"]) != int(row["actual_codes"])
        or int(row["outside_catalog"]) != 0
        or int(row["source_row_count"])
        != int(row["actual_rows"]) + int(row["excluded_row_count"])
    ):
        raise RuntimeError("stock name history snapshot verification failed")
    return {
        "status": "verified",
        "catalog_version": catalog_version,
        "content_sha256": str(row["content_sha256"]),
        "total": int(row["row_count"]),
        "distinct_code": int(row["distinct_code_count"]),
        "catalog_total": int(row["catalog_row_count"]),
        "outside_catalog": int(row["outside_catalog"]),
        "excluded_source_rows": int(row["excluded_row_count"]),
        "excluded_source_sha256": str(row["excluded_content_sha256"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("apply", "verify"))
    parser.add_argument("--db-host", default="/var/run/postgresql")
    parser.add_argument("--db-port", type=int, required=True)
    parser.add_argument("--db-name", required=True)
    parser.add_argument("--owner-role", required=True)
    parser.add_argument("--reader-role", required=True)
    args = parser.parse_args()
    result = apply(args) if args.action == "apply" else verify(args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
