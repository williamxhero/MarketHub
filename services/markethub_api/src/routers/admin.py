from __future__ import annotations

import anyio.to_thread
from fastapi import APIRouter, BackgroundTasks, Query
from pydantic import BaseModel, Field

from services import admin_runtime


router = APIRouter()


class SourceInstancePayload(BaseModel):
    instance_id: str
    package_id: str
    display_name: str
    enabled: bool = True
    priority: int = 100
    timeout_seconds: int | None = None
    config_values: dict[str, str] = {}
    secret_values: dict[str, str] = {}
    tags: list[str] = []


class SourceInstanceEnabledPayload(BaseModel):
    enabled: bool


class ImportSourcePackagePayload(BaseModel):
    path: str


class UploadedSourcePackageFilePayload(BaseModel):
    path: str
    content_base64: str


class ImportUploadedSourcePackagePayload(BaseModel):
    files: list[UploadedSourcePackageFilePayload]


class ImportSourcePackageArchivePayload(BaseModel):
    filename: str
    content_base64: str


class PublishProfilePayload(BaseModel):
    display_name: str = ""
    note: str = ""


class ContractPolicyPayload(BaseModel):
    mode: str
    source_order: list[str]
    merge_strategy: str = ""


class ContractMatrixContractPayload(BaseModel):
    capability_id: str = ""
    contract_name: str = ""
    enabled_package_ids: list[str]
    merge_strategy: str = ""


class ContractMatrixPayload(BaseModel):
    contracts: list[ContractMatrixContractPayload]



class CapabilitySettingsPayload(BaseModel):
    merge_strategy: str = ""
    ttl_days: int | None = Field(default=None, ge=-1)
    cache_enabled: bool | None = None


class CapturePolicyPayload(BaseModel):
    enabled: bool
    cadence: str
    run_time: str = "00:00:00"
    timezone: str = "Asia/Shanghai"
    weekday: int | None = None
    month: int | None = None
    month_day: int | None = None
    scope_profile: str
    window_count: int
    batch_size: int
    notes: str = ""


@router.get("/api/admin/source-packages")
async def api_admin_source_packages() -> list[dict[str, object]]:
    return admin_runtime.list_source_packages()


@router.post("/api/admin/source-packages/refresh")
async def api_admin_source_packages_refresh() -> list[dict[str, object]]:
    return admin_runtime.refresh_source_packages()


@router.post("/api/admin/source-packages/install-all")
async def api_admin_source_packages_install_all() -> list[dict[str, object]]:
    return admin_runtime.install_all_source_packages()


@router.post("/api/admin/source-packages/import")
async def api_admin_source_packages_import(payload: ImportSourcePackagePayload) -> dict[str, object]:
    return admin_runtime.import_source_package(payload.path)


@router.post("/api/admin/source-packages/import-directory")
async def api_admin_source_packages_import_directory(payload: ImportUploadedSourcePackagePayload) -> dict[str, object]:
    files = tuple({"path": item.path, "content_base64": item.content_base64} for item in payload.files)
    return admin_runtime.import_uploaded_source_package(files)


@router.post("/api/admin/source-packages/import-archive")
async def api_admin_source_packages_import_archive(payload: ImportSourcePackageArchivePayload) -> dict[str, object]:
    return admin_runtime.import_source_package_archive(payload.filename, payload.content_base64)


@router.get("/api/admin/source-packages/{package_id}")
async def api_admin_source_package_detail(package_id: str) -> dict[str, object]:
    return admin_runtime.get_source_package(package_id)


@router.delete("/api/admin/source-packages/{package_id}")
async def api_admin_source_package_unregister(package_id: str) -> dict[str, object]:
    return admin_runtime.unregister_source_package(package_id)


@router.get("/api/admin/source-packages/{package_id}/health")
async def api_admin_source_package_health(package_id: str) -> dict[str, object]:
    return admin_runtime.get_source_package_health(package_id)


@router.get("/api/admin/source-instances")
async def api_admin_source_instances() -> list[dict[str, object]]:
    return admin_runtime.list_source_instances()


@router.post("/api/admin/source-instances")
async def api_admin_source_instance_create(payload: SourceInstancePayload) -> dict[str, object]:
    return admin_runtime.save_source_instance(
        payload.instance_id,
        payload.package_id,
        payload.display_name,
        payload.enabled,
        payload.priority,
        payload.timeout_seconds,
        payload.config_values,
        payload.secret_values,
        tuple(payload.tags),
    )


@router.put("/api/admin/source-instances/{instance_id}")
async def api_admin_source_instance_update(instance_id: str, payload: SourceInstancePayload) -> dict[str, object]:
    return admin_runtime.save_source_instance(
        instance_id,
        payload.package_id,
        payload.display_name,
        payload.enabled,
        payload.priority,
        payload.timeout_seconds,
        payload.config_values,
        payload.secret_values,
        tuple(payload.tags),
    )


@router.post("/api/admin/source-instances/{instance_id}/enabled")
async def api_admin_source_instance_enabled(instance_id: str, payload: SourceInstanceEnabledPayload) -> dict[str, object]:
    return admin_runtime.set_source_instance_enabled(instance_id, payload.enabled)


@router.delete("/api/admin/source-instances/{instance_id}")
async def api_admin_source_instance_delete(instance_id: str) -> dict[str, object]:
    return admin_runtime.delete_source_instance(instance_id)


@router.get("/api/admin/runtime-profiles")
async def api_admin_runtime_profiles() -> list[dict[str, object]]:
    return admin_runtime.list_runtime_profiles()


@router.get("/api/admin/runtime-profiles/draft/validate")
async def api_admin_runtime_profiles_draft_validate() -> dict[str, object]:
    return admin_runtime.validate_runtime_profile_draft()


@router.get("/api/admin/runtime-profiles/draft/diff")
async def api_admin_runtime_profiles_draft_diff() -> dict[str, object]:
    return admin_runtime.diff_runtime_profile_draft()


@router.post("/api/admin/runtime-profiles/publish")
async def api_admin_runtime_profiles_publish(payload: PublishProfilePayload) -> dict[str, object]:
    return admin_runtime.publish_runtime_profile(payload.display_name, payload.note)


@router.post("/api/admin/runtime-profiles/{profile_id}/rollback")
async def api_admin_runtime_profiles_rollback(profile_id: str) -> dict[str, object]:
    return admin_runtime.rollback_runtime_profile(profile_id)


@router.get("/api/admin/contract-policies")
@router.get("/api/admin/capability-policies")
async def api_admin_contract_policies() -> list[dict[str, object]]:
    return admin_runtime.list_contract_policy_overrides()


@router.put("/api/admin/contract-policies/{contract_name}")
@router.put("/api/admin/capability-policies/{contract_name}")
async def api_admin_contract_policy_update(contract_name: str, payload: ContractPolicyPayload) -> dict[str, object]:
    return admin_runtime.save_contract_policy_override(contract_name, payload.mode, tuple(payload.source_order), payload.merge_strategy)


@router.get("/api/admin/contract-matrix")
@router.get("/api/admin/capability-matrix")
async def api_admin_contract_matrix() -> dict[str, object]:
    return admin_runtime.get_contract_matrix()


@router.put("/api/admin/contract-matrix")
@router.put("/api/admin/capability-matrix")
async def api_admin_contract_matrix_update(payload: ContractMatrixPayload) -> dict[str, object]:
    contracts = tuple(
        {
            "capability_id": item.capability_id,
            "contract_name": item.contract_name,
            "enabled_package_ids": item.enabled_package_ids,
            "merge_strategy": item.merge_strategy,
        }
        for item in payload.contracts
    )
    return admin_runtime.save_contract_matrix(contracts)


@router.get("/api/admin/capability-settings/{capability_id}")
async def api_admin_capability_settings(capability_id: str) -> dict[str, object]:
    return admin_runtime.get_capability_settings(capability_id)


@router.put("/api/admin/capability-settings/{capability_id}")
async def api_admin_capability_settings_update(capability_id: str, payload: CapabilitySettingsPayload) -> dict[str, object]:
    return admin_runtime.save_capability_settings(
        capability_id,
        payload.merge_strategy,
        payload.ttl_days,
        payload.cache_enabled,
    )


@router.get("/api/admin/capture-policies")
async def api_admin_capture_policies() -> list[dict[str, object]]:
    return admin_runtime.list_capture_policies()


@router.get("/api/admin/capture-overview")
async def api_admin_capture_overview() -> list[dict[str, object]]:
    return admin_runtime.list_capture_overview()


@router.get("/api/admin/capture-policies/{capability_id}")
async def api_admin_capture_policy(capability_id: str) -> dict[str, object]:
    return admin_runtime.get_capture_policy(capability_id)


@router.put("/api/admin/capture-policies/{capability_id}")
async def api_admin_capture_policy_update(capability_id: str, payload: CapturePolicyPayload) -> dict[str, object]:
    return admin_runtime.save_capture_policy(capability_id, payload.model_dump())


@router.get("/api/admin/capture-runs")
async def api_admin_capture_runs(
    capability_id: str = "",
    status: str = "",
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, object]]:
    return admin_runtime.list_capture_runs(capability_id, status, limit)


@router.post("/api/admin/capture-runs/{capability_id}")
async def api_admin_run_capture(capability_id: str) -> dict[str, object]:
    return await anyio.to_thread.run_sync(admin_runtime.run_capture, capability_id)


@router.post("/api/admin/capture/run-due")
async def api_admin_run_due_captures() -> list[dict[str, object]]:
    return await anyio.to_thread.run_sync(admin_runtime.run_due_captures)


@router.post("/api/admin/capture/run-due-async")
async def api_admin_run_due_captures_async(background_tasks: BackgroundTasks) -> dict[str, object]:
    background_tasks.add_task(admin_runtime.run_due_captures)
    return {"accepted": True}


@router.get("/api/admin/runtime-health")
async def api_admin_runtime_health() -> dict[str, object]:
    return admin_runtime.get_runtime_health()


@router.get("/api/admin/runtime-audit")
async def api_admin_runtime_audit(
    day: str = "",
    profile_id: str = "",
    capability_id: str = "",
    contract_name: str = "",
    package_id: str = "",
    source_instance_id: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, object]:
    return admin_runtime.get_runtime_audit(day, profile_id, capability_id or contract_name, package_id, source_instance_id, offset, limit)


@router.get("/api/admin/runtime-report")
async def api_admin_runtime_report(
    day: str = "",
    profile_id: str = "",
    capability_id: str = "",
    contract_name: str = "",
    package_id: str = "",
    source_instance_id: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, object]:
    return admin_runtime.get_runtime_report(day, profile_id, capability_id or contract_name, package_id, source_instance_id, offset, limit)
