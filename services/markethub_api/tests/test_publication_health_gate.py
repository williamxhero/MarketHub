from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "dailyupdate" / "publication_health_gate.py"
SPEC = importlib.util.spec_from_file_location("publication_health_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

STOCK_DAILY_DEPENDENCIES = (
    "database,trade_calendar,core_dataset_freshness:fact.stock_daily_1d,"
    "ohlc_valid:fact.stock_daily_1d,daily_coverage_90d"
)
CHECKED_AT = "2026-09-17 07:58:32"


def _payload(
    *,
    freshness: dict[str, str],
    stock_daily_checks: dict[str, str],
    capability_status: str = "unhealthy",
    checked_at: str = CHECKED_AT,
) -> dict[str, Any]:
    """Build a report shaped like /api/data-health's real response."""
    return {
        "status": "unhealthy",
        "checked_at": checked_at,
        "summary": {"status": "unhealthy", "total": 103, "healthy": 97, "warning": 3, "unhealthy": 3},
        "dependencies": {
            "database": {"status": "healthy", "available": True},
            "trade_calendar": {"status": "healthy"},
            "core_dataset_freshness": {
                "status": "unhealthy",
                "checks": [
                    {"check_id": f"core_dataset_freshness:{name}", "status": status}
                    for name, status in freshness.items()
                ],
            },
        },
        "capabilities": [
            {
                "capability_id": "stocks.quotes.daily",
                "status": capability_status,
                "checks": [
                    {"check_id": check_id, "status": status}
                    for check_id, status in stock_daily_checks.items()
                ],
            },
            {
                "capability_id": "concepts.indicators.money_flow",
                "status": "unhealthy",
                "checks": [{"check_id": "money_flow_values_valid", "status": "unhealthy"}],
            },
        ],
    }


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _run(path: Path, dependencies: str = STOCK_DAILY_DEPENDENCIES, not_before: str = "") -> int:
    return GATE.main(["--payload", str(path), "--dependencies", dependencies, "--not-before", not_before])


def test_unrelated_unhealthy_datasets_do_not_defer_the_publication(tmp_path: Path, capsys: Any) -> None:
    """The live 2026-09-17 failure mode: the aggregate is unhealthy only for
    datasets and catalog attributes the stock_daily_1d export does not carry."""
    payload = _payload(
        freshness={
            "fact.stock_daily_1d": "healthy",
            "fact.index_bar_1d": "unhealthy",
            "fact.concept_daily_1d": "unhealthy",
            "fact.board_daily_1d": "unhealthy",
        },
        stock_daily_checks={
            "ohlc_valid:fact.stock_daily_1d": "healthy",
            "daily_coverage_90d": "healthy",
            "market_data_contract:global_blank_board_type_count": "unhealthy",
        },
    )

    assert _run(_write(tmp_path, payload)) == GATE.EXIT_PASSED
    assert "publication_health_gate=passed" in capsys.readouterr().out


def test_unhealthy_declared_dependency_defers_the_publication(tmp_path: Path, capsys: Any) -> None:
    payload = _payload(
        freshness={"fact.stock_daily_1d": "unhealthy", "fact.index_bar_1d": "healthy"},
        stock_daily_checks={"ohlc_valid:fact.stock_daily_1d": "healthy", "daily_coverage_90d": "healthy"},
        capability_status="healthy",
    )

    assert _run(_write(tmp_path, payload)) == GATE.EXIT_DEFERRED
    assert "blocking=core_dataset_freshness:fact.stock_daily_1d" in capsys.readouterr().out


def test_unresolved_dependency_fails_closed(tmp_path: Path, capsys: Any) -> None:
    payload = _payload(
        freshness={"fact.stock_daily_1d": "healthy"},
        stock_daily_checks={"ohlc_valid:fact.stock_daily_1d": "healthy", "daily_coverage_90d": "healthy"},
    )

    assert _run(_write(tmp_path, payload), dependencies="never_registered_check") == GATE.EXIT_DEFERRED
    assert "blocking=never_registered_check" in capsys.readouterr().out


def test_warning_dependency_does_not_defer_the_publication(tmp_path: Path) -> None:
    payload = _payload(
        freshness={"fact.stock_daily_1d": "healthy"},
        stock_daily_checks={"ohlc_valid:fact.stock_daily_1d": "warning", "daily_coverage_90d": "healthy"},
    )

    assert _run(_write(tmp_path, payload)) == GATE.EXIT_PASSED


def test_worst_status_wins_when_a_check_id_repeats(tmp_path: Path) -> None:
    payload = _payload(
        freshness={"fact.stock_daily_1d": "healthy"},
        stock_daily_checks={"ohlc_valid:fact.stock_daily_1d": "healthy", "daily_coverage_90d": "healthy"},
    )
    payload["capabilities"].append(
        {
            "capability_id": "stocks.quotes.daily_snapshot",
            "status": "healthy",
            "checks": [{"check_id": "daily_coverage_90d", "status": "unhealthy"}],
        }
    )

    assert _run(_write(tmp_path, payload)) == GATE.EXIT_DEFERRED


def test_stale_report_is_a_config_error(tmp_path: Path, capsys: Any) -> None:
    payload = _payload(
        freshness={"fact.stock_daily_1d": "healthy"},
        stock_daily_checks={"ohlc_valid:fact.stock_daily_1d": "healthy", "daily_coverage_90d": "healthy"},
        checked_at="2026-09-16 04:00:00",
    )

    assert _run(_write(tmp_path, payload), not_before="2026-09-17 04:00:00") == GATE.EXIT_CONFIG_ERROR
    assert "publication_health_gate=config_error" in capsys.readouterr().out


def test_fresh_report_passes_the_staleness_boundary(tmp_path: Path) -> None:
    payload = _payload(
        freshness={"fact.stock_daily_1d": "healthy"},
        stock_daily_checks={"ohlc_valid:fact.stock_daily_1d": "healthy", "daily_coverage_90d": "healthy"},
    )

    assert _run(_write(tmp_path, payload), not_before="2026-09-17 07:56:29") == GATE.EXIT_PASSED


def test_empty_declaration_is_a_config_error(tmp_path: Path) -> None:
    payload = _payload(
        freshness={"fact.stock_daily_1d": "healthy"},
        stock_daily_checks={"ohlc_valid:fact.stock_daily_1d": "healthy", "daily_coverage_90d": "healthy"},
    )

    assert _run(_write(tmp_path, payload), dependencies=" , ") == GATE.EXIT_CONFIG_ERROR


def test_missing_payload_is_a_config_error(tmp_path: Path) -> None:
    assert _run(tmp_path / "absent.json") == GATE.EXIT_CONFIG_ERROR


def test_deploy_installs_the_publication_health_gate() -> None:
    deploy = (Path(__file__).resolve().parents[3] / "scripts" / "local" / "deploy_yosef_server.ps1").read_text(encoding="utf-8")

    assert (
        'install -m 0755 "$remote_root/current/MarketHub/scripts/dailyupdate/publication_health_gate.py" '
        '"$runtime_root/scripts/publication_health_gate.py"'
    ) in deploy
