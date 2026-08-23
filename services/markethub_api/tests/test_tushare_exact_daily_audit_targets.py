from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "maintenance" / "backfill_tushare_exact_daily.py"
SPEC = importlib.util.spec_from_file_location("tushare_exact_daily_audit_targets", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_exact_daily_export_accepts_exhaustive_daily_audit(tmp_path: Path) -> None:
    audit_path = tmp_path / "baseline.json"
    audit_path.write_text(
        json.dumps(
            {
                "contract": "markethub-stock-daily-all-a-audit-v1",
                "scope": "exhaustive",
                "gaps": [
                    {"code": "600984", "trade_date": "2026-08-21", "gap_kind": "absent"},
                    {"code": "300176", "trade_date": "2026-08-14", "gap_kind": "stored_suspended"},
                    {"code": "300176", "trade_date": "2026-08-15", "gap_kind": "stored_suspended"},
                ],
            }
        ),
        encoding="utf-8",
    )

    data_version, targets = MODULE.parse_failure_details(audit_path, {"600984", "300176"})

    assert data_version == ""
    assert [str(value) for value in targets["300176"]] == ["2026-08-14", "2026-08-15"]
    assert [str(value) for value in targets["600984"]] == ["2026-08-21"]


def test_exact_daily_export_rejects_partial_audit_scope(tmp_path: Path) -> None:
    audit_path = tmp_path / "baseline.json"
    audit_path.write_text(
        json.dumps(
            {
                "contract": "markethub-stock-daily-all-a-audit-v1",
                "scope": "sample",
                "gaps": [{"code": "600984", "trade_date": "2026-08-21"}],
            }
        ),
        encoding="utf-8",
    )

    try:
        MODULE.parse_failure_details(audit_path, {"600984"})
    except ValueError as exc:
        assert "not exhaustive" in str(exc)
    else:
        raise AssertionError("partial audit must not be accepted")


def test_exact_daily_import_allows_existing_suspended_placeholders() -> None:
    MODULE._assert_import_preconditions(
        {
            "traded_daily_rows": 0,
            "suspension_rows": 0,
            "conflicting_suspended_daily_rows": 0,
            "daily_rows": 146,
        }
    )


def test_exact_daily_import_rejects_conflicting_suspended_fact() -> None:
    try:
        MODULE._assert_import_preconditions(
            {
                "traded_daily_rows": 0,
                "suspension_rows": 0,
                "conflicting_suspended_daily_rows": 1,
            }
        )
    except RuntimeError as exc:
        assert "target facts changed" in str(exc)
    else:
        raise AssertionError("a traded fact must not be accepted as a suspended placeholder")
