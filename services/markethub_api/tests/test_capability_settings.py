from __future__ import annotations

import sys
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
TMP_ROOT = Path(__file__).resolve().parents[3] / ".tmp" / "capability_settings_tests"


class FakeCacheAdmin:
    def __init__(self) -> None:
        self._policies: dict[str, dict[str, object]] = {}

    def get_policy(self, capability_id: str) -> dict[str, object]:
        if capability_id not in self._policies:
            self._policies[capability_id] = admin_runtime._default_cache_policy_payload(capability_id)
        return dict(self._policies[capability_id])

    def update_policy(self, update) -> dict[str, object]:
        current = self.get_policy(update.capability_id)
        current.update(
            {
                "enabled": update.enabled,
                "read_enabled": update.read_enabled,
                "write_enabled": update.write_enabled,
                "ttl_seconds": update.ttl_seconds,
            }
        )
        self._policies[update.capability_id] = current
        return dict(current)


def _configure_admin_runtime(monkeypatch, case_name: str) -> None:
    case_root = TMP_ROOT / case_name
    runtime_root = case_root / "runtime"
    project_root = case_root / "markethub"
    runtime_root.mkdir(parents=True, exist_ok=True)
    project_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("QUOTEMUX_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("MARKETHUB_PROJECT_ROOT", str(project_root))
    monkeypatch.setattr(admin_runtime, "record_provider_event", lambda *args, **kwargs: None)
    reset_config_runtime_cache()


def test_capability_settings_reads_current_merge_and_cache(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "read")
    fake_cache_admin = FakeCacheAdmin()
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", fake_cache_admin)

    response = client.get("/api/admin/capability-settings/stocks.quotes.daily")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_id"] == "stocks.quotes.daily"
    assert payload["merge_strategy"] != ""
    assert payload["cache_policy"]["capability_id"] == "stocks.quotes.daily"
    assert payload["cache_policy"]["time_field"] != ""
    assert payload["cache_policy"]["key_fields"] != []


def test_capability_settings_updates_merge_and_cache(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "update")
    fake_cache_admin = FakeCacheAdmin()
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", fake_cache_admin)

    response = client.put(
        "/api/admin/capability-settings/stocks.quotes.daily",
        json={
            "merge_strategy": "append_dedupe",
            "cache_enabled": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_id"] == "stocks.quotes.daily"
    assert payload["merge_strategy"] == "append_dedupe"
    assert payload["cache_policy"]["enabled"] is True
    assert payload["cache_policy"]["read_enabled"] is True
    assert payload["cache_policy"]["write_enabled"] is True
    assert payload["cache_policy"]["ttl_seconds"] > 0
