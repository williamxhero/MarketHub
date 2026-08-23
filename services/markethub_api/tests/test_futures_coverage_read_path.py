from __future__ import annotations

from pathlib import Path
import sys


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from quotemux.infra.db.read_client import QueryBatch
from services import futures


def test_futures_coverage_uses_strict_public_reader(monkeypatch) -> None:
    batch = QueryBatch(
        ("product_code", "exchange", "series_type", "row_count", "first_bar_time", "last_bar_time"),
        (("IF", "CFFEX", "main_continuous", 10, "2026-08-20 09:31:00", "2026-08-20 15:00:00"),),
    )
    monkeypatch.setattr(futures._PUBLIC_READER, "list_futures_coverage_batch", lambda value: batch)
    monkeypatch.setattr(
        futures._QUOTEMUX.futures,
        "list_coverage",
        lambda _value: (_ for _ in ()).throw(AssertionError("provider-capable path must not run")),
    )

    assert futures.list_coverage("main_continuous") == [{
        "product_code": "IF", "exchange": "CFFEX", "series_type": "main_continuous", "row_count": 10,
        "first_bar_time": "2026-08-20 09:31:00", "last_bar_time": "2026-08-20 15:00:00",
    }]
