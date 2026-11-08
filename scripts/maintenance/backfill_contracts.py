from __future__ import annotations

from collections.abc import Iterable, Mapping
import math


def _missing(value: object) -> bool:
    if value is None:
        return True
    try:
        return math.isnan(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def require_complete_rows(
    *,
    trade_date: str,
    expected_codes: Iterable[str],
    items: Iterable[Mapping[str, object]],
    required_fields: Iterable[str],
    label: str,
) -> dict[str, Mapping[str, object]]:
    """Require one complete provider row for every expected active stock.

    Provider snapshots may contain extra codes, but an expected code must occur
    exactly once and every field that the destination fact contract publishes
    must be present.  This keeps a single populated field from masking a
    partial snapshot.
    """
    expected = {str(code).zfill(6) for code in expected_codes}
    fields = tuple(required_fields)
    by_code: dict[str, Mapping[str, object]] = {}
    for item in items:
        code = str(item.get("code", "")).zfill(6)
        if code not in expected or str(item.get("trade_date", "")).replace("-", "") != trade_date:
            continue
        if code in by_code:
            raise RuntimeError(f"{label} 返回重复代码: trade_date={trade_date} code={code}")
        by_code[code] = item
    missing = sorted(expected - set(by_code))
    if missing:
        raise RuntimeError(f"{label} 缺少股票代码: trade_date={trade_date} missing={','.join(missing[:20])}")
    incomplete = {
        code: [field for field in fields if _missing(by_code[code].get(field))]
        for code in sorted(expected)
    }
    incomplete = {code: values for code, values in incomplete.items() if values}
    if incomplete:
        sample = ";".join(f"{code}:{','.join(values)}" for code, values in list(incomplete.items())[:20])
        raise RuntimeError(f"{label} 必填字段缺失: trade_date={trade_date} rows={sample}")
    return by_code
