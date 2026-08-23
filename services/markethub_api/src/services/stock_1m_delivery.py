from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as day_time, timedelta
import io
import json
import time
from typing import Iterator

from fastapi import HTTPException
import pyarrow as pa

from quotemux import QuoteMuxPublicReader
from quotemux.infra.db.read_client import QueryBatch, ReadOnlyClient
from routers.stock_quote_models import StockQuotesQueryPayload
from services.dataset_versions import require_dataset_version
from services.request_timing import record_stage_ms
from services.stock_quotes_arrow import ARROW_MEDIA_TYPE, ARROW_SCHEMA, ARROW_SCHEMA_VERSION


STOCK_1M_DATASET_ID = "stock_bar_1m"
ARROW_RECORD_BATCH_ROWS = 8_192


def _read_stage(stage: str, duration_seconds: float) -> None:
    mapped = {"pool_wait": "db_pool", "sql_execute": "sql", "sql_fetch": "sql"}.get(stage)
    if mapped is not None:
        record_stage_ms(mapped, duration_seconds * 1_000)


_READER = QuoteMuxPublicReader(stage_callback=_read_stage)
_READ_CLIENT = ReadOnlyClient(stage_callback=_read_stage)


@dataclass(frozen=True)
class PreparedStock1mArrow:
    body: Iterator[bytes]
    headers: dict[str, str]


class _ArrowChunkSink(io.RawIOBase):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, value: bytes | bytearray | memoryview) -> int:
        data = bytes(value)
        self._chunks.append(data)
        return len(data)

    def drain(self) -> list[bytes]:
        chunks, self._chunks = self._chunks, []
        return chunks


def _bounds(payload: StockQuotesQueryPayload) -> tuple[datetime, datetime]:
    if payload.trade_date:
        trade_date = date.fromisoformat(payload.trade_date)
        return datetime.combine(trade_date, day_time(9, 31)), datetime.combine(trade_date, day_time(15, 0))
    start_text = payload.start_time or payload.start_date
    end_text = payload.end_time or payload.end_date
    if not start_text or not end_text:
        raise HTTPException(
            status_code=422,
            detail={"code": "STOCK_1M_BOUNDS_REQUIRED", "message": "1m 批查询必须指定 trade_date 或完整起止范围"},
        )
    start = datetime.fromisoformat(start_text)
    end = datetime.fromisoformat(end_text)
    if len(start_text) == 10:
        start = datetime.combine(start.date(), day_time(9, 31))
    if len(end_text) == 10:
        end = datetime.combine(end.date(), day_time(15, 0))
    if start > end:
        raise HTTPException(status_code=422, detail={"code": "STOCK_1M_BOUNDS_INVALID", "message": "起始时间不能晚于结束时间"})
    return start, end


def _expected_minutes(start: datetime, end: datetime) -> tuple[datetime, ...]:
    rows: list[datetime] = []
    current = start.date()
    while current <= end.date():
        for session_start, session_end in ((day_time(9, 31), day_time(11, 30)), (day_time(13, 1), day_time(15, 0))):
            value = datetime.combine(current, session_start)
            last = datetime.combine(current, session_end)
            while value <= last:
                if start <= value <= end:
                    rows.append(value)
                value += timedelta(minutes=1)
        current += timedelta(days=1)
    return tuple(rows)


def _is_open_trade_date(trade_date: date) -> bool:
    batch = _READ_CLIENT.query_batch(
        "select exists(select 1 from ref.trade_calendar where exchange='SHSE' and trade_date=%s and is_open) as is_open",
        (trade_date,),
        stage="stock_1m_calendar",
    )
    return bool(batch.rows and batch.rows[0][0])


def _coverage_map(batch: QueryBatch) -> dict[str, dict[str, object]]:
    return {str(row[0]).strip(): dict(zip(batch.columns, row, strict=True)) for row in batch.rows}


def _validate_coverage(
    payload: StockQuotesQueryPayload,
    coverage: QueryBatch,
    start: datetime,
    end: datetime,
    dataset_version: str,
) -> tuple[list[dict[str, object]], int]:
    # The calendar/read-model path removes closed days before production. Until
    # then a single-day request is the strict contract used by the hot API.
    if start.date() != end.date():
        raise HTTPException(
            status_code=503,
            detail={"code": "READ_MODEL_NOT_READY", "message": "多交易日 1m coverage read model 尚未就绪"},
        )
    is_open = _is_open_trade_date(start.date())
    expected = len(_expected_minutes(start, end)) if is_open else 0
    # Coverage is maintained per complete trading day. A complete day proves any
    # requested sub-window is available; the stream row-count check below still
    # verifies that the requested slice itself did not change after validation.
    expected_daily = 240 if is_open else 0
    by_code = _coverage_map(coverage)
    summaries: list[dict[str, object]] = []
    total_rows = 0
    gaps: list[dict[str, object]] = []
    for code in sorted(set(payload.codes)):
        actual = by_code.get(code, {})
        daily_actual_count = int(actual.get("row_count", 0) or 0)
        complete = daily_actual_count == expected_daily
        actual_count = expected if complete else min(expected, daily_actual_count)
        total_rows += actual_count
        summary = {
            "code": code,
            "row_count": actual_count,
            "expected_bar_count": expected,
            "actual_bar_count": actual_count,
            "missing_count": max(0, expected - actual_count),
            "first_trade_time": str(actual.get("first_trade_time", "") or ""),
            "last_trade_time": str(actual.get("last_trade_time", "") or ""),
            "complete": complete,
            "truncated": False,
            "missing_trade_dates": [] if complete else [start.date().isoformat()],
            "missing_trade_times": [],
        }
        summaries.append(summary)
        if not complete and len(gaps) < 100:
            gaps.append({"code": code, "expected_rows": expected_daily, "actual_rows": daily_actual_count})
    if gaps:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DATA_INCOMPLETE",
                "message": "股票 1m 数据不完整；普通查询不会触发 provider 或写库",
                "details": {
                    "dataset_id": STOCK_1M_DATASET_ID,
                    "dataset_version": dataset_version,
                    "expected_rows": expected * len(summaries),
                    "actual_rows": total_rows,
                    "missing_rows": sum(int(item["missing_count"]) for item in summaries),
                    "gap_sample": gaps,
                    "repair_endpoint": "/api/admin/data-repairs",
                    "repair_template": {
                        "dataset_id": STOCK_1M_DATASET_ID,
                        "dataset_version": dataset_version,
                        "scope": {"codes": payload.codes, "start_time": start.isoformat(sep=" "), "end_time": end.isoformat(sep=" ")},
                    },
                },
            },
        )
    return summaries, total_rows


def _record_batch(batch: QueryBatch, limit: int | None, emitted: int) -> pa.RecordBatch | None:
    rows = batch.rows
    if limit is not None:
        rows = rows[: max(0, limit - emitted)]
    if not rows:
        return None
    columns = {name: index for index, name in enumerate(batch.columns)}
    trade_times = [value.isoformat(sep=" ") if hasattr(value, "isoformat") else str(value) for value in (row[columns["trade_time"]] for row in rows)]
    count = len(rows)
    arrays = [
        pa.array([str(row[columns["code"]]).strip() for row in rows], type=pa.string()),
        pa.array(trade_times, type=pa.string()),
        pa.array(["1m"] * count, type=pa.string()),
    ]
    for name in ("open", "high", "low", "close"):
        arrays.append(pa.array([row[columns[name]] for row in rows], type=pa.float64()))
    arrays.extend(
        [
            pa.nulls(count, type=pa.float64()),
            pa.nulls(count, type=pa.float64()),
            pa.nulls(count, type=pa.float64()),
            pa.array([row[columns["volume"]] for row in rows], type=pa.float64()),
            pa.array([row[columns["amount"]] for row in rows], type=pa.float64()),
            pa.array(["none"] * count, type=pa.string()),
            pa.array([False] * count, type=pa.bool_()),
            pa.array([False] * count, type=pa.bool_()),
        ]
    )
    return pa.RecordBatch.from_arrays(arrays, schema=ARROW_SCHEMA)


def _json_rows(batch: QueryBatch, limit: int | None, emitted: int) -> list[dict[str, object]]:
    rows = batch.rows
    if limit is not None:
        rows = rows[: max(0, limit - emitted)]
    columns = {name: index for index, name in enumerate(batch.columns)}
    result: list[dict[str, object]] = []
    for row in rows:
        trade_time = row[columns["trade_time"]]
        result.append(
            {
                "code": str(row[columns["code"]]).strip(),
                "trade_time": trade_time.isoformat(sep=" ") if hasattr(trade_time, "isoformat") else str(trade_time),
                "freq": "1m",
                "open": row[columns["open"]], "high": row[columns["high"]], "low": row[columns["low"]], "close": row[columns["close"]],
                "pre_close": None, "change": None, "pct_chg": None,
                "volume": row[columns["volume"]], "amount": row[columns["amount"]],
                "adjust": "none", "is_suspended": False, "is_st": False,
            }
        )
    return result


def _body(stream, schema: pa.Schema, expected_rows: int, limit: int | None) -> Iterator[bytes]:
    sink = _ArrowChunkSink()
    output = pa.PythonFile(sink, mode="w")
    writer = pa.ipc.new_stream(output, schema)
    emitted = 0
    try:
        for chunk in sink.drain():
            yield chunk
        for batch in stream:
            record = _record_batch(batch, limit, emitted)
            if record is None:
                break
            writer.write_batch(record)
            emitted += record.num_rows
            for chunk in sink.drain():
                yield chunk
            if limit is not None and emitted >= limit:
                break
        expected_delivery = min(expected_rows, limit) if limit is not None else expected_rows
        if emitted != expected_delivery:
            raise RuntimeError(f"stock 1m stream row count changed: expected={expected_delivery} actual={emitted}")
        writer.close()
        for chunk in sink.drain():
            yield chunk
    finally:
        stream.__exit__(None, None, None)


def prepare_arrow(payload: StockQuotesQueryPayload) -> PreparedStock1mArrow:
    if payload.freq != "1m" or payload.count is not None:
        raise ValueError("stock 1m Arrow fast path requires freq=1m and count omitted")
    started = time.perf_counter()
    start, end = _bounds(payload)
    dataset_version = require_dataset_version(STOCK_1M_DATASET_ID, payload.dataset_version, payload.data_version)
    stream = _READER.open_stock_1m_batch_stream(payload.codes, start, end, batch_size=ARROW_RECORD_BATCH_ROWS)
    stream.__enter__()
    try:
        summaries, total_rows = _validate_coverage(payload, stream.coverage, start, end, dataset_version)
        require_dataset_version(STOCK_1M_DATASET_ID, dataset_version)
        returned_rows = min(total_rows, payload.limit) if payload.limit is not None else total_rows
        meta = {
            "data_version": payload.data_version,
            "dataset_version": dataset_version,
            "total_rows": total_rows,
            "returned_rows": returned_rows,
            "complete": payload.limit is None or payload.limit >= total_rows,
            "truncated": returned_rows < total_rows,
            "codes": summaries,
        }
        schema = ARROW_SCHEMA.with_metadata(
            {
                b"markethub.meta": json.dumps(meta, ensure_ascii=False, separators=(",", ":"), default=str).encode(),
                b"markethub.schema_version": ARROW_SCHEMA_VERSION.encode(),
                b"markethub.dataset_version": dataset_version.encode(),
            }
        )
        record_stage_ms("coverage", (time.perf_counter() - started) * 1_000)
        return PreparedStock1mArrow(
            body=_body(stream, schema, total_rows, payload.limit),
            headers={
                "Vary": "Accept, Accept-Encoding",
                "Cache-Control": "private,max-age=0,must-revalidate",
                "X-MarketHub-Dataset-Version": dataset_version,
                "X-MarketHub-Returned-Rows": str(returned_rows),
                "X-MarketHub-Complete": str(bool(meta["complete"])).lower(),
                "X-MarketHub-Arrow-Schema-Version": ARROW_SCHEMA_VERSION,
            },
        )
    except BaseException:
        stream.__exit__(*__import__("sys").exc_info())
        raise


def build_json(payload: StockQuotesQueryPayload) -> dict[str, object]:
    if payload.freq != "1m" or payload.count is not None:
        raise ValueError("stock 1m JSON fast path requires freq=1m and count omitted")
    start, end = _bounds(payload)
    dataset_version = require_dataset_version(STOCK_1M_DATASET_ID, payload.dataset_version, payload.data_version)
    stream = _READER.open_stock_1m_batch_stream(payload.codes, start, end, batch_size=ARROW_RECORD_BATCH_ROWS)
    with stream:
        summaries, total_rows = _validate_coverage(payload, stream.coverage, start, end, dataset_version)
        items: list[dict[str, object]] = []
        for batch in stream:
            items.extend(_json_rows(batch, payload.limit, len(items)))
            if payload.limit is not None and len(items) >= payload.limit:
                break
    require_dataset_version(STOCK_1M_DATASET_ID, dataset_version)
    return {
        "items": items,
        "meta": {
            "data_version": payload.data_version,
            "dataset_version": dataset_version,
            "total_rows": total_rows,
            "returned_rows": len(items),
            "complete": len(items) == total_rows,
            "truncated": len(items) < total_rows,
            "codes": summaries,
        },
    }
