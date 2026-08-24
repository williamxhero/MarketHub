from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException

from quotemux.infra.db.client import query_dataframe
from services.market_data_version import require_market_data_version


DATASET_IDS = (
    "stock_reference",
    "stock_daily_1d",
    "stock_bar_1m",
    "stock_bar_5m",
    "stock_bar_30m",
    "future_bar_1m",
    "future_contract_reference",
    "concept_daily_1d",
    "stock_research_daily",
)
STOCK_DAILY_DATASET_ID = "stock_daily_1d"
READ_MODEL_DATASET_IDS = frozenset(("stock_daily_1d", "stock_bar_1m", "future_bar_1m"))
_CAPABILITY_DATASET_IDS = {
    "futures.quotes.back_adjusted_continuous.1m": "future_bar_1m",
    "futures.quotes.main_continuous.1m": "future_bar_1m",
}
PUBLICATION_GATED_DATASET_IDS = READ_MODEL_DATASET_IDS | frozenset(("future_contract_reference",))
VERSION_CONTRACT = "markethub-dataset-vector-v1"
ROUTE_DATASET_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "/api/stocks/catalog": ("stock_reference",),
    "/api/stocks/{code}/profile/basic": ("stock_reference",),
    "/api/stocks/{code}/profile": ("stock_reference",),
    "/api/stocks/quotes/query:1m": ("stock_reference", "stock_bar_1m"),
    "/api/stocks/quotes/daily-window/query": ("stock_reference", "stock_daily_1d"),
    "/api/stocks/quotes/daily-snapshot": ("stock_daily_1d",),
    "/api/stocks/quotes/daily-local-window": ("stock_daily_1d",),
    "/api/futures/coverage": ("future_bar_1m",),
    "/api/futures/quotes/1m": ("future_bar_1m",),
    "/api/futures/contracts": ("future_contract_reference",),
    "/api/exports/stock_daily_1d/{dataset_version}/manifest": ("stock_daily_1d",),
    "/api/exports/stock_daily_1d/{dataset_version}/files/{relative_path}": ("stock_daily_1d",),
}


def dataset_version_from_state(dataset_id: str, baseline_id: str, generation: int) -> str:
    if dataset_id not in DATASET_IDS or baseline_id == "" or generation < 1:
        return ""
    payload = {"contract": "markethub-dataset-v1", "dataset_id": dataset_id, "baseline_id": baseline_id, "generation": generation}
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return f"mhd-v1-{hashlib.sha256(encoded).hexdigest()}"


def current_dataset_versions() -> dict[str, str]:
    frame = query_dataframe(
        "select dataset_id,baseline_id,generation from audit.dataset_version_state "
        "where dataset_id=any(%s::text[]) order by dataset_id",
        (list(DATASET_IDS),),
    )
    versions = {
        str(row["dataset_id"]): dataset_version_from_state(
            str(row["dataset_id"]),
            str(row.get("baseline_id", "") or ""),
            int(row.get("generation", 0) or 0),
        )
        for _, row in frame.iterrows()
    }
    return {dataset_id: versions.get(dataset_id, "") for dataset_id in DATASET_IDS}


def current_dataset_version(dataset_id: str) -> str:
    if dataset_id not in DATASET_IDS:
        return ""
    return current_dataset_versions().get(dataset_id, "")


def current_stock_daily_dataset_version() -> str:
    return current_dataset_version(STOCK_DAILY_DATASET_ID)


def current_dataset_publications(versions: dict[str, str] | None = None) -> dict[str, dict[str, object]]:
    versions = current_dataset_versions() if versions is None else versions
    publications = {
        dataset_id: {
            "status": "not_ready" if dataset_id in PUBLICATION_GATED_DATASET_IDS else "online",
            "dataset_version": version,
        }
        for dataset_id, version in versions.items()
    }
    try:
        frame = query_dataframe(
            "select dataset_id,dataset_version,status,built_at_utc,error_message "
            "from readmodel.dataset_build_state "
            "where dataset_id=any(%s::text[]) and dataset_version=any(%s::text[])",
            (list(versions), list(versions.values())),
        )
    except Exception:
        return publications
    for _, row in frame.iterrows():
        dataset_id = str(row["dataset_id"])
        if versions.get(dataset_id) != str(row["dataset_version"]):
            continue
        publications[dataset_id] = {
            "status": str(row["status"]),
            "dataset_version": str(row["dataset_version"]),
            "built_at_utc": str(row.get("built_at_utc", "") or ""),
            "error": str(row.get("error_message", "") or ""),
        }
    return publications


def require_current(capability_id: str, expected_version: str) -> str:
    """Strict transaction-time gate for a QuoteMux staged publisher.

    This deliberately consumes the one MarketHub dataset registry and its
    published read-model state.  It is not a second capability-version map.
    QuoteMux must invoke it while holding its repair transaction lock and
    before it stages any fact mutation.
    """
    dataset_id = _CAPABILITY_DATASET_IDS.get(capability_id)
    if dataset_id is None:
        raise RuntimeError(f"capability has no MarketHub immutable dataset registry: {capability_id}")
    if expected_version == "":
        raise RuntimeError(f"expected immutable dataset version is required: {capability_id}")
    current_version = current_dataset_version(dataset_id)
    if current_version == "" or expected_version != current_version:
        raise RuntimeError(
            f"stale immutable dataset version for {capability_id}: expected={expected_version} current={current_version}"
        )
    publication = current_dataset_publications({dataset_id: current_version}).get(dataset_id, {})
    if publication.get("dataset_version") != current_version or publication.get("status") != "online":
        raise RuntimeError(
            f"immutable dataset is not online for {capability_id}: "
            f"version={current_version} status={publication.get('status', 'unknown')}"
        )
    return current_version


def require_dataset_version(
    dataset_id: str,
    requested_dataset_version: str = "",
    requested_market_version: str = "",
) -> str:
    if dataset_id not in DATASET_IDS:
        raise HTTPException(status_code=500, detail={"code": "DATASET_ID_UNKNOWN", "message": f"未注册数据集: {dataset_id}"})
    if requested_market_version:
        require_market_data_version(requested_market_version)
    actual_version = current_dataset_version(dataset_id)
    if actual_version == "":
        raise HTTPException(
            status_code=503,
            detail={"code": "DATASET_VERSION_UNAVAILABLE", "message": f"无法读取数据集版本: {dataset_id}"},
        )
    if requested_dataset_version and requested_dataset_version != actual_version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DATASET_VERSION_STALE",
                "message": "请求的数据集版本已失效，请重新读取 /api/health",
                "details": {
                    "dataset_id": dataset_id,
                    "requested_version": requested_dataset_version,
                    "current_version": actual_version,
                },
            },
        )
    return actual_version


def require_dataset_versions(
    dataset_ids: tuple[str, ...],
    requested_versions: dict[str, str] | None = None,
    requested_market_version: str = "",
) -> dict[str, str]:
    requested = requested_versions or {}
    if requested_market_version:
        require_market_data_version(requested_market_version)
    return {
        dataset_id: require_dataset_version(dataset_id, requested.get(dataset_id, ""))
        for dataset_id in dataset_ids
    }
