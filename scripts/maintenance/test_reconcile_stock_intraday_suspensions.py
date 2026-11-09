from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).with_name("reconcile_stock_intraday_suspensions.py")
SPEC = importlib.util.spec_from_file_location("reconcile_stock_intraday_suspensions", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _Cursor:
    def __init__(self, results: list[list[tuple[object, ...]]]) -> None:
        self.results = iter(results)
        self.executed: list[tuple[str, object]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> None:
        self.executed.append((query, params))

    def fetchall(self) -> list[tuple[object, ...]]:
        return next(self.results)

    def fetchone(self) -> tuple[bool]:
        raise AssertionError("a no-row candidate must not run the daily post-check")


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor

    def transaction(self) -> "_Connection":
        return self

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_candidates_adds_snapshot_shortfall_codes_without_daily_rows(monkeypatch) -> None:
    monkeypatch.setattr(MODULE, "_snapshot_shortfall_codes", lambda _day: ("000016",))
    cursor = _Cursor(
        [
            [("SHSE", "600000", "2026-10-23", 0)],
            [("SZSE", "000016", "2026-10-23")],
        ]
    )

    result = MODULE.candidates(
        _Connection(cursor),
        "2026-10-23",
        include_snapshot_shortfall=True,
    )

    assert result == [
        ("SHSE", "600000", "2026-10-23", 0, "daily_row"),
        ("SZSE", "000016", "2026-10-23", 0, "snapshot_shortfall_no_daily"),
    ]
    assert "not exists" in cursor.executed[1][0]


def test_no_row_qualified_candidate_writes_history_only() -> None:
    cursor = _Cursor([])
    cursor.rowcount = 1
    connection = _Connection(cursor)

    inserted, updated, ineligible = MODULE.apply_qualified(
        connection,
        [
            {
                "market": "SZSE",
                "code": "000016",
                "trade_date": "2026-10-23",
                "candidate_kind": "snapshot_shortfall_no_daily",
            }
        ],
        "mhf-v1-test",
        "2026-10-23T12:00:00+00:00",
    )

    assert (inserted, updated, ineligible) == (1, 0, 0)
    assert len(cursor.executed) == 2
    assert "pg_advisory_xact_lock" in cursor.executed[0][0]
    assert "insert into fact.stock_suspension_history" in cursor.executed[1][0]


def test_eastmoney_active_filter_excludes_future_start() -> None:
    rows = [
        {"代码": "688053", "停牌时间": "2026-11-10", "停牌截止时间": "2026-11-10"},
        {"代码": "688121", "停牌时间": "2026-10-29", "停牌截止时间": None},
        {"代码": "000831", "停牌时间": "2026-11-02", "停牌截止时间": "2026-11-13"},
    ]

    assert MODULE._eastmoney_active_codes(rows, "2026-11-09") == ("000831", "688121")


def test_eastmoney_real_akshare_columns_match_target_markets() -> None:
    # This mirrors the actual AKShare/Eastmoney response shape: the current
    # endpoint exposes ``代码`` and ``所属市场`` but no ``SECUCODE`` column.
    rows = [
        {"代码": "688121", "所属市场": "上交所"},
        {"代码": "688496", "所属市场": "上交所"},
        {"代码": "000831", "所属市场": "深交所"},
        {"代码": "002129", "所属市场": "深交所"},
        {"代码": "002623", "所属市场": "深交所"},
        {"代码": "002717", "所属市场": "深交所"},
    ]
    expected = [
        ("SHSE", "688121"),
        ("SHSE", "688496"),
        ("SZSE", "000831"),
        ("SZSE", "002129"),
        ("SZSE", "002623"),
        ("SZSE", "002717"),
    ]

    assert all(MODULE._eastmoney_identity_matches(row, market, code)
               for row, (market, code) in zip(rows, expected, strict=True))


def test_eastmoney_identity_rejects_wrong_market_without_secucode() -> None:
    assert not MODULE._eastmoney_identity_matches(
        {"代码": "688121", "所属市场": "深交所"}, "SHSE", "688121"
    )


def test_eastmoney_identity_uses_market_when_secucode_is_null() -> None:
    assert MODULE._eastmoney_identity_matches(
        {"代码": "688121", "SECUCODE": None, "所属市场": "上交所"}, "SHSE", "688121"
    )


def test_snapshot_shortfall_falls_back_to_eastmoney(monkeypatch) -> None:
    calls: list[str] = []

    def unavailable(_target_date: str) -> tuple[str, ...]:
        calls.append("tushare")
        raise RuntimeError("provider unavailable")

    def fallback(_target_date: str) -> tuple[str, ...]:
        calls.append("eastmoney")
        return ("688121",)

    monkeypatch.setattr(MODULE, "_snapshot_shortfall_codes_tushare", unavailable)
    monkeypatch.setattr(MODULE, "_snapshot_shortfall_codes_eastmoney", fallback)

    assert MODULE._snapshot_shortfall_codes("2026-11-09") == ("688121",)
    assert calls == ["tushare", "eastmoney"]


def test_source_probe_falls_back_to_eastmoney(monkeypatch) -> None:
    targets = [("SHSE", "688121", "2026-11-09", 0, "snapshot_shortfall_no_daily")]
    calls: list[str] = []
    qualified = [{"source": MODULE.EASTMONEY_SOURCE, "source_marker": MODULE.EASTMONEY_SOURCE_MARKER}]

    def unavailable(_targets):
        calls.append("tushare")
        raise RuntimeError("provider unavailable")

    def fallback(_targets):
        calls.append("eastmoney")
        return ([{"source": MODULE.EASTMONEY_SOURCE}], qualified, [])

    monkeypatch.setattr(MODULE, "_source_probe_tushare", unavailable)
    monkeypatch.setattr(MODULE, "_source_probe_eastmoney", fallback)

    raw, result, residual = MODULE.source_probe(targets)
    assert calls == ["tushare", "eastmoney"]
    assert result == qualified
    assert residual == []
    assert raw[0]["fallback"] == MODULE.EASTMONEY_SOURCE


def test_source_probe_falls_back_when_tushare_has_per_target_error(monkeypatch) -> None:
    targets = [("SHSE", "688121", "2026-11-09", 0, "snapshot_shortfall_no_daily")]
    calls: list[str] = []
    received_by_fallback: list[list[MODULE.Candidate]] = []
    tushare_error = {
        "market": "SHSE",
        "code": "688121",
        "trade_date": "2026-11-09",
        "candidate_kind": "snapshot_shortfall_no_daily",
        "reason": "source_error:RuntimeError:no suspend_d access",
    }
    eastmoney_qualified = [{
        "market": "SHSE",
        "code": "688121",
        "trade_date": "2026-11-09",
        "candidate_kind": "snapshot_shortfall_no_daily",
        "source": MODULE.EASTMONEY_SOURCE,
        "source_marker": MODULE.EASTMONEY_SOURCE_MARKER,
    }]

    def partial_failure(_targets):
        calls.append("tushare")
        return ([{"source": MODULE.TUSHARE_SOURCE, "target_count": 1}], [], [tushare_error])

    def fallback(fallback_targets):
        calls.append("eastmoney")
        received_by_fallback.append(fallback_targets)
        return ([{"source": MODULE.EASTMONEY_SOURCE, "target_count": len(fallback_targets)}], eastmoney_qualified, [])

    monkeypatch.setattr(MODULE, "_source_probe_tushare", partial_failure)
    monkeypatch.setattr(MODULE, "_source_probe_eastmoney", fallback)

    raw, result, residual = MODULE.source_probe(targets)
    assert calls == ["tushare", "eastmoney"]
    assert received_by_fallback == [targets]
    assert result == eastmoney_qualified
    assert residual == []
    assert raw[0]["fallback"] == MODULE.EASTMONEY_SOURCE
    assert raw[0]["errors"] == [tushare_error]


def test_eastmoney_lineage_is_used_for_history_insert() -> None:
    cursor = _Cursor([])
    cursor.rowcount = 1
    connection = _Connection(cursor)

    MODULE.apply_qualified(
        connection,
        [{
            "market": "SHSE",
            "code": "688121",
            "trade_date": "2026-11-09",
            "candidate_kind": "snapshot_shortfall_no_daily",
            "source": MODULE.EASTMONEY_SOURCE,
            "source_marker": MODULE.EASTMONEY_SOURCE_MARKER,
        }],
        "mhf-v1-test",
        "2026-11-09T12:00:00+00:00",
    )

    params = cursor.executed[1][1]
    assert MODULE.EASTMONEY_SOURCE in params
    assert MODULE.EASTMONEY_SOURCE_MARKER in params


def test_dry_run_apply_gate_skips_database_write() -> None:
    assert MODULE.apply_if_requested(None, [{"candidate_kind": "snapshot_shortfall_no_daily"}], "dv", "captured", dry_run=True) == (0, 0, 0)
