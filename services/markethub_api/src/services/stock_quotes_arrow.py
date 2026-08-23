from __future__ import annotations

from dataclasses import dataclass
import json

import pyarrow as pa


ARROW_MEDIA_TYPE = "application/vnd.apache.arrow.stream"
ARROW_SCHEMA_VERSION = "markethub-stock-quotes-arrow-v1"
ARROW_SCHEMA = pa.schema(
    [
        ("code", pa.string()),
        ("trade_time", pa.string()),
        ("freq", pa.string()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("pre_close", pa.float64()),
        ("change", pa.float64()),
        ("pct_chg", pa.float64()),
        ("volume", pa.float64()),
        ("amount", pa.float64()),
        ("adjust", pa.string()),
        ("is_suspended", pa.bool_()),
        ("is_st", pa.bool_()),
    ]
)


@dataclass(frozen=True)
class EncodedStockQuotesArrow:
    content: bytes
    headers: dict[str, str]


def encode(payload: dict[str, object]) -> EncodedStockQuotesArrow:
    items = payload.get("items")
    meta = payload.get("meta")
    if not isinstance(items, list) or not isinstance(meta, dict):
        raise ValueError("StockQuotesQueryResult 结构无效")
    schema = ARROW_SCHEMA.with_metadata(
        {
            b"markethub.meta": json.dumps(meta, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"),
            b"markethub.schema_version": ARROW_SCHEMA_VERSION.encode("ascii"),
            b"markethub.data_version": str(meta.get("data_version", "")).encode("utf-8"),
        }
    )
    table = pa.Table.from_pylist(items, schema=schema)
    output = pa.BufferOutputStream()
    with pa.ipc.new_stream(output, schema) as writer:
        writer.write_table(table, max_chunksize=8_192)
    content = output.getvalue().to_pybytes()
    return EncodedStockQuotesArrow(
        content=content,
        headers={
            "Content-Length": str(len(content)),
            "Vary": "Accept",
            "X-MarketHub-Data-Version": str(meta.get("data_version", "")),
            "X-MarketHub-Returned-Rows": str(meta.get("returned_rows", len(items))),
            "X-MarketHub-Complete": str(bool(meta.get("complete", False))).lower(),
            "X-MarketHub-Arrow-Schema-Version": ARROW_SCHEMA_VERSION,
        },
    )
