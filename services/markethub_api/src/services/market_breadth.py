from __future__ import annotations

from datetime import date, datetime, time
import os
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from services.dataset_versions import current_stock_daily_dataset_version


CHINA_TZ = ZoneInfo("Asia/Shanghai")
MARKET_CLOSE = time(15, 0)
UNIVERSE_CONTRACT = "cn-a-share-listed-eligible-v1"

BREADTH_SQL = """
with catalog as materialized (
    select distinct on (code) market,code,listed_date,delisted_date
    from ref.stock
    where code <> '000000'
    order by code,(delisted_date is null) desc,listed_date desc,market
), universe as materialized (
    select market,code
    from catalog
    where (case when market='BJSE' then greatest(listed_date,date '2021-11-15') else listed_date end) <= %(trade_date)s::date
      and (delisted_date is null or %(trade_date)s::date < delisted_date)
      and ((market='SHSE' and left(code,1)='6')
        or (market='SZSE' and left(code,1) in ('0','3'))
        or (market='BJSE' and left(code,1) in ('4','8','9')))
), classified as materialized (
    select u.market,u.code,b.loaded_at,
           (b.code is not null
             and b.open is not null and b.high is not null and b.low is not null
             and b.close is not null and b.pre_close is not null
             and b.volume is not null and b.amount is not null
             and not coalesce(b.is_suspended,false)) as priced,
           (b.code is not null and not coalesce(b.is_suspended,false)
             and (b.open is null or b.high is null or b.low is null or b.close is null
               or b.pre_close is null or b.volume is null or b.amount is null)) as invalid_price,
           case when b.close > b.pre_close then 1 else 0 end as is_up,
           case when b.close < b.pre_close then 1 else 0 end as is_down,
           case when b.close = b.pre_close then 1 else 0 end as is_flat,
           exists (
             select 1 from fact.stock_suspension_history s
             where s.market=u.market and s.code=u.code and s.status='suspended'
               and s.suspend_start_date<=%(trade_date)s::date
               and s.suspend_end_date>=%(trade_date)s::date
           ) as suspension_evidenced
    from universe u
    left join fact.stock_daily_1d b
      on b.market=u.market and b.code=u.code and b.trade_date=%(trade_date)s::date
)
select count(*)::int as universe_count,
       count(*) filter (where priced)::int as priced_count,
       coalesce(sum(is_up) filter (where priced),0)::int as up_count,
       coalesce(sum(is_down) filter (where priced),0)::int as down_count,
       coalesce(sum(is_flat) filter (where priced),0)::int as flat_count,
       count(*) filter (where not priced and suspension_evidenced)::int as suspended_count,
       count(*) filter (where not priced and not suspension_evidenced)::int as unpriced_count,
       count(*) filter (where invalid_price)::int as invalid_price_count,
       min(loaded_at) filter (where priced) as first_loaded_at,
       max(loaded_at) filter (where priced) as last_loaded_at
from classified
"""


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        connect_timeout=10,
        application_name="markethub-market-breadth-read",
        row_factory=dict_row,
    )


def _as_iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def build_market_breadth(
    *,
    trade_date: str,
    now: datetime,
    rows: Mapping[str, object],
    dataset_version: str,
    is_open: bool,
) -> dict[str, object]:
    actual_date = date.fromisoformat(trade_date)
    close_at = datetime.combine(actual_date, MARKET_CLOSE, CHINA_TZ)
    after_close = now.astimezone(CHINA_TZ) >= close_at
    universe_count = int(rows["universe_count"] or 0)
    priced_count = int(rows["priced_count"] or 0)
    suspended_count = int(rows["suspended_count"] or 0)
    unpriced_count = int(rows["unpriced_count"] or 0)
    invalid_price_count = int(rows["invalid_price_count"] or 0)
    accounted = priced_count + suspended_count + unpriced_count
    complete = bool(
        is_open
        and after_close
        and universe_count > 0
        and accounted == universe_count
        and unpriced_count == 0
        and invalid_price_count == 0
        and priced_count == int(rows["up_count"] or 0) + int(rows["down_count"] or 0) + int(rows["flat_count"] or 0)
    )
    status = "complete" if complete else "not_closed" if is_open and not after_close else "not_trading_day" if not is_open else "incomplete"
    coverage = {
        "eligible_count": universe_count,
        "priced_count": priced_count,
        "suspended_count": suspended_count,
        "missing_count": unpriced_count,
        "invalid_price_count": invalid_price_count,
        "accounted_count": accounted,
        "coverage_ratio": (priced_count + suspended_count) / universe_count if universe_count else 0.0,
        "observed_up": int(rows["up_count"] or 0),
        "observed_down": int(rows["down_count"] or 0),
        "observed_flat": int(rows["flat_count"] or 0),
    }
    return {
        "contract": "markethub-cn-a-share-market-breadth-v1",
        "trade_date": trade_date,
        "fact_as_of": close_at.isoformat() if complete else None,
        "status": status,
        "finality": "final" if complete else "not_final",
        "up": int(rows["up_count"] or 0) if complete else None,
        "down": int(rows["down_count"] or 0) if complete else None,
        "flat": int(rows["flat_count"] or 0) if complete else None,
        "unpriced": unpriced_count,
        "suspended": suspended_count,
        "universe_count": universe_count,
        "source": "markethub_local_canonical_daily_snapshot",
        "lineage": {
            "dataset_id": "stock_daily_1d",
            "dataset_version": dataset_version,
            "price_fact": "fact.stock_daily_1d",
            "suspension_fact": "fact.stock_suspension_history",
            "reference_fact": "ref.stock",
            "loaded_at_min": _as_iso(rows.get("first_loaded_at")),
            "loaded_at_max": _as_iso(rows.get("last_loaded_at")),
            "loaded_at_semantics": "ingestion_time_not_fact_time",
        },
        "universe": {
            "contract": UNIVERSE_CONTRACT,
            "markets": ["SHSE", "SZSE", "BJSE"],
            "security_scope": "A shares only; SHSE 6*, SZSE 0*/3*, BJSE 4*/8*/9*",
            "eligibility": "listed_date <= trade_date < delisted_date; BJSE no earlier than 2021-11-15",
            "suspension_policy": "eligible suspended securities remain in universe and require dated suspension evidence",
        },
        "coverage": coverage,
    }


def get_market_breadth(trade_date: str) -> dict[str, object]:
    actual_date = date.fromisoformat(trade_date)
    with _connect() as connection:
        connection.execute("set transaction isolation level repeatable read read only")
        row = connection.execute(BREADTH_SQL, {"trade_date": actual_date}, prepare=False).fetchone()
        calendar = connection.execute(
            "select coalesce(bool_or(is_open),false) as is_open from ref.trade_calendar "
            "where exchange='SHSE' and trade_date=%s",
            (actual_date,),
        ).fetchone()
        connection.rollback()
    if row is None:
        raise RuntimeError("market breadth aggregate returned no row")
    return build_market_breadth(
        trade_date=trade_date,
        now=datetime.now(CHINA_TZ),
        rows=row,
        dataset_version=current_stock_daily_dataset_version(),
        is_open=bool(calendar and calendar["is_open"]),
    )
