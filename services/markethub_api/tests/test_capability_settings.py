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
from quotemux.capabilities import DERIVED_CAPABILITY_BASE_IDS, get_capability_config_root, is_independently_configurable_capability_id
from quotemux.config_runtime import reset_config_runtime_cache
from quotemux.contracts.policies import get_contract_policy
from quotemux.store.default_update_policy import get_capability_update_policy_default, ttl_seconds_from_days
from services import admin_runtime


client = TestClient(app)
TMP_ROOT = Path(__file__).resolve().parents[3] / ".tmp" / "capability_settings_tests"
STOCK_DAILY_POLICY_DEFAULT = get_capability_update_policy_default("stocks.quotes.daily")
STOCK_INTRADAY_POLICY_DEFAULT = get_capability_update_policy_default("stocks.quotes.intraday")
TRADING_CALENDAR_POLICY_DEFAULT = get_capability_update_policy_default("markets.calendar.trading")


class FakeCacheAdmin:
    def __init__(self) -> None:
        self._policies: dict[str, dict[str, object]] = {}

    def get_policy(self, capability_id: str) -> dict[str, object]:
        if capability_id not in self._policies:
            self._policies[capability_id] = admin_runtime._default_cache_policy_payload(capability_id)
        return dict(self._policies[capability_id])

    def update_policy(self, update) -> dict[str, object]:
        current = self.get_policy(update.capability_id)
        read_enabled = update.enabled if getattr(update, "read_enabled", None) is None else update.read_enabled
        write_enabled = update.enabled if getattr(update, "write_enabled", None) is None else update.write_enabled
        current.update(
            {
                "enabled": update.enabled,
                "read_enabled": read_enabled,
                "write_enabled": write_enabled,
                "ttl_seconds": update.ttl_seconds,
            }
        )
        self._policies[update.capability_id] = current
        return dict(current)


class FakeCaptureAdmin:
    def __init__(self, enabled: bool = False) -> None:
        self.policy: dict[str, object] = {
            "capability_id": "stocks.quotes.daily",
            "enabled": enabled,
            "cadence": "daily",
            "run_time": "00:00:00",
            "timezone": "Asia/Shanghai",
            "weekday": None,
            "month": None,
            "month_day": None,
            "scope_profile": "active_stocks_recent_trading_days",
            "window_count": 5,
            "batch_size": 50,
            "notes": "",
        }

    def list_policies(self) -> tuple[dict[str, object], ...]:
        return (dict(self.policy),)

    def get_policy(self, capability_id: str) -> dict[str, object]:
        return dict(self.policy | {"capability_id": capability_id})

    def update_policy(self, payload) -> dict[str, object]:
        self.policy = {
            "capability_id": payload.capability_id,
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
        return dict(self.policy)


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
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", FakeCaptureAdmin(enabled=False))

    response = client.get("/api/admin/capability-settings/stocks.quotes.daily")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_id"] == "stocks.quotes.daily"
    assert payload["merge_strategy"] != ""
    assert payload["ttl_days"] == STOCK_DAILY_POLICY_DEFAULT.cache_ttl_days
    assert payload["cache_effective"] is True
    assert payload["cache_policy"]["capability_id"] == "stocks.quotes.daily"
    assert payload["cache_policy"]["ttl_seconds"] == ttl_seconds_from_days(STOCK_DAILY_POLICY_DEFAULT.cache_ttl_days)
    assert payload["cache_policy"]["time_field"] != ""
    assert payload["cache_policy"]["key_fields"] != []


def test_capability_settings_reads_intraday_default_capture_cache(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "read_intraday_default")
    fake_cache_admin = FakeCacheAdmin()
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", fake_cache_admin)
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", FakeCaptureAdmin(enabled=True))

    response = client.get("/api/admin/capability-settings/stocks.quotes.intraday")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_id"] == "stocks.quotes.intraday"
    assert payload["ttl_days"] == STOCK_INTRADAY_POLICY_DEFAULT.cache_ttl_days
    assert payload["cache_effective"] is True
    assert payload["cache_policy"]["enabled"] is True
    assert payload["cache_policy"]["read_enabled"] is True
    assert payload["cache_policy"]["write_enabled"] is True
    assert payload["cache_policy"]["ttl_seconds"] == ttl_seconds_from_days(STOCK_INTRADAY_POLICY_DEFAULT.cache_ttl_days)


def test_capability_settings_updates_ttl_days_with_capture_disabled(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "update")
    fake_cache_admin = FakeCacheAdmin()
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", fake_cache_admin)
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", FakeCaptureAdmin(enabled=False))

    response = client.put(
        "/api/admin/capability-settings/stocks.quotes.daily",
        json={
            "merge_strategy": "append_dedupe",
            "ttl_days": 2,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_id"] == "stocks.quotes.daily"
    assert payload["merge_strategy"] == "append_dedupe"
    assert payload["ttl_days"] == 2
    assert payload["cache_effective"] is True
    assert payload["cache_policy"]["enabled"] is True
    assert payload["cache_policy"]["read_enabled"] is True
    assert payload["cache_policy"]["write_enabled"] is True
    assert payload["cache_policy"]["ttl_seconds"] == 172800


def test_capability_settings_ttl_zero_disables_cache(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "disable")
    fake_cache_admin = FakeCacheAdmin()
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", fake_cache_admin)
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", FakeCaptureAdmin(enabled=False))

    response = client.put(
        "/api/admin/capability-settings/stocks.quotes.daily",
        json={
            "merge_strategy": "append_dedupe",
            "ttl_days": 0,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_id"] == "stocks.quotes.daily"
    assert payload["ttl_days"] == 0
    assert payload["cache_effective"] is False
    assert payload["cache_policy"]["enabled"] is False
    assert payload["cache_policy"]["read_enabled"] is False
    assert payload["cache_policy"]["write_enabled"] is False
    assert payload["cache_policy"]["ttl_seconds"] == 0


def test_capability_settings_ttl_minus_one_never_expires(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "never_expire")
    fake_cache_admin = FakeCacheAdmin()
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", fake_cache_admin)
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", FakeCaptureAdmin(enabled=False))

    response = client.put(
        "/api/admin/capability-settings/stocks.quotes.daily",
        json={
            "merge_strategy": "append_dedupe",
            "ttl_days": -1,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ttl_days"] == -1
    assert payload["cache_effective"] is True
    assert payload["cache_policy"]["enabled"] is True
    assert payload["cache_policy"]["read_enabled"] is True
    assert payload["cache_policy"]["write_enabled"] is True
    assert payload["cache_policy"]["ttl_seconds"] == -1


def test_capability_settings_preserves_never_expire_when_legacy_cache_enabled(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "legacy_never_expire")
    fake_cache_admin = FakeCacheAdmin()
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", fake_cache_admin)
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", FakeCaptureAdmin(enabled=False))

    client.put(
        "/api/admin/capability-settings/stocks.quotes.daily",
        json={
            "merge_strategy": "append_dedupe",
            "ttl_days": -1,
        },
    )
    response = client.put(
        "/api/admin/capability-settings/stocks.quotes.daily",
        json={
            "merge_strategy": "append_dedupe",
            "cache_enabled": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ttl_days"] == -1
    assert payload["cache_effective"] is True
    assert payload["cache_policy"]["ttl_seconds"] == -1


def test_capability_settings_accepts_legacy_cache_enabled_input(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "legacy")
    fake_cache_admin = FakeCacheAdmin()
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", fake_cache_admin)
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", FakeCaptureAdmin(enabled=False))

    response = client.put(
        "/api/admin/capability-settings/stocks.quotes.daily",
        json={
            "merge_strategy": "append_dedupe",
            "cache_enabled": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert "cache_enabled" not in payload
    assert payload["ttl_days"] == 0
    assert payload["cache_effective"] is False


def test_capability_settings_legacy_cache_enabled_true_uses_default_ttl(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "legacy_enabled")
    fake_cache_admin = FakeCacheAdmin()
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", fake_cache_admin)
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", FakeCaptureAdmin(enabled=False))

    client.put(
        "/api/admin/capability-settings/stocks.quotes.daily",
        json={
            "merge_strategy": "append_dedupe",
            "ttl_days": 0,
        },
    )
    response = client.put(
        "/api/admin/capability-settings/stocks.quotes.daily",
        json={
            "merge_strategy": "append_dedupe",
            "cache_enabled": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ttl_days"] == STOCK_DAILY_POLICY_DEFAULT.cache_ttl_days
    assert payload["cache_effective"] is True
    assert payload["cache_policy"]["ttl_seconds"] == ttl_seconds_from_days(STOCK_DAILY_POLICY_DEFAULT.cache_ttl_days)


def test_capability_settings_preserves_ttl_and_keeps_cache_when_capture_enabled(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "capture_enabled")
    fake_cache_admin = FakeCacheAdmin()
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", fake_cache_admin)
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", FakeCaptureAdmin(enabled=True))

    response = client.put(
        "/api/admin/capability-settings/stocks.quotes.daily",
        json={
            "merge_strategy": "append_dedupe",
            "ttl_days": 2,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ttl_days"] == 2
    assert payload["cache_effective"] is True
    assert payload["cache_policy"]["enabled"] is True
    assert payload["cache_policy"]["read_enabled"] is True
    assert payload["cache_policy"]["write_enabled"] is True
    assert payload["cache_policy"]["ttl_seconds"] == 172800


def test_capture_policy_none_restores_cache_from_existing_ttl(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "restore")
    fake_cache_admin = FakeCacheAdmin()
    fake_capture_admin = FakeCaptureAdmin(enabled=True)
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", fake_cache_admin)
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", fake_capture_admin)

    client.put(
        "/api/admin/capability-settings/stocks.quotes.daily",
        json={
            "merge_strategy": "append_dedupe",
            "ttl_days": 2,
        },
    )
    response = client.put(
        "/api/admin/capture-policies/stocks.quotes.daily",
        json={
            "enabled": False,
            "cadence": "daily",
            "run_time": "00:00:00",
            "timezone": "Asia/Shanghai",
            "weekday": None,
            "month": None,
            "month_day": None,
            "scope_profile": "active_stocks_recent_trading_days",
            "window_count": 5,
            "batch_size": 50,
            "notes": "",
        },
    )
    settings_response = client.get("/api/admin/capability-settings/stocks.quotes.daily")

    assert response.status_code == 200
    payload = settings_response.json()
    assert payload["ttl_days"] == 2
    assert payload["cache_effective"] is True
    assert payload["cache_policy"]["enabled"] is True
    assert payload["cache_policy"]["ttl_seconds"] == 172800


def test_capture_policy_enables_cache_even_when_ttl_zero(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "capture_ttl_zero")
    fake_cache_admin = FakeCacheAdmin()
    fake_capture_admin = FakeCaptureAdmin(enabled=False)
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", fake_cache_admin)
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", fake_capture_admin)

    client.put(
        "/api/admin/capability-settings/stocks.quotes.daily",
        json={
            "merge_strategy": "append_dedupe",
            "ttl_days": 0,
        },
    )
    response = client.put(
        "/api/admin/capture-policies/stocks.quotes.daily",
        json={
            "enabled": True,
            "cadence": "daily",
            "run_time": "00:00:00",
            "timezone": "Asia/Shanghai",
            "weekday": None,
            "month": None,
            "month_day": None,
            "scope_profile": "active_stocks_recent_trading_days",
            "window_count": 5,
            "batch_size": 50,
            "notes": "",
        },
    )
    settings_response = client.get("/api/admin/capability-settings/stocks.quotes.daily")

    assert response.status_code == 200
    payload = settings_response.json()
    assert payload["ttl_days"] == 0
    assert payload["cache_effective"] is True
    assert payload["cache_policy"]["enabled"] is True
    assert payload["cache_policy"]["read_enabled"] is True
    assert payload["cache_policy"]["write_enabled"] is True


def test_derived_capability_roots_are_explicit() -> None:
    assert DERIVED_CAPABILITY_BASE_IDS["markets.calendar.trading.next"] == "markets.calendar.trading"
    assert get_capability_config_root("markets.calendar.trading.next") == "markets.calendar.trading"
    assert is_independently_configurable_capability_id("markets.calendar.trading.next") is False
    assert get_contract_policy("markets.calendar.trading.next").name == "markets.calendar.trading"
    assert get_capability_update_policy_default("markets.calendar.trading.next").capability_id == "markets.calendar.trading"
    assert get_capability_config_root("markets.calendar.trading.future") == "markets.calendar.trading.future"
    assert is_independently_configurable_capability_id("markets.calendar.trading.future") is True


def test_derived_calendar_settings_use_trading_calendar_root(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "derived_settings")
    fake_cache_admin = FakeCacheAdmin()
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", fake_cache_admin)
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", FakeCaptureAdmin(enabled=False))

    response = client.get("/api/admin/capability-settings/markets.calendar.trading.next")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_id"] == "markets.calendar.trading"
    assert payload["contract_name"] == "markets.calendar.trading"
    assert payload["ttl_days"] == TRADING_CALENDAR_POLICY_DEFAULT.cache_ttl_days
    assert payload["cache_policy"]["capability_id"] == "markets.calendar.trading"
    assert "markets.calendar.trading.next" not in fake_cache_admin._policies


def test_derived_calendar_capture_policy_uses_trading_calendar_root(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "derived_capture")
    fake_cache_admin = FakeCacheAdmin()
    fake_capture_admin = FakeCaptureAdmin(enabled=False)
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", fake_cache_admin)
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", fake_capture_admin)

    detail_response = client.get("/api/admin/capture-policies/markets.calendar.trading.previous")
    update_response = client.put(
        "/api/admin/capture-policies/markets.calendar.trading.previous",
        json={
            "enabled": True,
            "cadence": "monthly",
            "run_time": "00:00:00",
            "timezone": "Asia/Shanghai",
            "weekday": None,
            "month": None,
            "month_day": 31,
            "scope_profile": "trading_calendar_year_window",
            "window_count": 2,
            "batch_size": 1,
            "notes": "",
        },
    )

    assert detail_response.status_code == 200
    assert detail_response.json()["capability_id"] == "markets.calendar.trading"
    assert update_response.status_code == 200
    payload = update_response.json()
    assert payload["capability_id"] == "markets.calendar.trading"
    assert fake_capture_admin.policy["capability_id"] == "markets.calendar.trading"
    assert "markets.calendar.trading.previous" not in fake_cache_admin._policies


def test_capability_matrix_hides_derived_calendar_capabilities(monkeypatch) -> None:
    _configure_admin_runtime(monkeypatch, "derived_matrix")
    monkeypatch.setattr(admin_runtime, "_CACHE_ADMIN", FakeCacheAdmin())
    monkeypatch.setattr(admin_runtime, "_CAPTURE_ADMIN", FakeCaptureAdmin(enabled=False))

    response = client.get("/api/admin/capability-matrix")

    assert response.status_code == 200
    capability_ids = {item["capability_id"] for item in response.json()["capabilities"]}
    assert "markets.calendar.trading" in capability_ids
    assert "markets.calendar.trading.next" not in capability_ids
    assert "markets.calendar.trading.previous" not in capability_ids
    assert "markets.calendar.trading.yearly" not in capability_ids
