from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Iterator

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from quotemux.infra.db.client import query_dataframe
from services.market_data_version import current_market_data_version


DATASET_ID = "stock_daily_1d"
DATASET_VERSION_RE = re.compile(r"^mhd-v1-[0-9a-f]{64}$")
MARKET_VERSION_RE = re.compile(r"^mhf-v1-[0-9a-f]{64}$")


def export_root() -> Path:
    return Path(os.getenv("MARKETHUB_EXPORT_ROOT", "/data/MarketHub2/exports")).expanduser().resolve()


def _dataset_root(dataset_version: str) -> Path:
    if DATASET_VERSION_RE.fullmatch(dataset_version) is None:
        raise HTTPException(status_code=404, detail={"code": "EXPORT_NOT_FOUND", "message": "数据集版本不存在"})
    root = export_root() / DATASET_ID / dataset_version
    resolved = root.resolve()
    expected_parent = (export_root() / DATASET_ID).resolve()
    if resolved.parent != expected_parent or not resolved.is_dir():
        raise HTTPException(status_code=404, detail={"code": "EXPORT_NOT_FOUND", "message": "数据集版本不存在"})
    return resolved


def _current_dataset_version() -> str:
    frame = query_dataframe(
        "select baseline_id,generation from audit.dataset_version_state where dataset_id=%s",
        (DATASET_ID,),
    )
    if len(frame.index) != 1:
        return ""
    baseline_id = str(frame.iloc[0].get("baseline_id", "") or "")
    generation = int(frame.iloc[0].get("generation", 0) or 0)
    if baseline_id == "" or generation < 1:
        return ""
    payload = {
        "contract": "markethub-dataset-v1",
        "dataset_id": DATASET_ID,
        "baseline_id": baseline_id,
        "generation": generation,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return f"mhd-v1-{hashlib.sha256(encoded).hexdigest()}"


def _current_published_mapping(market_data_version: str) -> dict[str, str] | None:
    if current_market_data_version() != market_data_version:
        return None
    dataset_version = _current_dataset_version()
    if dataset_version == "":
        return None
    try:
        manifest_path = _dataset_root(dataset_version) / "manifest.json"
        read_manifest(dataset_version)
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    except HTTPException:
        return None
    if current_market_data_version() != market_data_version or _current_dataset_version() != dataset_version:
        return None
    return {
        "dataset_id": DATASET_ID,
        "market_data_version": market_data_version,
        "dataset_version": dataset_version,
        "manifest_sha256": manifest_sha256,
        "manifest_url": f"/api/exports/{DATASET_ID}/{dataset_version}/manifest",
    }


def resolve_market_version(market_data_version: str) -> dict[str, str]:
    if MARKET_VERSION_RE.fullmatch(market_data_version) is None:
        raise HTTPException(status_code=404, detail={"code": "EXPORT_NOT_FOUND", "message": "市场版本没有发布映射"})
    frame = query_dataframe(
        "select dataset_version,manifest_sha256,relative_root from audit.dataset_version_publication where dataset_id=%s and market_data_version=%s",
        (DATASET_ID, market_data_version),
    )
    if len(frame.index) != 1:
        current_mapping = _current_published_mapping(market_data_version)
        if current_mapping is not None:
            return current_mapping
        raise HTTPException(status_code=404, detail={"code": "EXPORT_NOT_FOUND", "message": "市场版本没有发布映射"})
    row = frame.iloc[0]
    return {
        "dataset_id": DATASET_ID,
        "market_data_version": market_data_version,
        "dataset_version": str(row["dataset_version"]),
        "manifest_sha256": str(row["manifest_sha256"]),
        "manifest_url": f"/api/exports/{DATASET_ID}/{row['dataset_version']}/manifest",
    }


def read_manifest(dataset_version: str) -> dict[str, object]:
    path = _dataset_root(dataset_version) / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail={"code": "EXPORT_MANIFEST_INVALID", "message": "发布清单不可读"}) from exc
    if payload.get("dataset_version") != dataset_version or payload.get("dataset_id") != DATASET_ID:
        raise HTTPException(status_code=503, detail={"code": "EXPORT_MANIFEST_INVALID", "message": "发布清单版本不匹配"})
    return payload


def _allowed_file(dataset_version: str, relative_path: str) -> Path:
    manifest = read_manifest(dataset_version)
    allowed = {str(item.get("path", "")) for item in manifest.get("files", []) if isinstance(item, dict)}
    if relative_path not in allowed:
        raise HTTPException(status_code=404, detail={"code": "EXPORT_FILE_NOT_FOUND", "message": "导出文件不存在"})
    root = _dataset_root(dataset_version)
    resolved = (root / relative_path).resolve()
    if resolved == root or root not in resolved.parents or not resolved.is_file():
        raise HTTPException(status_code=404, detail={"code": "EXPORT_FILE_NOT_FOUND", "message": "导出文件不存在"})
    return resolved


def _chunks(path: Path, start: int, length: int) -> Iterator[bytes]:
    remaining = length
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            data = handle.read(min(1024 * 1024, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def file_response(dataset_version: str, relative_path: str, request: Request) -> StreamingResponse:
    path = _allowed_file(dataset_version, relative_path)
    size = path.stat().st_size
    start, end, status = 0, size - 1, 200
    range_header = request.headers.get("range", "")
    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if match is None or (match.group(1) == "" and match.group(2) == ""):
            raise HTTPException(status_code=416, detail={"code": "EXPORT_RANGE_INVALID", "message": "仅支持单一 bytes Range"})
        if match.group(1) == "":
            suffix = int(match.group(2))
            if suffix <= 0 or size == 0:
                raise HTTPException(status_code=416, detail={"code": "EXPORT_RANGE_INVALID", "message": "Range 超出文件范围"})
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(match.group(1))
            if match.group(2) != "":
                end = int(match.group(2))
        if start >= size or start > end:
            raise HTTPException(status_code=416, detail={"code": "EXPORT_RANGE_INVALID", "message": "Range 超出文件范围"})
        end = min(end, size - 1)
        status = 206
    length = max(0, end - start + 1)
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length), "ETag": f'"{path.name}-{size}"'}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(_chunks(path, start, length), status_code=status, media_type="application/vnd.apache.parquet", headers=headers)
