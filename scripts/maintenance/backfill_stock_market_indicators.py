from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


DEFAULT_START_DATE = "20200101"
DEFAULT_BATCH_DAYS = 5
MIN_COVERAGE_RATIO = 0.9


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
    code_root = os.getenv("MARKETHUB_CODE_ROOT", "")
    workspace_root = (
        Path(code_root).expanduser().resolve()
        if code_root
        else next(
            (parent for parent in Path(__file__).resolve().parents if (parent / "QuoteMux" / "src").is_dir()),
            Path(__file__).resolve().parents[2],
        )
    )
    quotemux_source_root = str(workspace_root / "QuoteMux" / "src")
    if quotemux_source_root not in sys.path:
        sys.path.insert(0, quotemux_source_root)
    candidates = (
        Path(os.getenv("MARKETHUB_ENV_PATH", "")),
        Path(os.getenv("MARKETHUB_RUNTIME_ROOT", "")) / "env" / "markethub.env",
        workspace_root / "runtime" / "env" / "markethub.env",
        Path("/data/markethub/env/markethub.env"),
    )
    for path in candidates:
        if str(path) != "." and _load_env_file(path):
            break

    runtime_root = os.getenv("MARKETHUB_RUNTIME_ROOT", "")
    if runtime_root == "":
        runtime_root = str(Path("/data/markethub") if Path("/data/markethub").exists() else workspace_root / "runtime")
        os.environ["MARKETHUB_RUNTIME_ROOT"] = runtime_root
    runtime_path = Path(runtime_root).expanduser()
    os.environ.setdefault("QUOTEMUX_RUNTIME_ROOT", str(runtime_path / "runtime"))
    os.environ.setdefault("QUOTEMUX_CACHE_PAYLOAD_ROOT", str(runtime_path / "cache_payloads"))
    os.environ.setdefault("MARKETHUB_DATA_ROOT", str(runtime_path))


@dataclass(frozen=True)
class BackfillState:
    completed_trade_dates: tuple[str, ...]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按交易日回填全市场换手率和流通市值")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="起始交易日，YYYYMMDD")
    parser.add_argument("--end-date", default="", help="结束交易日，YYYYMMDD，默认最新完整交易日")
    parser.add_argument("--batch-days", type=int, default=DEFAULT_BATCH_DAYS, help="本次最多处理的交易日数")
    parser.add_argument("--state-path", default="", help="断点状态文件路径")
    return parser.parse_args()


def _state_path(value: str) -> Path:
    if value != "":
        return Path(value).expanduser()
    return Path(os.environ["MARKETHUB_DATA_ROOT"]) / "backfill_state" / "stock_market_indicators.json"


def _read_state(path: Path) -> BackfillState:
    if not path.is_file():
        return BackfillState(())
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get("completed_trade_dates", [])
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ValueError(f"断点状态格式无效: {path}")
    return BackfillState(tuple(sorted(set(values))))


def _write_state(path: Path, state: BackfillState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"completed_trade_dates": list(state.completed_trade_dates)}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _trade_dates(start_date: str, end_date: str) -> tuple[str, ...]:
    from quotemux.infra.db.client import query_dataframe

    sql = """
        select trade_date::text as trade_date
        from fact.stock_daily_1d
        where trade_date >= %s::date
          and trade_date <= coalesce(nullif(%s, '')::date, current_date)
        group by trade_date
        having count(*) filter (where coalesce(amount, 0) > 0) >= 1000
        order by trade_date asc
    """
    frame = query_dataframe(sql, (start_date, end_date))
    return tuple(str(row["trade_date"]).replace("-", "") for row in frame.to_dict("records"))


def _expected_stock_count(trade_date: str) -> int:
    from quotemux.infra.db.client import query_dataframe

    frame = query_dataframe(
        "select count(*) as row_count from fact.stock_daily_1d where trade_date = %s::date and coalesce(amount, 0) > 0",
        (trade_date,),
    )
    return int(frame.iloc[0].to_dict()["row_count"])


def _markets_by_code(trade_date: str) -> dict[str, str]:
    from quotemux.infra.db.client import query_dataframe

    frame = query_dataframe(
        """
        select market, code
        from fact.stock_daily_1d
        where trade_date = %s::date
          and coalesce(amount, 0) > 0
        order by code
        """,
        (trade_date,),
    )
    return {str(row["code"]).zfill(6): str(row["market"]) for row in frame.to_dict("records")}


def _require_coverage(name: str, trade_date: str, rows: int, expected: int) -> None:
    minimum = max(1, int(expected * MIN_COVERAGE_RATIO))
    if rows < minimum:
        raise RuntimeError(f"{name} 覆盖不足: trade_date={trade_date} expected={expected} actual={rows} minimum={minimum}")


def _fetch_indicators(trade_date: str) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    from quotemux.runtime_core.registry import _default_source_instance
    from quotemux.source_packages.instance_context import use_source_instance
    from quotemux.source_packages.registry import get_default_source_package_registry

    source_instance = _default_source_instance("tushare")
    if source_instance is None:
        raise RuntimeError("未找到启用的 Tushare source instance")
    registry = get_default_source_package_registry()
    basic_handler = registry.get_handler("tushare", "get_stock_daily_basic")
    valuation_handler = registry.get_handler("tushare", "get_stock_daily_valuation")
    market_value_handler = registry.get_handler("tushare", "get_stock_daily_market_value")
    with use_source_instance(source_instance):
        basic_items = basic_handler("", "", trade_date, "", "")
        valuation_items = valuation_handler("", "", trade_date, "", "")
        market_value_items = market_value_handler("", "", trade_date, "", "")
    return (
        [item.model_dump() for item in basic_items],
        [item.model_dump() for item in valuation_items],
        [item.model_dump() for item in market_value_items],
    )


def _ensure_table() -> None:
    from quotemux.infra.db.client import execute_sql

    statements = (
        """
        create table if not exists fact.stock_market_indicators_daily (
            market text not null,
            code text not null,
            trade_date date not null,
            turnover_rate double precision,
            float_mv double precision,
            total_share double precision,
            float_share double precision,
            free_share double precision,
            dv_ratio double precision,
            dv_ttm double precision,
            source text not null,
            loaded_at timestamp with time zone not null default now(),
            primary key (market, code, trade_date)
        )
        """,
        "create index if not exists stock_market_indicators_daily_date_idx on fact.stock_market_indicators_daily (trade_date)",
        "alter table fact.stock_market_indicators_daily add column if not exists float_share double precision",
        "alter table fact.stock_market_indicators_daily add column if not exists free_share double precision",
        "alter table fact.stock_market_indicators_daily add column if not exists dv_ratio double precision",
        "alter table fact.stock_market_indicators_daily add column if not exists dv_ttm double precision",
        "alter table fact.stock_market_indicators_daily add column if not exists total_share double precision",
    )
    for statement in statements:
        if not execute_sql(statement, ()):
            raise RuntimeError("创建流通市值和换手率事实表失败")


def _write_rows(
    trade_date: str,
    markets: dict[str, str],
    basic_items: list[dict[str, object]],
    valuation_items: list[dict[str, object]],
    market_value_items: list[dict[str, object]],
) -> int:
    from quotemux.infra.db.client import execute_many

    turnover_by_code = {str(item.get("code", "")).zfill(6): item.get("turnover_rate") for item in basic_items}
    float_share_by_code = {str(item.get("code", "")).zfill(6): item.get("float_share") for item in basic_items}
    free_share_by_code = {str(item.get("code", "")).zfill(6): item.get("free_share") for item in basic_items}
    total_share_by_code = {str(item.get("code", "")).zfill(6): item.get("total_share") for item in basic_items}
    dividend_yield_by_code = {str(item.get("code", "")).zfill(6): item.get("dv_ratio") for item in valuation_items}
    dividend_yield_ttm_by_code = {str(item.get("code", "")).zfill(6): item.get("dv_ttm") for item in valuation_items}
    float_mv_by_code = {str(item.get("code", "")).zfill(6): item.get("float_mv") for item in market_value_items}
    codes = sorted(set(turnover_by_code) & set(float_mv_by_code) & set(markets))
    rows = [
        (
            markets[code],
            code,
            trade_date,
            turnover_by_code[code],
            float_mv_by_code[code],
            total_share_by_code.get(code),
            float_share_by_code.get(code),
            free_share_by_code.get(code),
            dividend_yield_by_code.get(code),
            dividend_yield_ttm_by_code.get(code),
            "tushare",
        )
        for code in codes
    ]
    if not execute_many(
        """
        insert into fact.stock_market_indicators_daily (market, code, trade_date, turnover_rate, float_mv, total_share, float_share, free_share, dv_ratio, dv_ttm, source)
        values (%s, %s, %s::date, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (market, code, trade_date) do update set
            turnover_rate = excluded.turnover_rate,
            float_mv = excluded.float_mv,
            total_share = excluded.total_share,
            float_share = excluded.float_share,
            free_share = excluded.free_share,
            dv_ratio = excluded.dv_ratio,
            dv_ttm = excluded.dv_ttm,
            source = excluded.source,
            loaded_at = now()
        """,
        rows,
    ):
        raise RuntimeError(f"流通市值和换手率写入失败: trade_date={trade_date}")
    return len(rows)


def _backfill_trade_date(trade_date: str) -> None:
    expected = _expected_stock_count(trade_date)
    markets = _markets_by_code(trade_date)
    if len(markets) != expected:
        raise RuntimeError(f"日线股票池不一致: trade_date={trade_date} expected={expected} codes={len(markets)}")
    basic_items, valuation_items, market_value_items = _fetch_indicators(trade_date)
    written = _write_rows(trade_date, markets, basic_items, valuation_items, market_value_items)
    ttm_rows = sum(1 for item in valuation_items if item.get("dv_ttm") is not None)
    if ttm_rows == 0:
        raise RuntimeError(
            f"TTM 股息率缺少上游数据: trade_date={trade_date}; 不得把空值标记为已回填"
        )
    _require_coverage("stock_market_indicators", trade_date, written, expected)


def main() -> int:
    _bootstrap_env()
    args = _parse_args()
    if args.batch_days < 1:
        raise ValueError("batch_days 必须大于 0")
    _ensure_table()

    state_path = _state_path(args.state_path)
    state = _read_state(state_path)
    completed = set(state.completed_trade_dates)
    candidates = [day for day in _trade_dates(args.start_date, args.end_date) if day not in completed]
    batch = candidates[: args.batch_days]
    if batch == []:
        print("stock_market_indicators_backfill=complete")
        return 0

    for trade_date in batch:
        _backfill_trade_date(trade_date)
        completed.add(trade_date)
        _write_state(state_path, BackfillState(tuple(sorted(completed))))
        print(f"stock_market_indicators_backfill=success trade_date={trade_date}")
    print(f"stock_market_indicators_backfill=batch_complete processed={len(batch)} remaining={len(candidates) - len(batch)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
