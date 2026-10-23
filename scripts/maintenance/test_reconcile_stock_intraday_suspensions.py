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
