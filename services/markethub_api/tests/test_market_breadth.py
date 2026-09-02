from __future__ import annotations

from datetime import datetime

from services.market_breadth import build_market_breadth


def test_complete_close_breadth_has_disjoint_universe_accounting() -> None:
    result = build_market_breadth(
        trade_date="2026-09-02",
        now=datetime.fromisoformat("2026-09-02T15:20:00+08:00"),
        rows={
            "universe_count": 6,
            "priced_count": 4,
            "up_count": 2,
            "down_count": 1,
            "flat_count": 1,
            "suspended_count": 1,
            "unpriced_count": 1,
            "invalid_price_count": 0,
            "first_loaded_at": "2026-09-02T15:31:00+08:00",
            "last_loaded_at": "2026-09-02T15:32:00+08:00",
        },
        dataset_version="mhd-v1-test",
        is_open=True,
    )

    assert result["status"] == "incomplete"
    assert result["finality"] == "not_final"
    assert result["up"] is None
    assert result["coverage"]["observed_up"] == 2
    assert result["coverage"]["missing_count"] == 1


def test_complete_close_breadth_exposes_final_counts_and_close_fact_time() -> None:
    result = build_market_breadth(
        trade_date="2026-09-02",
        now=datetime.fromisoformat("2026-09-02T15:20:00+08:00"),
        rows={
            "universe_count": 5,
            "priced_count": 4,
            "up_count": 2,
            "down_count": 1,
            "flat_count": 1,
            "suspended_count": 1,
            "unpriced_count": 0,
            "invalid_price_count": 0,
            "first_loaded_at": "2026-09-02T15:10:00+08:00",
            "last_loaded_at": "2026-09-02T15:11:00+08:00",
        },
        dataset_version="mhd-v1-test",
        is_open=True,
    )

    assert result["status"] == "complete"
    assert result["finality"] == "final"
    assert result["fact_as_of"] == "2026-09-02T15:00:00+08:00"
    assert (result["up"], result["down"], result["flat"]) == (2, 1, 1)
    assert result["universe_count"] == 5
    assert result["unpriced"] == 0
    assert result["suspended"] == 1
    assert result["lineage"]["loaded_at_max"] == "2026-09-02T15:11:00+08:00"


def test_before_close_never_claims_finality_even_with_full_rows() -> None:
    result = build_market_breadth(
        trade_date="2026-09-02",
        now=datetime.fromisoformat("2026-09-02T14:59:59+08:00"),
        rows={
            "universe_count": 1,
            "priced_count": 1,
            "up_count": 1,
            "down_count": 0,
            "flat_count": 0,
            "suspended_count": 0,
            "unpriced_count": 0,
            "invalid_price_count": 0,
            "first_loaded_at": "2026-09-02T14:59:00+08:00",
            "last_loaded_at": "2026-09-02T14:59:00+08:00",
        },
        dataset_version="mhd-v1-test",
        is_open=True,
    )

    assert result["status"] == "not_closed"
    assert result["up"] is None
    assert result["fact_as_of"] is None
