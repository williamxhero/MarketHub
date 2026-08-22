from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.exports import router
from services import exports


def _app(tmp_path: Path, monkeypatch) -> tuple[TestClient, str, bytes]:
    version = "mhd-v1-" + "a" * 64
    root = tmp_path / "stock_daily_1d" / version
    file_path = root / "year=2021" / "month=01" / "bars.parquet"
    file_path.parent.mkdir(parents=True)
    content = b"0123456789"
    file_path.write_bytes(content)
    manifest = {
        "dataset_id": "stock_daily_1d", "dataset_version": version,
        "files": [{"path": "year=2021/month=01/bars.parquet", "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("MARKETHUB_EXPORT_ROOT", str(tmp_path))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), version, content


def test_manifest_and_single_range(monkeypatch, tmp_path: Path) -> None:
    client, version, content = _app(tmp_path, monkeypatch)
    assert client.get(f"/api/exports/stock_daily_1d/{version}/manifest").status_code == 200
    response = client.get(
        f"/api/exports/stock_daily_1d/{version}/files/year=2021/month=01/bars.parquet",
        headers={"Range": "bytes=2-5"},
    )
    assert response.status_code == 206
    assert response.content == content[2:6]
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["accept-ranges"] == "bytes"
    suffix = client.get(
        f"/api/exports/stock_daily_1d/{version}/files/year=2021/month=01/bars.parquet",
        headers={"Range": "bytes=-4"},
    )
    assert suffix.status_code == 206
    assert suffix.content == content[-4:]
    assert suffix.headers["content-range"] == "bytes 6-9/10"


def test_manifest_allowlist_rejects_traversal(monkeypatch, tmp_path: Path) -> None:
    client, version, _ = _app(tmp_path, monkeypatch)
    response = client.get(f"/api/exports/stock_daily_1d/{version}/files/../manifest.json")
    assert response.status_code == 404
