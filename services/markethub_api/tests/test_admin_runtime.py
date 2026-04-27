from __future__ import annotations

import sys
import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from runtime_paths import configure_python_path


configure_python_path()

from app import app
from quotemux.config_runtime import reset_config_runtime_cache
from services import admin_runtime


client = TestClient(app)


def _auth_headers() -> dict[str, str]:
    return {}


def _configure_admin_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QUOTEMUX_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("MARKETHUB_PROJECT_ROOT", str(tmp_path / "markethub"))
    reset_config_runtime_cache()


def test_admin_api_allows_console_cors_preflight(monkeypatch, tmp_path) -> None:
    _configure_admin_runtime(monkeypatch, tmp_path)

    response = client.options(
        "/api/admin/source-packages",
        headers={
            "Origin": "http://127.0.0.1:8803",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:8803"


def test_admin_does_not_require_token(monkeypatch, tmp_path) -> None:
    _configure_admin_runtime(monkeypatch, tmp_path)

    read_response = client.get("/api/admin/source-packages")
    write_response = client.post("/api/admin/source-packages/refresh")

    assert read_response.status_code == 200
    assert write_response.status_code == 200


def test_admin_source_packages_and_instances(monkeypatch, tmp_path) -> None:
    _configure_admin_runtime(monkeypatch, tmp_path)

    packages_response = client.get("/api/admin/source-packages", headers=_auth_headers())
    assert packages_response.status_code == 200
    packages = packages_response.json()
    assert any(item["package_id"] == "datalake" for item in packages)
    datalake_package = next(item for item in packages if item["package_id"] == "datalake")
    assert datalake_package["health"]["status"] == "ok"

    health_response = client.get("/api/admin/source-packages/datalake/health", headers=_auth_headers())
    assert health_response.status_code == 200
    assert health_response.json()["handler_count"] > 0

    create_response = client.post(
        "/api/admin/source-instances",
        headers=_auth_headers(),
        json={
            "instance_id": "efinance-backup",
            "package_id": "efinance",
            "display_name": "EFinance 备用",
            "enabled": True,
            "priority": 88,
            "config_values": {"timeout_seconds": "5"},
            "secret_values": {},
            "tags": ["backup"],
        },
    )
    assert create_response.status_code == 200
    assert create_response.json()["instance_id"] == "efinance-backup"

    disable_response = client.post(
        "/api/admin/source-instances/efinance-backup/enabled",
        headers=_auth_headers(),
        json={"enabled": False},
    )
    assert disable_response.status_code == 200
    assert disable_response.json()["enabled"] is False

    list_response = client.get("/api/admin/source-instances", headers=_auth_headers())
    assert list_response.status_code == 200
    instances = list_response.json()
    assert any(item["instance_id"] == "efinance-backup" for item in instances)
    datalake_instance = next(item for item in instances if item["package_id"] == "datalake")
    assert datalake_instance["secret_values"] == {"db_password": "***"}


def test_admin_unknown_resource_and_validation_errors(monkeypatch, tmp_path) -> None:
    _configure_admin_runtime(monkeypatch, tmp_path)

    unknown_response = client.get("/api/admin/source-packages/not-found", headers=_auth_headers())
    invalid_instance_response = client.post(
        "/api/admin/source-instances",
        headers=_auth_headers(),
        json={
            "instance_id": "bad-default",
            "package_id": "not-found",
            "display_name": "Bad",
            "enabled": True,
            "priority": 1,
            "config_values": {},
            "secret_values": {},
            "tags": [],
        },
    )

    assert unknown_response.status_code == 404
    assert unknown_response.json()["code"] == "UNKNOWN_RESOURCE"
    assert invalid_instance_response.status_code == 422
    assert invalid_instance_response.json()["code"] == "VALIDATION_FAILED"


def test_admin_profiles_and_policies(monkeypatch, tmp_path) -> None:
    _configure_admin_runtime(monkeypatch, tmp_path)

    profiles_response = client.get("/api/admin/runtime-profiles", headers=_auth_headers())
    assert profiles_response.status_code == 200
    default_profile_id = profiles_response.json()[0]["profile_id"]

    policy_response = client.put(
        "/api/admin/contract-policies/stocks.quotes.daily",
        headers=_auth_headers(),
        json={"mode": "auto", "source_order": ["efinance-default", "tushare-default", "mootdx-default", "akshare-default"]},
    )
    assert policy_response.status_code == 200
    assert policy_response.json()["source_order"][0] == "efinance-default"

    validate_response = client.get("/api/admin/runtime-profiles/draft/validate", headers=_auth_headers())
    diff_response = client.get("/api/admin/runtime-profiles/draft/diff", headers=_auth_headers())
    assert validate_response.status_code == 200
    assert validate_response.json()["valid"] is True
    assert diff_response.status_code == 200
    assert "stocks.quotes.daily" in diff_response.json()["policy_changes"]

    publish_response = client.post(
        "/api/admin/runtime-profiles/publish",
        headers=_auth_headers(),
        json={"display_name": "测试发布", "note": "回归测试发布"},
    )
    assert publish_response.status_code == 200
    published_profile = publish_response.json()
    assert published_profile["display_name"] == "测试发布"

    report_response = client.get("/api/admin/runtime-report", headers=_auth_headers())
    assert report_response.status_code == 200
    report_payload = report_response.json()
    assert report_payload["active_profile"] == published_profile["profile_id"]
    assert "static_core" in report_payload["enabled_packages"]
    assert "datalake" not in report_payload["enabled_packages"]
    assert any(item["package_id"] == "static_core" for item in report_payload["package_health"])
    assert report_payload["source_instance_page"]["limit"] == 100

    filtered_report_response = client.get(
        "/api/admin/runtime-report?contract_name=stocks.quotes.daily&package_id=efinance&source_instance_id=efinance-default&limit=5",
        headers=_auth_headers(),
    )
    assert filtered_report_response.status_code == 200
    assert filtered_report_response.json()["source_instance_page"]["limit"] == 5

    health_response = client.get("/api/admin/runtime-health", headers=_auth_headers())
    assert health_response.status_code == 200
    assert any(item["source_instance_id"] == "static_core-default" for item in health_response.json()["source_instances"])

    rollback_response = client.post(f"/api/admin/runtime-profiles/{default_profile_id}/rollback", headers=_auth_headers())
    assert rollback_response.status_code == 200
    assert rollback_response.json()["profile_id"] == default_profile_id
    rollback_report_response = client.get("/api/admin/runtime-report", headers=_auth_headers())
    assert rollback_report_response.status_code == 200
    assert rollback_report_response.json()["profile_transitions"][-1]["to_profile_id"] == default_profile_id


def test_admin_unregister_source_package_hides_registration(monkeypatch, tmp_path) -> None:
    _configure_admin_runtime(monkeypatch, tmp_path)

    delete_response = client.delete("/api/admin/source-packages/akshare", headers=_auth_headers())
    assert delete_response.status_code == 200
    assert delete_response.json()["unregistered"] is True

    packages_response = client.get("/api/admin/source-packages", headers=_auth_headers())
    assert packages_response.status_code == 200
    assert all(item["package_id"] != "akshare" for item in packages_response.json())

    detail_response = client.get("/api/admin/source-packages/akshare", headers=_auth_headers())
    assert detail_response.status_code == 404


def test_admin_import_directory_restores_builtin_package_by_directory_name(monkeypatch, tmp_path) -> None:
    _configure_admin_runtime(monkeypatch, tmp_path)

    delete_response = client.delete("/api/admin/source-packages/akshare", headers=_auth_headers())
    assert delete_response.status_code == 200

    content = base64.b64encode(b"# placeholder").decode("ascii")
    import_response = client.post(
        "/api/admin/source-packages/import-directory",
        headers=_auth_headers(),
        json={"files": [{"path": "akshare/source.py", "content_base64": content}]},
    )

    assert import_response.status_code == 200
    assert any(item["package_id"] == "akshare" for item in import_response.json()["packages"])
    matrix_response = client.get("/api/admin/capability-matrix", headers=_auth_headers())
    assert matrix_response.status_code == 200
    daily_row = next(item for item in matrix_response.json()["capabilities"] if item["capability_id"] == "stocks.quotes.daily")
    daily_packages = {item["package_id"]: item for item in daily_row["packages"]}
    assert daily_packages["akshare"]["enabled"] is True


def test_admin_contract_matrix_reads_and_saves_package_selection(monkeypatch, tmp_path) -> None:
    _configure_admin_runtime(monkeypatch, tmp_path)

    matrix_response = client.get("/api/admin/capability-matrix", headers=_auth_headers())
    assert matrix_response.status_code == 200
    matrix = matrix_response.json()
    package_ids = [item["package_id"] for item in matrix["packages"]]
    assert "tushare" in package_ids
    finance_row = next(item for item in matrix["capabilities"] if item["capability_id"] == "stocks.finance.statements")
    finance_packages = {item["package_id"]: item for item in finance_row["packages"]}
    assert finance_packages["tushare"]["supported"] is True
    assert finance_packages["efinance"]["supported"] is False
    news_row = next(item for item in matrix["capabilities"] if item["capability_id"] == "markets.events.news")
    news_packages = {item["package_id"]: item for item in news_row["packages"]}
    assert news_row["capability_id"] == "markets.events.news"
    assert news_row["policy_managed"] is True
    assert news_row["result_shape"] == "event_stream"
    assert "append_dedupe" in news_row["allowed_merge_strategies"]
    assert news_packages["news_store"]["supported"] is True
    assert news_packages["news_store"]["enabled"] is True
    assert news_packages["datalake"]["enabled"] is False

    save_response = client.put(
        "/api/admin/capability-matrix",
        headers=_auth_headers(),
        json={"contracts": [{"capability_id": "stocks.quotes.daily", "enabled_package_ids": ["static_core", "tushare"], "merge_strategy": "append_dedupe"}]},
    )
    assert save_response.status_code == 200
    daily_row = next(item for item in save_response.json()["capabilities"] if item["capability_id"] == "stocks.quotes.daily")
    daily_packages = {item["package_id"]: item for item in daily_row["packages"]}
    assert daily_packages["static_core"]["enabled"] is True
    assert daily_packages["tushare"]["enabled"] is True
    assert daily_packages["efinance"]["enabled"] is False
    assert daily_row["merge_strategy"] == "append_dedupe"


def test_admin_capture_management(monkeypatch, tmp_path) -> None:
    class FakeCaptureAdmin:
        def __init__(self) -> None:
            self.policy = {
                "capability_id": "stocks.quotes.daily",
                "enabled": False,
                "cadence": "daily",
                "run_time": "18:00:00",
                "timezone": "Asia/Shanghai",
                "weekday": None,
                "month": None,
                "month_day": None,
                "scope_profile": "active_stocks_recent_trading_days",
                "scope_profile_label": "活跃股票最近交易日",
                "window_count": 30,
                "batch_size": 100,
                "notes": "",
            }

        def list_policies(self):
            return (self.policy,)

        def list_overview(self):
            return ({**self.policy, "latest_run": {}},)

        def get_policy(self, capability_id: str):
            return self.policy

        def update_policy(self, payload):
            self.policy = {
                **self.policy,
                "enabled": payload.enabled,
                "cadence": payload.cadence,
                "run_time": payload.run_time.strftime("%H:%M:%S"),
                "timezone": payload.timezone,
                "weekday": payload.weekday,
                "month": payload.month,
                "month_day": payload.month_day,
                "scope_profile": payload.scope_profile,
                "window_count": payload.window_count,
                "batch_size": payload.batch_size,
                "notes": payload.notes,
            }
            return self.policy

        def list_runs(self, capability_id: str = "", status: str = "", limit: int = 100):
            return ({"capability_id": capability_id, "status": status, "limit": limit},)

        def run_capture(self, capability_id: str):
            return {"capability_id": capability_id, "status": "success"}

        def run_due_captures(self):
            return ({"capability_id": "stocks.quotes.daily", "status": "success"},)

    _configure_admin_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", FakeCaptureAdmin())

    policies_response = client.get("/api/admin/capture-policies", headers=_auth_headers())
    overview_response = client.get("/api/admin/capture-overview", headers=_auth_headers())
    detail_response = client.get("/api/admin/capture-policies/stocks.quotes.daily", headers=_auth_headers())
    update_response = client.put(
        "/api/admin/capture-policies/stocks.quotes.daily",
        headers=_auth_headers(),
        json={
            "enabled": True,
            "cadence": "daily",
            "run_time": "18:00:00",
            "timezone": "Asia/Shanghai",
            "weekday": None,
            "month": None,
            "month_day": None,
            "scope_profile": "active_stocks_recent_trading_days",
            "window_count": 5,
            "batch_size": 50,
            "notes": "测试",
        },
    )
    runs_response = client.get("/api/admin/capture-runs?capability_id=stocks.quotes.daily&status=success&limit=5", headers=_auth_headers())
    run_one_response = client.post("/api/admin/capture-runs/stocks.quotes.daily", headers=_auth_headers())
    run_due_response = client.post("/api/admin/capture/run-due", headers=_auth_headers())

    assert policies_response.status_code == 200
    assert policies_response.json()[0]["capability_id"] == "stocks.quotes.daily"
    assert overview_response.status_code == 200
    assert overview_response.json()[0]["latest_run"] == {}
    assert detail_response.status_code == 200
    assert update_response.status_code == 200
    assert update_response.json()["enabled"] is True
    assert update_response.json()["window_count"] == 5
    assert runs_response.status_code == 200
    assert runs_response.json()[0]["limit"] == 5
    assert run_one_response.status_code == 200
    assert run_one_response.json()["status"] == "success"
    assert run_due_response.status_code == 200
    assert run_due_response.json()[0]["capability_id"] == "stocks.quotes.daily"


def test_admin_import_uploaded_source_package_directory(monkeypatch, tmp_path) -> None:
    _configure_admin_runtime(monkeypatch, tmp_path)
    manifest = {
        "package_id": "demo_source",
        "version": "1.0.0",
        "source_name": "demo_source",
        "display_name": "Demo Source",
        "description": "测试导入目录 package。",
        "contract_names": ["stocks.quotes"],
        "capability_tags": ["test"],
        "config_schema": [],
        "secret_fields": [],
        "supports_multi_instance": True,
        "handler_targets": {"get_stock_quotes": "quotemux.sources.akshare.source:get_stock_quotes"},
    }
    content = base64.b64encode(json.dumps(manifest).encode("utf-8")).decode("ascii")

    response = client.post(
        "/api/admin/source-packages/import-directory",
        headers=_auth_headers(),
        json={"files": [{"path": "demo_source/quotemux_package.json", "content_base64": content}]},
    )

    assert response.status_code == 200
    assert any(item["package_id"] == "demo_source" for item in response.json()["packages"])


def test_console_entry_page() -> None:
    response = client.get("/console", follow_redirects=False)
    assert response.status_code in {307, 308}
    assert response.headers["location"] == "/admin"

    admin_response = client.get("/admin")
    assert admin_response.status_code == 200
    assert "MarketHub Console" in admin_response.text

    config_response = client.get("/api/console/config")
    assert config_response.status_code == 200
    assert config_response.json()["admin_api_base_url"] == ""
