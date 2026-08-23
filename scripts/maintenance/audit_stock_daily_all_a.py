from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


AUDIT_SQL = """
with catalog as materialized (
    select distinct on (code) market,code,listed_date,delisted_date
    from ref.stock
    where code <> '000000'
    order by code,(delisted_date is null) desc,listed_date desc,market
), universe as materialized (
    select market,code,
           case when market='BJSE' then greatest(listed_date,date '2021-11-15') else listed_date end as listed_date,
           delisted_date
    from catalog
    where (case when market='BJSE' then greatest(listed_date,date '2021-11-15') else listed_date end) <= %(end)s::date
      and (delisted_date is null or delisted_date > %(start)s::date)
      and (case when market='BJSE' then greatest(listed_date,date '2021-11-15') else listed_date end) < coalesce(delisted_date,date 'infinity')
      and ((market='SHSE' and left(code,1)='6')
        or (market='SZSE' and left(code,1) in ('0','3'))
        or (market='BJSE' and left(code,1) in ('4','8','9')))
), open_dates as materialized (
    select trade_date
    from ref.trade_calendar
    where exchange='SHSE' and is_open
      and trade_date between %(start)s::date and %(end)s::date
), expected as materialized (
    select u.market,u.code,u.listed_date,u.delisted_date,d.trade_date
    from universe u cross join open_dates d
    where (u.listed_date is null or u.listed_date<=d.trade_date)
      and (u.delisted_date is null or d.trade_date<u.delisted_date)
      and not exists (
        select 1 from fact.stock_suspension_history s
        where s.market=u.market and s.code=u.code and s.status='suspended'
          and s.suspend_start_date<=d.trade_date and s.suspend_end_date>=d.trade_date
      )
)
select e.market,e.code,e.trade_date,e.listed_date,e.delisted_date,
       case
         when b.code is null then 'absent'
         when coalesce(b.is_suspended,false) then 'stored_suspended'
         else 'null_required_field'
       end as gap_kind,
       b.open,b.high,b.low,b.close,b.volume,b.amount,b.is_suspended,b.loaded_at
from expected e
left join fact.stock_daily_1d b
  on b.market=e.market and b.code=e.code and b.trade_date=e.trade_date
where b.code is null or coalesce(b.is_suspended,false)
   or b.open is null or b.high is null or b.low is null or b.close is null or b.volume is null
order by e.trade_date,e.market,e.code
"""


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["MARKETHUB_DB_HOST"],
        port=int(os.environ["MARKETHUB_DB_PORT"]),
        dbname=os.environ["MARKETHUB_DB_NAME"],
        user=os.environ["MARKETHUB_DB_USER"],
        password=os.environ["MARKETHUB_DB_PASSWORD"],
        connect_timeout=10,
        application_name="markethub-stock-daily-all-a-audit",
        row_factory=dict_row,
    )


def audit(start: date, end: date) -> dict[str, object]:
    with _connect() as connection, connection.cursor() as cursor:
        connection.execute("set transaction isolation level repeatable read read only")
        cursor.execute(AUDIT_SQL, {"start": start, "end": end})
        gaps = [dict(row) for row in cursor.fetchall()]
        cursor.execute("select baseline_id,generation from audit.market_data_version_state where singleton=true")
        version_state = dict(cursor.fetchone())
        connection.rollback()
    codes = sorted({str(row["code"]) for row in gaps})
    kinds: dict[str, int] = {}
    for row in gaps:
        kind = str(row["gap_kind"])
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        "contract": "markethub-stock-daily-all-a-audit-v1",
        "scope": "exhaustive",
        "start_date": start,
        "end_date": end,
        "audited_at_utc": datetime.now(timezone.utc),
        "market_version_state": version_state,
        "gap_rows": len(gaps),
        "gap_instruments": len(codes),
        "gap_kinds": kinds,
        "codes": codes,
        "gaps": gaps,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit exact all-A stock daily coverage gaps")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not be after --end")
    payload = audit(args.start, args.end)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n").encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, args.output)
    print(json.dumps({"output": str(args.output), "sha256": hashlib.sha256(encoded).hexdigest(), "gap_rows": payload["gap_rows"], "gap_instruments": payload["gap_instruments"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
