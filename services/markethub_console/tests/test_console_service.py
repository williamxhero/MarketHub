from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient


API_ROOT = Path(__file__).resolve().parents[2] / "markethub_api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from runtime_paths import configure_python_path


configure_python_path()

from app import app


client = TestClient(app)


def test_console_index_serves_management_workspace() -> None:
    response = client.get("/admin")

    assert response.status_code == 200
    assert "MarketHub Console" in response.text
    assert "Source Packages" in response.text
    assert "Capability Matrix" in response.text
    assert "/api/admin/source-packages" in response.text
    assert "DOMContentLoaded" in response.text


def test_console_contains_capability_editor_modal() -> None:
    response = client.get("/admin")

    assert response.status_code == 200
    assert "id=\"capability-editor\"" in response.text
    assert "data-capability-edit" in response.text
    assert "/api/admin/capability-settings/" in response.text
    assert "/api/admin/capture-policies/" in response.text
    assert "Merge Strategy" in response.text
    assert "TTL 缓存（天）" in response.text
    assert "id=\"cache-ttl-days\"" in response.text
    assert "id=\"cache-permanent\"" in response.text
    assert "永久缓存" in response.text
    assert "永久保存" in response.text
    assert "markethub.console.manual_permanent." in response.text
    assert "restoreManualPermanent" in response.text
    assert "DEFAULT_TTL_DAYS = 365" in response.text
    assert "CACHE_NEVER_EXPIRE_TTL_DAYS = -1" in response.text
    assert "ttl_days" in response.text
    assert "定时更新" in response.text
    assert "无缓存" not in response.text
    assert ">无</label>" in response.text
    assert "不更新" not in response.text
    assert "api-badges" in response.text
    assert "renderApiDocLinks" in response.text
    assert '<a href="${href}">${path}</a>' in response.text
    assert "renderCapabilityBadges" in response.text
    assert "renderCadenceBadge" in response.text
    assert "ttlLabelFromSeconds" in response.text
    assert "cadenceLabel" in response.text
    assert "存 ${escapeHtml(ttlLabel)}" in response.text
    assert "停" not in response.text
    assert "capture-schedule-option" in response.text
    assert "每月最后一天" in response.text
    assert "<th>Edit</th>" in response.text
    assert "<th>Merge Strategy</th>" not in response.text
    assert 'document.getElementById("close-capability-editor").addEventListener("click", closeCapabilityEditor)' in response.text
    assert 'document.getElementById("close-capability-editor-top").addEventListener("click", closeCapabilityEditor)' in response.text
    assert 'capabilityEditor.addEventListener("click"' not in response.text


def test_console_keeps_package_import_and_matrix_selection_flow() -> None:
    response = client.get("/admin")

    assert response.status_code == 200
    assert "id=\"directory-input\"" in response.text
    assert "webkitdirectory" in response.text
    assert "showDirectoryPicker" in response.text
    assert "shouldIgnorePackagePath" in response.text
    assert "__pycache__" in response.text
    assert "/api/admin/source-packages/import-directory" in response.text
    assert "请输入 source package 目录路径" not in response.text
    assert "/api/admin/capability-matrix" in response.text
    assert "enabled_package_ids" in response.text
    assert "type=\"checkbox\"" in response.text
    assert "data-source-toggle" in response.text
    assert "syncMatrixSourceToggles" in response.text
    assert "setMatrixSourceSelection" in response.text
    assert "save-capability-settings" in response.text
    assert "captureSchedulePayload" in response.text
    assert "Cache Read" not in response.text
    assert "Cache Write" not in response.text
    assert "cache_enabled" not in response.text


def test_console_config_uses_admin_api_base_url(monkeypatch) -> None:
    monkeypatch.setenv("MARKETHUB_ADMIN_API_BASE_URL", "http://markethub-api:8000")

    response = client.get("/api/console/config")

    assert response.status_code == 200
    assert response.json()["admin_api_base_url"] == "http://markethub-api:8000"
