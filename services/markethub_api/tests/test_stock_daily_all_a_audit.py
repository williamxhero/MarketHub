from __future__ import annotations

from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "maintenance" / "audit_stock_daily_all_a.py"


def test_audit_is_read_only_exhaustive_and_preserves_eligibility() -> None:
    content = SCRIPT.read_text(encoding="utf-8").lower()
    assert "transaction isolation level repeatable read read only" in content
    assert "stock_suspension_history" in content
    assert "listed_date<=d.trade_date" in content
    # ref.stock.delisted_date is the first ineligible date. Including it creates
    # false gaps for every code whose source correctly has no bar on that day.
    assert "d.trade_date<u.delisted_date" in content
    assert "b.open is null" in content and "b.volume is null" in content
    assert "date '2021-11-15'" in content
    for forbidden in ("insert into", "update fact.", "delete from", "on conflict"):
        assert forbidden not in content
