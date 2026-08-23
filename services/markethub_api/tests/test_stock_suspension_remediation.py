from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "maintenance" / "backfill_stock_suspension_history_tushare.py"
SPEC = importlib.util.spec_from_file_location("stock_suspension_remediation", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_remediation_requires_source_native_full_day_evidence_and_frozen_version() -> None:
    content = SCRIPT.read_text(encoding="utf-8")
    assert 'SOURCE = "Tushare.suspend_d"' in content
    assert 'SOURCE_MARKER = "suspend_type_S_full_day_no_daily"' in content
    assert 'BAOSTOCK_MARKER = "tradestatus_0_zero_volume_amount"' in content
    assert "_source_numeric_zero" in content
    assert 'BSE_INCEPTION = date(2021, 11, 15)' in content
    assert '"provider_has_daily_row"' in content
    assert 'full_day_suspend_records=' in content
    assert 'residual_rows", -1)) != 0' in content
    assert "live release/data version drifted from frozen source artifact" in content
    assert "artifact target accounting mismatch" in content
    assert "where not exists" in content.lower()
    assert "--commit" in content


def test_remediation_accepts_legacy_null_volume_suspended_placeholder(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "contract": "markethub-stock-daily-all-a-audit-v1",
                "scope": "exhaustive",
                "gap_rows": 1,
                "gaps": [
                    {
                        "market": "SHSE",
                        "code": "600000",
                        "trade_date": "2026-08-14",
                        "gap_kind": "stored_suspended",
                        "is_suspended": True,
                        "volume": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    _, targets = MODULE._targets(audit_path)

    assert tuple(targets) == (("SHSE", "600000"),)
    assert str(targets[("SHSE", "600000")][0]) == "2026-08-14"


def test_remediation_rejects_nonzero_suspended_rows(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "contract": "markethub-stock-daily-all-a-audit-v1",
                "scope": "exhaustive",
                "gap_rows": 1,
                "gaps": [
                    {
                        "market": "SHSE",
                        "code": "600000",
                        "trade_date": "2026-08-14",
                        "gap_kind": "stored_suspended",
                        "is_suspended": True,
                        "volume": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        MODULE._targets(audit_path)
    except ValueError as exc:
        assert "suspended placeholder" in str(exc)
    else:
        raise AssertionError("nonzero row must not be accepted as a suspension placeholder")
