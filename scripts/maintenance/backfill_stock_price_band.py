from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import threading


DEFAULT_START_DATE = "20200101"
REQUIRED_FIELDS = ("limit_up", "limit_down")


def _load_env_file(path: Path) -> bool:
    if not path.is_file():
        return False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line != "" and not line.startswith("#") and "=" in line:
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
    source_root = str(workspace_root / "QuoteMux" / "src")
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    environment_paths: list[Path] = []
    env_path = os.getenv("MARKETHUB_ENV_PATH", "").strip()
    if env_path:
        environment_paths.append(Path(env_path).expanduser())
    configured_runtime_root = os.getenv("MARKETHUB_RUNTIME_ROOT", "").strip()
    if configured_runtime_root:
        environment_paths.append(Path(configured_runtime_root).expanduser() / "env" / "markethub.env")
    environment_paths.append(workspace_root / "runtime" / "env" / "markethub.env")
    for path in environment_paths:
        if _load_env_file(path):
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
    os.environ.setdefault("QUOTEMUX_RUNTIME_ROOT", str(runtime_path / "runtime"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按交易日批量回填股票历史涨跌停边界")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default="")
    parser.add_argument("--batch-days", type=int, default=5)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--state-path", default="")
    return parser.parse_args()


def _state_path(value: str) -> Path:
    if value != "":
        return Path(value).expanduser()
    return Path(os.environ["MARKETHUB_DATA_ROOT"]) / "backfill_state" / "stock_price_band.json"


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
        temporary_path = Path(handle.name)
    temporary_path.replace(path)


def _ensure_table() -> None:
    from quotemux.infra.db.client import execute_sql

    statements = (
        """
        create table if not exists fact.stock_price_band_daily (
            market text not null,
            code text not null,
            trade_date date not null,
            upper_limit double precision,
            lower_limit double precision,
            source text not null,
            loaded_at timestamp with time zone not null default now(),
            primary key (market, code, trade_date)
        )
        """,
        "create index if not exists stock_price_band_daily_date_idx on fact.stock_price_band_daily (trade_date)",
    )
    for statement in statements:
        if not execute_sql(statement, ()):
            raise RuntimeError("创建历史涨跌停事实表失败")


def _trade_dates(start_date: str, end_date: str) -> tuple[str, ...]:
    from quotemux.infra.db.client import query_dataframe

    frame = query_dataframe(
        """
        select trade_date::text as trade_date
        from fact.stock_daily_1d
        where trade_date >= %s::date
          and trade_date <= coalesce(nullif(%s, '')::date, current_date)
        group by trade_date
        having count(*) filter (where not coalesce(is_suspended, false)) >= 1000
        order by trade_date
        """,
        (start_date, end_date),
    )
    return tuple(str(row["trade_date"]).replace("-", "") for row in frame.to_dict("records"))


def _market_by_code(trade_date: str) -> dict[str, str]:
    from quotemux.infra.db.client import query_dataframe

    frame = query_dataframe(
        """
        select market, code
        from fact.stock_daily_1d
        where trade_date = %s::date
        order by code
        """,
        (trade_date,),
    )
    return {str(row["code"]).zfill(6): str(row["market"]) for row in frame.to_dict("records")}


_FETCH_LOCK = threading.Lock()
_FETCH_INSTANCE = None
_FETCH_HANDLER = None


def _fetch_snapshot(trade_date: str) -> list[dict[str, object]]:
    global _FETCH_INSTANCE, _FETCH_HANDLER
    from quotemux.runtime_core.registry import _default_source_instance
    from quotemux.source_packages.instance_context import use_source_instance
    from quotemux.source_packages.registry import get_default_source_package_registry

    with _FETCH_LOCK:
        if _FETCH_INSTANCE is None or _FETCH_HANDLER is None:
            _FETCH_INSTANCE = _default_source_instance("tushare")
            if _FETCH_INSTANCE is None:
                raise RuntimeError("Tushare source instance is unavailable")
            _FETCH_INSTANCE = replace(_FETCH_INSTANCE, timeout_seconds=max(_FETCH_INSTANCE.timeout_seconds or 0, 120))
            _FETCH_HANDLER = get_default_source_package_registry().get_handler("tushare", "get_premarket")
        instance = _FETCH_INSTANCE
        handler = _FETCH_HANDLER
    if instance is None:
        raise RuntimeError("未找到启用的 Tushare source instance")
    with use_source_instance(instance):
        items = handler("", trade_date, "", "")
    if not isinstance(items, list):
        raise RuntimeError(f"涨跌停边界返回结构无效: trade_date={trade_date}")
    return [item.model_dump() for item in items]


def _write_rows(trade_date: str, markets: dict[str, str], items: list[dict[str, object]]) -> int:
    from quotemux.infra.db.client import execute_many
    from backfill_contracts import require_complete_rows

    by_code = require_complete_rows(
        trade_date=trade_date,
        expected_codes=markets,
        items=items,
        required_fields=REQUIRED_FIELDS,
        label="历史涨跌停边界",
    )
    rows = [
        (markets[code], code, trade_date, by_code[code]["limit_up"], by_code[code]["limit_down"], "tushare.stk_limit")
        for code in sorted(markets)
    ]
    if not execute_many(
        """
        insert into fact.stock_price_band_daily (market, code, trade_date, upper_limit, lower_limit, source)
        values (%s, %s, %s::date, %s, %s, %s)
        on conflict (market, code, trade_date) do update set
            upper_limit = excluded.upper_limit,
            lower_limit = excluded.lower_limit,
            source = excluded.source,
            loaded_at = now()
        """,
        rows,
    ):
        raise RuntimeError(f"历史涨跌停边界写入失败: trade_date={trade_date}")
    return len({str(row[1]) for row in rows})


def _backfill_day(trade_date: str) -> int:
    markets = _market_by_code(trade_date)
    written = _write_rows(trade_date, markets, _fetch_snapshot(trade_date))
    if written != len(markets):
        raise RuntimeError(f"历史涨跌停边界覆盖不足: trade_date={trade_date} expected={len(markets)} actual={written}")
    return written


def _fetch_batch(trade_dates: list[str], workers: int) -> list[tuple[str, list[dict[str, object]]]]:
    # provider 读取并行，数据库写入仍由主线程按交易日顺序完成。
    with ThreadPoolExecutor(max_workers=workers) as executor:
        snapshots = executor.map(_fetch_snapshot, trade_dates)
        return list(zip(trade_dates, snapshots, strict=True))


def main() -> int:
    _bootstrap_env()
    args = _parse_args()
    if args.batch_days < 1:
        raise ValueError("batch_days 必须大于 0")
    if args.workers < 1:
        raise ValueError("workers must be greater than zero")
    _ensure_table()
    state_path = _state_path(args.state_path)
    completed = _read_completed(state_path)
    candidates = [day for day in _trade_dates(args.start_date, args.end_date) if day not in completed]
    batch = candidates[: args.batch_days]
    if batch == []:
        print("stock_price_band_backfill=complete")
        return 0
    for offset in range(0, len(batch), args.workers):
        trade_dates = batch[offset : offset + args.workers]
        for trade_date, items in _fetch_batch(trade_dates, args.workers):
            markets = _market_by_code(trade_date)
            rows = _write_rows(trade_date, markets, items)
            if rows != len(markets):
                raise RuntimeError(f"coverage incomplete trade_date={trade_date} expected={len(markets)} actual={rows}")
            completed.add(trade_date)
            _write_completed(state_path, completed)
            print(f"stock_price_band_backfill=success trade_date={trade_date} rows={rows}", flush=True)
    print(
        f"stock_price_band_backfill=batch_complete processed={len(batch)} remaining={len(candidates) - len(batch)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
