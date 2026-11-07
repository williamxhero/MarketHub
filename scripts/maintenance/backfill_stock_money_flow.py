from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


DEFAULT_START_DATE = "20200101"
DEFAULT_BATCH_DAYS = 1
REQUIRED_FIELDS = ("main_inflow", "main_outflow", "net_inflow", "active_buy_amount")


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
    candidates: list[Path] = []
    env_path = os.getenv("MARKETHUB_ENV_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())
    configured_runtime_root = os.getenv("MARKETHUB_RUNTIME_ROOT", "").strip()
    if configured_runtime_root:
        candidates.append(Path(configured_runtime_root).expanduser() / "env" / "markethub.env")
    candidates.append(workspace_root / "runtime" / "env" / "markethub.env")
    for path in candidates:
        if str(path) != "." and _load_env_file(path):
            if os.getenv("MARKETHUB_RUNTIME_ROOT", "").strip():
                break
    runtime_root = os.getenv("MARKETHUB_RUNTIME_ROOT", "").strip()
    if runtime_root == "":
        raise RuntimeError("MARKETHUB_RUNTIME_ROOT is required and must be provided directly or by a loaded env file")
    runtime_path = Path(runtime_root).expanduser()
    if not runtime_path.is_absolute():
        raise RuntimeError("MARKETHUB_RUNTIME_ROOT must be an absolute directory")
    os.environ["MARKETHUB_RUNTIME_ROOT"] = str(runtime_path)
    os.environ.setdefault("MARKETHUB_DATA_ROOT", runtime_root)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按交易日回填个股资金流事实表")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default="")
    parser.add_argument("--batch-days", type=int, default=DEFAULT_BATCH_DAYS)
    parser.add_argument("--state-path", default="")
    return parser.parse_args()


def _state_path(value: str) -> Path:
    if value != "":
        return Path(value).expanduser()
    return Path(os.environ["MARKETHUB_DATA_ROOT"]) / "backfill_state" / "stock_money_flow.json"


def _read_completed(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get("completed_trade_dates", [])
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ValueError(f"断点状态格式无效: {path}")
    return set(values)


def _write_completed(path: Path, values: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"completed_trade_dates": sorted(values)}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _ensure_table() -> None:
    from quotemux.infra.db.client import execute_sql

    statements = (
        """
        create table if not exists fact.stock_money_flow_daily (
            market text not null,
            code text not null,
            trade_date date not null,
            main_inflow double precision,
            main_outflow double precision,
            net_inflow double precision,
            active_buy_amount double precision,
            source text not null,
            loaded_at timestamp with time zone not null default now(),
            primary key (market, code, trade_date)
        )
        """,
        "create index if not exists stock_money_flow_daily_date_idx on fact.stock_money_flow_daily (trade_date)",
        "alter table fact.stock_money_flow_daily add column if not exists active_buy_amount double precision",
    )
    for statement in statements:
        if not execute_sql(statement, ()):
            raise RuntimeError("创建资金流事实表失败")


def _trade_dates(start_date: str, end_date: str) -> tuple[str, ...]:
    from quotemux.infra.db.client import query_dataframe

    frame = query_dataframe(
        """
        select trade_date::text as trade_date
        from fact.stock_daily_1d
        where trade_date >= %s::date
          and trade_date <= coalesce(nullif(%s, '')::date, current_date)
        group by trade_date
        having count(*) filter (where coalesce(amount, 0) > 0) >= 1000
        order by trade_date
        """,
        (start_date, end_date),
    )
    return tuple(str(row["trade_date"]).replace("-", "") for row in frame.to_dict("records"))


def _active_stocks(trade_date: str) -> tuple[tuple[str, str], ...]:
    from quotemux.infra.db.client import query_dataframe

    frame = query_dataframe(
        """
        select market, code
        from fact.stock_daily_1d
        where trade_date = %s::date and coalesce(amount, 0) > 0
        order by market, code
        """,
        (trade_date,),
    )
    return tuple((str(row["market"]), str(row["code"]).zfill(6)) for row in frame.to_dict("records"))


def _fetch_snapshot(trade_date: str) -> list[dict[str, object]]:
    from quotemux.runtime_core.registry import _default_source_instance
    from quotemux.source_packages.instance_context import use_source_instance
    from quotemux.source_packages.registry import get_default_source_package_registry

    source_instance = _default_source_instance("tushare")
    if source_instance is None:
        raise RuntimeError("未找到启用的 Tushare source instance")
    handler = get_default_source_package_registry().get_handler("tushare", "get_stock_money_flow_snapshot")
    with use_source_instance(source_instance):
        items = handler(trade_date, "main")
    if not isinstance(items, list):
        raise RuntimeError(f"资金流返回结构无效: trade_date={trade_date}")
    return [item.model_dump() for item in items]


def _write_rows(trade_date: str, stocks: tuple[tuple[str, str], ...], items: list[dict[str, object]]) -> int:
    from quotemux.infra.db.client import execute_many
    from backfill_contracts import require_complete_rows

    markets = {code: market for market, code in stocks}
    by_code = require_complete_rows(
        trade_date=trade_date,
        expected_codes=markets,
        items=items,
        required_fields=REQUIRED_FIELDS,
        label="资金流",
    )
    expected_codes = set(markets)
    rows = [
        (
            markets[code], code, trade_date,
            by_code[code]["main_inflow"], by_code[code]["main_outflow"],
            by_code[code]["net_inflow"], by_code[code]["active_buy_amount"], "tushare",
        )
        for code in sorted(expected_codes)
    ]
    ok = execute_many(
        """
        insert into fact.stock_money_flow_daily (market, code, trade_date, main_inflow, main_outflow, net_inflow, active_buy_amount, source)
        values (%s, %s, %s::date, %s, %s, %s, %s, %s)
        on conflict (market, code, trade_date) do update set
            main_inflow = excluded.main_inflow,
            main_outflow = excluded.main_outflow,
            net_inflow = excluded.net_inflow,
            active_buy_amount = excluded.active_buy_amount,
            source = excluded.source,
            loaded_at = now()
        """,
        rows,
    )
    if not ok:
        raise RuntimeError(f"资金流写入失败: trade_date={trade_date}")
    return len({row[1] for row in rows})


def _backfill_day(trade_date: str) -> int:
    stocks = _active_stocks(trade_date)
    items = _fetch_snapshot(trade_date)
    written = _write_rows(trade_date, stocks, items)
    if written != len(stocks):
        raise RuntimeError(f"资金流覆盖不足: trade_date={trade_date} expected={len(stocks)} actual={written}")
    return written


def main() -> int:
    _bootstrap_env()
    args = _parse_args()
    if args.batch_days < 1:
        raise ValueError("batch_days 必须大于 0")
    _ensure_table()
    state_path = _state_path(args.state_path)
    completed = _read_completed(state_path)
    candidates = [day for day in _trade_dates(args.start_date, args.end_date) if day not in completed]
    batch = candidates[: args.batch_days]
    if batch == []:
        print("stock_money_flow_backfill=complete")
        return 0
    for trade_date in batch:
        rows = _backfill_day(trade_date)
        completed.add(trade_date)
        _write_completed(state_path, completed)
        print(f"stock_money_flow_backfill=success trade_date={trade_date} rows={rows}", flush=True)
    print(f"stock_money_flow_backfill=batch_complete processed={len(batch)} remaining={len(candidates) - len(batch)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
