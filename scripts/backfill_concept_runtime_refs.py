from __future__ import annotations

import argparse
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo


def _load_env_file(path: Path) -> bool:
    if not path.is_file():
        return False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line == "" or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name, value)
    return True


def _bootstrap_env() -> None:
    workspace_root = Path(__file__).resolve().parents[2]
    env_candidates: list[Path] = []
    env_path_text = os.getenv("MARKETHUB_ENV_PATH", "")
    if env_path_text != "":
        env_candidates.append(Path(env_path_text).expanduser())
    runtime_root_text = os.getenv("MARKETHUB_RUNTIME_ROOT", "")
    if runtime_root_text != "":
        env_candidates.append(Path(runtime_root_text).expanduser() / "env" / "markethub.env")
    env_candidates.append(workspace_root / "runtime" / "env" / "markethub.env")
    env_candidates.append(Path("/data/markethub/env/markethub.env"))
    for env_path in env_candidates:
        if _load_env_file(env_path):
            break

    runtime_root = os.getenv("MARKETHUB_RUNTIME_ROOT", "")
    if runtime_root == "":
        default_runtime_root = Path("/data/markethub") if Path("/data/markethub").exists() else workspace_root / "runtime"
        runtime_root = str(default_runtime_root)
        os.environ["MARKETHUB_RUNTIME_ROOT"] = runtime_root
    runtime_path = Path(runtime_root).expanduser()
    os.environ.setdefault("QUOTEMUX_RUNTIME_ROOT", str(runtime_path / "runtime"))
    os.environ.setdefault("QUOTEMUX_CACHE_PAYLOAD_ROOT", str(runtime_path / "cache_payloads"))
    os.environ.setdefault("MARKETHUB_DATA_ROOT", str(runtime_path))


_bootstrap_env()

from quotemux.fact_ref_writes import get_fact_ref_writer
from quotemux.infra.db.client import execute_sql, query_dataframe
from quotemux.runtime import QuoteMux
from quotemux.concept_runtime import (
    CONCEPT_MEMBERS_SOURCE_ORDER,
    _crawler_provider_aliases,
    _dedupe_member_union,
    _rewrite_provider_member_items,
    _timed_source_package_call,
)
from platform_models import BoardMemberItem


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_WINDOW_COUNT = 7
DEFAULT_WORKERS = 4
MAX_CONCEPT_LIMIT = 10000
CONCEPT_FACT_REF_SCHEMA_SQL = (
    """
    create table if not exists ref.concept (
        concept_id text primary key,
        concept_type text not null default 'concept',
        name text not null default '',
        market text not null default 'a_share',
        status text not null default 'active',
        updated_at timestamp without time zone not null default now()
    )
    """,
    """
    create table if not exists ref.concept_stock_membership (
        concept_id text not null,
        stock_market text not null,
        stock_code text not null,
        valid_from date not null,
        valid_to date,
        weight double precision,
        updated_at timestamp without time zone not null default now(),
        primary key (concept_id, stock_market, stock_code, valid_from)
    )
    """,
    "create index if not exists concept_stock_membership_stock_idx on ref.concept_stock_membership (stock_code, valid_from, valid_to)",
    """
    create table if not exists fact.concept_daily_1d (
        concept_id text not null,
        trade_date date not null,
        open double precision,
        high double precision,
        low double precision,
        close double precision,
        volume double precision,
        amount double precision,
        pre_close double precision,
        change double precision,
        pct_chg double precision,
        loaded_at timestamp without time zone not null default now(),
        primary key (concept_id, trade_date)
    )
    """,
    "create index if not exists concept_daily_1d_date_idx on fact.concept_daily_1d (trade_date)",
)


@dataclass(frozen=True)
class ConceptMemberBackfillPayload:
    concept_id: str
    snapshot_trade_date: str
    snapshot_items: tuple[object, ...]


def log(message: str) -> None:
    print(f"[{datetime.now(SHANGHAI_TZ).strftime('%F %T')}] {message}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填概念本地事实/引用表")
    parser.add_argument("--window-count", type=int, default=DEFAULT_WINDOW_COUNT, help="最近开盘交易日窗口数")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="概念成分抓取并发数")
    parser.add_argument("--concept-id", type=str, default="", help="只回填指定概念成分")
    parser.add_argument("--members-only", action="store_true", help="只重建概念成分股表")
    parser.add_argument("--quotes-only", action="store_true", help="只按统一成分股聚合口径重建概念日线")
    return parser.parse_args()


def ensure_concept_fact_ref_tables() -> None:
    for statement in CONCEPT_FACT_REF_SCHEMA_SQL:
        if not execute_sql(statement, ()):
            raise RuntimeError("概念本地事实/引用表建表失败")


def recent_trade_dates(window_count: int) -> tuple[str, ...]:
    frame = query_dataframe(
        """
        with recent_counts as (
            select
                trade_date,
                count(*) filter (where coalesce(amount, 0) > 0) as positive_amount_count
            from fact.stock_daily_1d
            where trade_date >= current_date - interval '45 days'
            group by trade_date
        ),
        benchmark as (
            select coalesce(max(positive_amount_count), 0) as expected_count
            from recent_counts
        )
        select recent_counts.trade_date::text as trade_date
        from recent_counts
        cross join benchmark
        where recent_counts.positive_amount_count >= greatest(1, benchmark.expected_count * 0.9)
        order by recent_counts.trade_date desc
        limit %s
        """,
        (window_count,),
    )
    if frame.empty:
        return ()
    values = [str(row["trade_date"]) for row in frame.to_dict("records") if str(row["trade_date"]) != ""]
    return tuple(reversed(values))


def delete_incomplete_concept_daily_rows(latest_complete_trade_date: str) -> None:
    if not execute_sql(
        "delete from fact.concept_daily_1d where trade_date > %s::date",
        (latest_complete_trade_date,),
    ):
        raise RuntimeError("清理未完成交易日的概念日线失败")


def latest_concept_daily_trade_date() -> str:
    frame = query_dataframe("select coalesce(max(trade_date)::text, '') as trade_date from fact.concept_daily_1d", ())
    if frame.empty:
        return ""
    return str(frame.iloc[0].to_dict().get("trade_date", ""))


def write_items(capability_id: str, items: Sequence[object]) -> int:
    if not items:
        return 0
    writer = get_fact_ref_writer(capability_id)
    if writer is None:
        raise RuntimeError(f"缺少 writer: {capability_id}")
    if not writer(list(items)):
        raise RuntimeError(f"写入失败: {capability_id}")
    return len(items)


def normalize_concept_id(value: str) -> str:
    return value.strip().upper()


def fetch_member_payload(runtime: QuoteMux, concept_id: str, trade_dates: tuple[str, ...]) -> ConceptMemberBackfillPayload:
    for trade_date in reversed(trade_dates):
        items = runtime.concepts.get_members(concept_id, trade_date)
        if items != []:
            return ConceptMemberBackfillPayload(concept_id, trade_date, tuple(items))
    return ConceptMemberBackfillPayload(concept_id, "", ())


def fetch_provider_member_payload(runtime: QuoteMux, concept_id: str, trade_dates: tuple[str, ...]) -> ConceptMemberBackfillPayload:
    for trade_date in reversed(trade_dates):
        aliases = runtime.concepts._concept_aliases(concept_id, trade_date, "concepts.members", CONCEPT_MEMBERS_SOURCE_ORDER)
        items = []
        for alias in _crawler_provider_aliases(aliases):
            try:
                raw_items = _timed_source_package_call(runtime.concepts._settings, "concepts.members", "crawler_provider", "get_concept_members", f"{alias.board_type}:{alias.board_code}", trade_date)
            except Exception:
                continue
            if isinstance(raw_items, list):
                items.extend(_rewrite_provider_member_items([item for item in raw_items if isinstance(item, BoardMemberItem)], alias))
        if items != []:
            return ConceptMemberBackfillPayload(concept_id, trade_date, tuple(_dedupe_member_union(items)))
        for alias in aliases:
            if alias.provider in {"crawler_provider", "derived_core"}:
                continue
            try:
                raw_items = _timed_source_package_call(runtime.concepts._settings, "concepts.members", alias.provider, "get_concept_members", alias.board_code, trade_date)
            except Exception:
                continue
            if isinstance(raw_items, list):
                items.extend(_rewrite_provider_member_items([item for item in raw_items if isinstance(item, BoardMemberItem)], alias))
        if items != []:
            return ConceptMemberBackfillPayload(concept_id, trade_date, tuple(_dedupe_member_union(items)))
    return ConceptMemberBackfillPayload(concept_id, "", ())


def rebuild_daily_snapshots(runtime: QuoteMux, trade_dates: tuple[str, ...], concept_ids: tuple[str, ...]) -> int:
    daily_rows = 0
    for trade_date in trade_dates:
        items = runtime.concepts._get_derived_snapshot_items(list(concept_ids), trade_date)
        daily_rows += write_items("concepts.quotes.daily", items)
        log(f"已按统一成分股聚合口径重建概念日线 {trade_date}: rows={len(items)}")
    return daily_rows


def current_catalog(runtime: QuoteMux) -> tuple[object, ...]:
    items = runtime.concepts.get_catalog("", "a_share", "active", MAX_CONCEPT_LIMIT, 0)
    return tuple(items)


def member_write_items(snapshot_items: tuple[object, ...], valid_from: str) -> list[object]:
    rows: list[object] = []
    for item in snapshot_items:
        if not hasattr(item, "model_copy"):
            continue
        rows.append(item.model_copy(update={"join_date": valid_from}))
    return rows


def members_for_concepts(
    runtime: QuoteMux,
    concept_ids: tuple[str, ...],
    trade_dates: tuple[str, ...],
    workers: int,
    requested_concept_id: str,
) -> int:
    snapshot_rows = 0
    completed = 0
    total = len(concept_ids)
    if total == 0:
        return 0
    default_valid_from = trade_dates[0]
    targeted_mode = normalize_concept_id(requested_concept_id) != ""
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {executor.submit(fetch_member_payload, runtime, concept_id, trade_dates): concept_id for concept_id in concept_ids}
        for future in as_completed(future_map):
            concept_id = future_map[future]
            payload = future.result()
            if payload.snapshot_items != ():
                valid_from = default_valid_from if targeted_mode else (payload.snapshot_trade_date or default_valid_from)
                snapshot_rows += write_items("concepts.members", member_write_items(payload.snapshot_items, valid_from))
            completed += 1
            if completed % 20 == 0 or completed == total:
                log(f"概念成分回填进度 {completed}/{total} snapshot_rows={snapshot_rows} last={concept_id}")
    return snapshot_rows


def delete_membership_rows(concept_ids: tuple[str, ...]) -> None:
    if concept_ids == ():
        return
    if not execute_sql("delete from ref.concept_stock_membership where concept_id = any(%s)", (list(concept_ids),)):
        raise RuntimeError("清理概念成分股行失败")


def delete_orphan_membership_rows() -> None:
    if not execute_sql(
        """
        delete from ref.concept_stock_membership membership_rows
        where not exists (
            select 1
            from ref.concept concept_ref
            where concept_ref.concept_id = membership_rows.concept_id
        )
        """,
        (),
    ):
        raise RuntimeError("清理孤儿概念成分股行失败")


def rebuild_membership_for_concepts(runtime: QuoteMux, concept_ids: tuple[str, ...], trade_dates: tuple[str, ...], workers: int) -> int:
    payloads: list[ConceptMemberBackfillPayload] = []
    completed = 0
    total = len(concept_ids)
    if total == 0:
        return 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {executor.submit(fetch_provider_member_payload, runtime, concept_id, trade_dates): concept_id for concept_id in concept_ids}
        for future in as_completed(future_map):
            concept_id = future_map[future]
            payload = future.result()
            if payload.snapshot_items != ():
                payloads.append(payload)
            completed += 1
            if completed % 20 == 0 or completed == total:
                log(f"概念成分抓取进度 {completed}/{total} payloads={len(payloads)} last={concept_id}")
    delete_membership_rows(concept_ids)
    delete_orphan_membership_rows()
    snapshot_rows = 0
    for payload in payloads:
        snapshot_rows += write_items("concepts.members", member_write_items(payload.snapshot_items, payload.snapshot_trade_date or trade_dates[-1]))
    return snapshot_rows


def ensure_positive_window(window_count: int) -> int:
    if window_count < 1:
        raise ValueError("window_count 必须大于 0")
    return window_count


def main() -> None:
    args = parse_args()
    window_count = ensure_positive_window(int(args.window_count))
    workers = max(1, int(args.workers))
    requested_concept_id = normalize_concept_id(str(args.concept_id))
    runtime = QuoteMux()

    trade_dates = recent_trade_dates(window_count)
    if trade_dates == ():
        raise RuntimeError("最近交易日为空，无法回填概念本地表")

    log(f"开始回填概念本地表: trade_dates={trade_dates[0]}..{trade_dates[-1]} days={len(trade_dates)} workers={workers}")
    if args.members_only:
        latest_trade_date = latest_concept_daily_trade_date()
        if latest_trade_date != "":
            trade_dates = (latest_trade_date,)
    ensure_concept_fact_ref_tables()
    catalog_rows = 0
    daily_rows = 0
    if args.members_only:
        if requested_concept_id == "":
            concept_ids = tuple(item.concept_id for item in current_catalog(runtime) if item.concept_id != "")
        else:
            concept_ids = (requested_concept_id,)
        log(f"概念成分重建目标数 {len(concept_ids)}")
        snapshot_rows = rebuild_membership_for_concepts(runtime, concept_ids, trade_dates, workers)
        log(f"概念成分重建完成 snapshot_rows={snapshot_rows}")
        return
    if args.quotes_only:
        concept_ids = tuple(item.concept_id for item in current_catalog(runtime) if item.concept_id != "")
        daily_rows = rebuild_daily_snapshots(runtime, trade_dates, concept_ids)
        delete_incomplete_concept_daily_rows(trade_dates[-1])
        log(f"概念日线重建完成 daily_rows={daily_rows}")
        return
    if requested_concept_id == "":
        catalog_items = current_catalog(runtime)
        catalog_rows = write_items("concepts.catalog", catalog_items)
        log(f"已回填概念目录 rows={catalog_rows}")
        concept_ids = tuple(item.concept_id for item in catalog_items if item.concept_id != "")
    else:
        concept_ids = (requested_concept_id,)
    log(f"概念成分回填目标数 {len(concept_ids)}")

    snapshot_rows = members_for_concepts(runtime, concept_ids, trade_dates, workers, requested_concept_id)
    if requested_concept_id == "":
        daily_rows = rebuild_daily_snapshots(runtime, trade_dates, concept_ids)
    log(
        "概念本地表回填完成 "
        f"catalog_rows={catalog_rows} daily_rows={daily_rows} snapshot_rows={snapshot_rows}"
    )


if __name__ == "__main__":
    main()
