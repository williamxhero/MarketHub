from __future__ import annotations

import sys
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from runtime_paths import configure_python_path


configure_python_path()

from services import data_health


def test_data_health_profiles_are_loaded_from_service_json() -> None:
    profiles_path = data_health._profiles_path()
    assert profiles_path.name == "data_health_checks.json"
    assert profiles_path.parent.name == "services"
    assert "docs" not in str(profiles_path)

    profiles = data_health._load_profiles()
    assert len(profiles) >= 83
    money_flow = profiles["concepts.indicators.money_flow"]
    assert money_flow["reference_objects"] == ["fact.stock_daily_1d", "ref.trade_calendar", "ref.concept", "ref.concept_stock_membership"]
    assert {check["type"] for check in money_flow["checks"]} >= {"database_available", "table_exists", "reference_valid", "fixed_field", "money_flow_values_valid"}


def test_data_health_run_endpoint_returns_structured_checks(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient
    from app import app

    monkeypatch.setenv("MARKETHUB_DATA_HEALTH_ROOT", str(tmp_path))
    monkeypatch.setattr(data_health, "is_db_available", lambda: False)

    response = TestClient(app).post("/api/data-health/run")
    assert response.status_code == 200
    capability = _capability(response.json(), "concepts.indicators.money_flow")
    checks = capability["checks"]
    assert isinstance(checks, list)
    assert checks != []
    assert {"check_id", "title", "status", "result_text", "error_text"}.issubset(set(checks[0]))
    assert (tmp_path / "latest.json").is_file()


def test_data_health_get_reads_latest_snapshot_without_running_checks(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient
    from app import app

    snapshot = {
        "status": "healthy",
        "checked_at": "2026-07-07 12:00:00",
        "summary": {"status": "healthy", "total": 1, "healthy": 1, "warning": 0, "unhealthy": 0},
        "dependencies": {},
        "groups": [],
        "capabilities": [],
    }
    monkeypatch.setenv("MARKETHUB_DATA_HEALTH_ROOT", str(tmp_path))
    (tmp_path / "latest.json").write_text(__import__("json").dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(data_health, "is_db_available", lambda: (_ for _ in ()).throw(AssertionError("GET should not run checks")))

    response = TestClient(app).get("/api/data-health")
    assert response.status_code == 200
    assert response.json() == snapshot


def _capability(payload: dict[str, object], capability_id: str) -> dict[str, object]:
    capabilities = payload["capabilities"]
    assert isinstance(capabilities, list)
    for item in capabilities:
        assert isinstance(item, dict)
        if item.get("capability_id") == capability_id:
            return item
    raise AssertionError(f"missing capability {capability_id}")


def _checks_by_id(capability: dict[str, object]) -> dict[str, dict[str, str]]:
    checks = capability["checks"]
    assert isinstance(checks, list)
    result: dict[str, dict[str, str]] = {}
    for check in checks:
        assert isinstance(check, dict)
        result[str(check["check_id"])] = {str(key): str(value) for key, value in check.items()}
    return result


def _availability(objects: dict[str, bool]) -> dict[str, object]:
    return {
        "status": "warning",
        "warnings": [],
        "objects": [
            {"name": name, "exists": exists, "missing_indexes": [], "row_count": 100 if exists else 0, "min_value": "2026-01-01", "max_value": "2026-07-01"}
            for name, exists in objects.items()
        ],
    }


def test_money_flow_profile_contains_expected_structured_checks(monkeypatch) -> None:
    monkeypatch.setattr(data_health, "is_db_available", lambda: False)

    payload = data_health._compute_data_health()
    capability = _capability(payload, "concepts.indicators.money_flow")
    checks = _checks_by_id(capability)

    assert checks["database_available"]["status"] == "unhealthy"
    assert checks["table_exists:fact.stock_daily_1d"]["status"] == "unhealthy"
    assert checks["table_exists:ref.trade_calendar"]["status"] == "unhealthy"
    assert checks["table_exists:ref.concept"]["status"] == "unhealthy"
    assert checks["table_exists:ref.concept_stock_membership"]["status"] == "unhealthy"
    assert "trade_calendar_available" in checks
    assert "concept_reference_valid" in checks
    assert "concept_membership_valid" in checks
    assert "concept_id/trade_date/scope" in checks["capability_key_unique_rule"]["title"]
    assert "concept" in checks["scope_fixed"]["title"]
    assert "money_flow_values_valid" in checks
    assert "non_trading_day_empty_result" in checks
    assert "concept_money_flow_long_empty" in checks


def test_db_unavailable_has_no_coarse_logic_dependency_check(monkeypatch) -> None:
    monkeypatch.setattr(data_health, "is_db_available", lambda: False)

    payload = data_health._compute_data_health()
    capability = _capability(payload, "concepts.indicators.money_flow")
    checks = capability["checks"]
    assert isinstance(checks, list)

    assert all(isinstance(check, dict) and check.get("check_id") != "coarse_logic_dependency" for check in checks)
    database_check = _checks_by_id(capability)["database_available"]
    assert database_check["status"] == "unhealthy"
    assert database_check["error_text"] != ""


def test_dependency_tables_are_reported_independently(monkeypatch) -> None:
    monkeypatch.setattr(data_health, "is_db_available", lambda: True)
    monkeypatch.setattr(
        data_health,
        "get_fact_ref_availability",
        lambda: _availability(
            {
                "fact.stock_daily_1d": False,
                "ref.trade_calendar": True,
                "ref.concept": True,
                "ref.concept_stock_membership": False,
            }
        ),
    )
    monkeypatch.setattr(data_health, "query_dataframe", lambda *args, **kwargs: _empty_frame())

    payload = data_health._compute_data_health()
    checks = _checks_by_id(_capability(payload, "concepts.indicators.money_flow"))

    assert checks["table_exists:fact.stock_daily_1d"]["status"] == "unhealthy"
    assert checks["table_exists:fact.stock_daily_1d"]["error_text"] != ""
    assert checks["table_exists:ref.trade_calendar"]["status"] == "healthy"
    assert checks["table_exists:ref.concept"]["status"] == "healthy"
    assert checks["table_exists:ref.concept_stock_membership"]["status"] == "unhealthy"


def test_status_from_checks_priority() -> None:
    assert data_health._status_from_checks([{"status": "healthy"}]) == "healthy"
    assert data_health._status_from_checks([{"status": "healthy"}, {"status": "warning"}]) == "warning"
    assert data_health._status_from_checks([{"status": "healthy"}, {"status": "unknown"}]) == "warning"
    assert data_health._status_from_checks([{"status": "warning"}, {"status": "unhealthy"}]) == "unhealthy"


def test_duplicate_check_uses_primary_key_index_without_scanning(monkeypatch) -> None:
    def fail_query_dataframe(*args, **kwargs):
        raise AssertionError("should not scan duplicate rows when primary key index exists")

    monkeypatch.setattr(data_health, "query_dataframe", fail_query_dataframe)
    check = data_health._duplicate_check(
        "fact.stock_bar_1m",
        {"fact.stock_bar_1m": {"exists": True, "missing_indexes": []}},
        True,
    )

    assert check.status == "healthy"
    assert check.error_text == ""


def test_recent_coverage_uses_provider_earliest_date(monkeypatch) -> None:
    import pandas as pd

    calls: list[object] = []

    def fake_query_dataframe(query: str, params: object = None):
        calls.append(params)
        if "expected_days" in query:
            return pd.DataFrame([{"expected_days": 10}])
        return pd.DataFrame([{"actual_days": 9}])

    monkeypatch.setattr(data_health, "query_dataframe", fake_query_dataframe)
    check = data_health._recent_coverage_check(
        "daily_coverage_90d",
        "",
        "fact.stock_daily_1d",
        "trade_date",
        "1990-12-19",
        {"fact.stock_daily_1d": {"exists": True}},
        True,
        {"status": "healthy"},
    )

    assert calls == [("1990-12-19",), ("1990-12-19",)]
    assert check.status == "warning"
    assert "1990-12-19" in check.error_text
    assert "9/10" in check.error_text


def test_recent_coverage_rejects_missing_provider_earliest_date() -> None:
    check = data_health._recent_coverage_check(
        "daily_coverage_90d",
        "",
        "fact.stock_daily_1d",
        "trade_date",
        "",
        {"fact.stock_daily_1d": {"exists": True}},
        True,
        {"status": "healthy"},
    )

    assert check.status == "unknown"
    assert "provider_earliest_date" in check.error_text


def test_recent_minute_session_check_reports_incomplete_trade_day(monkeypatch) -> None:
    import pandas as pd

    def fake_query_dataframe(query: str, params: object = None):
        if "from ref.trade_calendar" in query:
            assert params == (10,)
            return pd.DataFrame([{"trade_date": "2026-07-09"}])
        assert ">= %s::timestamp" in query
        assert "<= %s::timestamp" in query
        assert "= any(%s::date[])" in query
        assert "expected_minutes" not in query
        assert params == ("2026-07-09 09:31:00", "2026-07-09 15:00:00", ["2026-07-09"])
        return pd.DataFrame(
            [
                {
                    "trade_date": "2026-07-09",
                    "minute_count": 120,
                    "total_rows": 600000,
                    "first_minute": "09:31:00",
                    "last_minute": "11:30:00",
                    "min_rows_per_minute": 5000,
                    "max_rows_per_minute": 5000,
                }
            ]
        )

    monkeypatch.setattr(data_health, "query_dataframe", fake_query_dataframe)
    check = data_health._recent_minute_session_check(
        "recent_minute_session_complete:fact.stock_bar_1m",
        "1m recent session completeness",
        "fact.stock_bar_1m",
        240,
        10,
        0.95,
        {"fact.stock_bar_1m": {"exists": True}},
        True,
        {"status": "healthy"},
    )

    assert check.status == "warning"
    assert "120/240" in check.error_text
    assert "09:31-11:30" in check.error_text


def test_recent_minute_session_check_passes_complete_trade_days(monkeypatch) -> None:
    import pandas as pd

    def fake_query_dataframe(query: str, params: object = None):
        if "from ref.trade_calendar" in query:
            return pd.DataFrame([{"trade_date": "2026-07-08"}])
        return pd.DataFrame(
            [
                {
                    "trade_date": "2026-07-08",
                    "minute_count": 240,
                    "total_rows": 1329838,
                    "first_minute": "09:31:00",
                    "last_minute": "15:00:00",
                    "min_rows_per_minute": 5518,
                    "max_rows_per_minute": 5518,
                }
            ]
        )

    monkeypatch.setattr(data_health, "query_dataframe", fake_query_dataframe)

    check = data_health._recent_minute_session_check(
        "recent_minute_session_complete:fact.stock_bar_1m",
        "",
        "fact.stock_bar_1m",
        240,
        10,
        0.95,
        {"fact.stock_bar_1m": {"exists": True}},
        True,
        {"status": "healthy"},
    )

    assert check.status == "healthy"
    assert check.error_text == ""


def test_recent_minute_session_check_reports_missing_expected_day(monkeypatch) -> None:
    import pandas as pd

    def fake_query_dataframe(query: str, params: object = None):
        if "from ref.trade_calendar" in query:
            return pd.DataFrame([{"trade_date": "2026-07-09"}, {"trade_date": "2026-07-08"}])
        return pd.DataFrame(
            [
                {
                    "trade_date": "2026-07-09",
                    "minute_count": 240,
                    "total_rows": 1329838,
                    "first_minute": "09:31:00",
                    "last_minute": "15:00:00",
                    "min_rows_per_minute": 5518,
                    "max_rows_per_minute": 5518,
                }
            ]
        )

    monkeypatch.setattr(data_health, "query_dataframe", fake_query_dataframe)
    check = data_health._recent_minute_session_check(
        "recent_minute_session_complete:fact.stock_bar_1m",
        "",
        "fact.stock_bar_1m",
        240,
        10,
        0.95,
        {"fact.stock_bar_1m": {"exists": True}},
        True,
        {"status": "healthy"},
    )

    assert check.status == "warning"
    assert "2026-07-08 无 1m 数据" in check.error_text


def test_market_data_contract_check_reports_metric_failure() -> None:
    metrics = {metric_name: expectation[0] for metric_name, expectation in data_health.MARKET_DATA_CONTRACT_EXPECTATIONS.items()}
    metrics["global_ref_index_bad_name_count"] = 2

    check = data_health._market_data_contract_check("global_ref_index_bad_name_count", metrics)

    assert check.check_id == "market_data_contract:global_ref_index_bad_name_count"
    assert check.status == "unhealthy"
    assert "2" in check.error_text
    assert "0" in check.error_text


def test_market_data_contract_check_is_configured_on_stock_daily_profile() -> None:
    profiles = data_health._load_profiles()
    checks = profiles["stocks.quotes.daily"]["checks"]

    assert any(check.get("type") == "market_data_contract" for check in checks if isinstance(check, dict))


def test_concept_market_data_contract_is_isolated_from_stock_daily_profile() -> None:
    profiles = data_health._load_profiles()
    concept_check = next(
        check
        for check in profiles["concepts.quotes.daily"]["checks"]
        if isinstance(check, dict) and check.get("type") == "market_data_contract"
    )
    stock_check = next(
        check
        for check in profiles["stocks.quotes.daily"]["checks"]
        if isinstance(check, dict) and check.get("type") == "market_data_contract"
    )

    assert "second_stage_amount_mismatch_count" in concept_check["metrics"]
    assert "second_stage_amount_mismatch_count" not in stock_check["metrics"]


def test_market_data_contract_checks_share_one_metrics_query(monkeypatch) -> None:
    calls = 0

    def fake_query_metrics(index_code: str) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {metric_name: expectation[0] for metric_name, expectation in data_health.MARKET_DATA_CONTRACT_EXPECTATIONS.items()}

    monkeypatch.setattr(data_health, "_query_market_data_contract_metrics", fake_query_metrics)
    cache: dict[str, object] = {}
    checks = data_health._market_data_contract_checks(
        ["global_ref_index_bad_name_count", "global_stock_daily_core_null_count"],
        "000001",
        [],
        {object_name: {"exists": True} for object_name in data_health.MARKET_DATA_CONTRACT_REQUIRED_OBJECTS},
        True,
        {"status": "healthy"},
        cache,
    )

    assert calls == 1
    assert [check.status for check in checks] == ["healthy", "healthy"]


def test_market_data_contract_active_stock_check_ignores_future_listings(monkeypatch) -> None:
    import pandas as pd

    captured_query = ""

    def fake_query_dataframe(query: str, params: tuple[str, str]) -> pd.DataFrame:
        nonlocal captured_query
        captured_query = query
        return pd.DataFrame()

    monkeypatch.setattr(data_health, "query_dataframe", fake_query_dataframe)

    data_health._query_market_data_contract_metrics("000001")

    assert "stock_ref.listed_date <= target.trade_date" in captured_query


def test_market_data_contract_excludes_b_shares_without_full_daily_provider_contract(monkeypatch) -> None:
    import pandas as pd

    captured_query = ""

    def fake_query_dataframe(query: str, params: object = None):
        nonlocal captured_query
        captured_query = query
        return pd.DataFrame([{}])

    monkeypatch.setattr(data_health, "query_dataframe", fake_query_dataframe)

    data_health._query_market_data_contract_metrics("000001")

    assert "stock_ref.market = 'SHSE' and left(stock_ref.code, 3) = '900'" in captured_query
    assert "stock_ref.market = 'SZSE' and left(stock_ref.code, 3) = '200'" in captured_query


def test_market_data_contract_uses_canonical_concept_membership_snapshot(monkeypatch) -> None:
    import pandas as pd

    captured_query = ""

    def fake_query_dataframe(query: str, params: object = None):
        nonlocal captured_query
        captured_query = query
        return pd.DataFrame([{}])

    monkeypatch.setattr(data_health, "query_dataframe", fake_query_dataframe)

    data_health._query_market_data_contract_metrics("000001")

    assert "concept_snapshot_dates as" in captured_query
    assert "join concept_snapshot_dates snapshot" in captured_query
    assert "snapshot.valid_from = membership.valid_from" in captured_query


def test_market_data_contract_core_fields_do_not_reject_source_confirmed_suspensions(monkeypatch) -> None:
    import pandas as pd

    captured_query = ""

    def fake_query_dataframe(query: str, params: tuple[str, str]) -> pd.DataFrame:
        nonlocal captured_query
        captured_query = query
        return pd.DataFrame()

    monkeypatch.setattr(data_health, "query_dataframe", fake_query_dataframe)

    data_health._query_market_data_contract_metrics("000001")

    assert "stock_rows.trade_date = target.trade_date and not coalesce(stock_rows.is_suspended, false)" in captured_query


def test_market_data_contract_latest_date_check_ignores_unclosed_partial_rows(monkeypatch) -> None:
    import pandas as pd

    captured_query = ""

    def fake_query_dataframe(query: str, params: tuple[str, str]) -> pd.DataFrame:
        nonlocal captured_query
        captured_query = query
        return pd.DataFrame()

    monkeypatch.setattr(data_health, "query_dataframe", fake_query_dataframe)

    data_health._query_market_data_contract_metrics("000001")

    assert "where index_rows.trade_date <= target.trade_date" in captured_query
    assert "where stock_rows.trade_date <= target.trade_date" in captured_query


def _empty_frame():
    import pandas as pd

    return pd.DataFrame()
