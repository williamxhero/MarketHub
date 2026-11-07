from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).parent
INSTALLER = ROOT.parent.parent / "migrations" / "storage_v2_20260823" / "install_release_linux.sh"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


contracts = _load("backfill_contracts")
money = _load("backfill_stock_money_flow")
indicators = _load("backfill_stock_market_indicators")
price_band = _load("backfill_stock_price_band")


def _row(code: str, **values: object) -> dict[str, object]:
    return {"code": code, "trade_date": "2026-10-29", **values}


def test_required_rows_rejects_one_populated_field_as_complete() -> None:
    rows = [_row("000001", active_buy_amount=1.0)]

    with pytest.raises(RuntimeError, match="必填字段缺失"):
        contracts.require_complete_rows(
            trade_date="20261029",
            expected_codes={"000001"},
            items=rows,
            required_fields=money.REQUIRED_FIELDS,
            label="资金流",
        )


def test_required_rows_rejects_nan_as_missing() -> None:
    rows = [_row("000001", limit_up=math.nan, limit_down=0.9)]

    with pytest.raises(RuntimeError, match="必填字段缺失"):
        contracts.require_complete_rows(
            trade_date="20261029",
            expected_codes={"000001"},
            items=rows,
            required_fields=price_band.REQUIRED_FIELDS,
            label="历史涨跌停边界",
        )


def test_required_rows_rejects_partial_code_set() -> None:
    with pytest.raises(RuntimeError, match="缺少股票代码"):
        contracts.require_complete_rows(
            trade_date="20261029",
            expected_codes={"000001", "000002"},
            items=[_row("000001", limit_up=1.0, limit_down=0.9)],
            required_fields=price_band.REQUIRED_FIELDS,
            label="历史涨跌停边界",
        )


def test_required_rows_rejects_duplicate_expected_code() -> None:
    rows = [
        _row("000001", dv_ratio=1.0, dv_ttm=2.0),
        _row("000001", dv_ratio=1.1, dv_ttm=2.1),
    ]

    with pytest.raises(RuntimeError, match="重复代码"):
        contracts.require_complete_rows(
            trade_date="20261029",
            expected_codes={"000001"},
            items=rows,
            required_fields=indicators.REQUIRED_VALUATION_FIELDS,
            label="市场指标 valuation",
        )


def test_required_rows_accepts_every_required_field_for_every_code() -> None:
    rows = [
        _row("000001", limit_up=1.0, limit_down=0.9),
        _row("000002", limit_up=2.0, limit_down=1.8),
    ]

    result = contracts.require_complete_rows(
        trade_date="20261029",
        expected_codes={"000001", "000002"},
        items=rows,
        required_fields=price_band.REQUIRED_FIELDS,
        label="历史涨跌停边界",
    )
    assert sorted(result) == ["000001", "000002"]


def test_release_installer_has_fail_closed_gate_and_preserves_old_release() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert "capture_owner_quiescence_gate" in source
    assert source.index("capture_owner_quiescence_gate") < source.index('systemctl stop "$service_name.service"')
    assert "readback_release_manifest" in source
    assert 'tar --no-same-owner -xzf "$archive" -C "$candidate_root"' in source
    assert 'mv -- "$candidate_root" "$release_root"' in source
    assert "candidate_file_inventory_sha256" in source
    assert "commit_inputs" in source
    assert "chmod a+X" not in source
    assert "systemctl restart" not in source
    assert 'rm -rf -- "$release_root"' not in source
