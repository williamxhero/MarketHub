from __future__ import annotations

import base64
import binascii
from datetime import datetime
from io import BytesIO
import json
import os
import shutil
import zipfile
from pathlib import Path
from pathlib import PurePosixPath

from quotemux.capabilities import get_capability_definition, list_public_api_bindings, normalize_capability_id
from quotemux.config_runtime import ContractPolicyOverride, SourceInstanceConfig, get_config_runtime
from quotemux.contracts.policies import get_contract_policy, list_contract_policies
from quotemux.contracts.registry import get_contract_allowed_merge_strategies, get_contract_result_shape, list_contract_names
from quotemux.contracts.strategies import list_merge_strategies
from quotemux.runtime_core.audit import read_fallback_summary, record_provider_event
from quotemux.runtime_core.health import get_provider_metrics
from quotemux.source_packages.registry import clear_loaded_source_package_modules, refresh_default_source_package_registry
from quotemux.store.admin import CachePolicyUpdate, CapturePolicyPayload, QuoteMuxCacheAdmin, QuoteMuxCaptureAdmin
from quotemux.store.postgres import CACHE_NEVER_EXPIRE_TTL_SECONDS, _coverage_mode_for_capability, _key_fields_for_capability, _request_scope_fields_for_capability, _time_field_for_capability


MANIFEST_FILE_NAME = "quotemux_package.json"
SECONDS_PER_DAY = 86400
DEFAULT_TTL_DAYS = 365
DEFAULT_TTL_SECONDS = DEFAULT_TTL_DAYS * SECONDS_PER_DAY
_CACHE_ADMIN = QuoteMuxCacheAdmin()
_CAPTURE_ADMIN: QuoteMuxCaptureAdmin | None = None


def _runtime():
    return get_config_runtime()


def _capture_admin():
    global _CAPTURE_ADMIN
    if _CAPTURE_ADMIN is None:
        _CAPTURE_ADMIN = QuoteMuxCaptureAdmin()
    return _CAPTURE_ADMIN


def _project_root() -> Path:
    env_root = os.getenv("MARKETHUB_PROJECT_ROOT", "")
    if env_root != "":
        return Path(env_root)
    return Path(__file__).resolve().parents[4]


def _admin_state_path() -> Path:
    return _project_root() / "config_runtime" / "admin_state.json"


def _managed_source_packages_root() -> Path:
    return _project_root() / "source_packages"


def _managed_namespace_root() -> Path:
    return _managed_source_packages_root() / "quotemux_packages"


def _managed_archive_upload_root() -> Path:
    return _managed_source_packages_root() / "uploaded_archives"


def _read_admin_state() -> dict[str, object]:
    state_path = _admin_state_path()
    if not state_path.is_file():
        return {"unregistered_package_ids": []}
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"unregistered_package_ids": []}
    return payload


def _write_admin_state(payload: dict[str, object]) -> None:
    state_path = _admin_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _unregistered_package_ids() -> tuple[str, ...]:
    payload = _read_admin_state()
    package_ids = payload.get("unregistered_package_ids", [])
    if not isinstance(package_ids, list):
        return ()
    return tuple(str(item) for item in package_ids)


def _save_unregistered_package_ids(package_ids: tuple[str, ...]) -> None:
    _write_admin_state({"unregistered_package_ids": list(dict.fromkeys(sorted(package_ids)))})


def _visible_manifests():
    unregistered = set(_unregistered_package_ids())
    return tuple(item for item in _runtime().list_source_packages() if item.package_id not in unregistered)


def _ensure_managed_import_root() -> tuple[str, ...]:
    managed_root = _managed_source_packages_root()
    managed_root.mkdir(parents=True, exist_ok=True)
    return _register_managed_import_root(managed_root, ())


def _read_manifest_package_id(package_root: Path) -> str:
    manifest_path = package_root / MANIFEST_FILE_NAME
    if not manifest_path.is_file():
        return ""
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("package_id", ""))


def _read_manifest_handler_targets(package_root: Path) -> tuple[str, ...]:
    manifest_path = package_root / MANIFEST_FILE_NAME
    if not manifest_path.is_file():
        return ()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return ()
    handler_targets = payload.get("handler_targets", {})
    if not isinstance(handler_targets, dict):
        return ()
    return tuple(str(item) for item in handler_targets.values())


def _uses_quotemux_namespace(package_root: Path) -> bool:
    return any(target.startswith("quotemux_packages.") for target in _read_manifest_handler_targets(package_root))


def _copy_ignore_patterns(_: str, names: list[str]) -> set[str]:
    ignored = {"__pycache__", ".git", "build", "dist"}
    return {name for name in names if name in ignored or name.endswith(".pyc") or name.endswith(".egg-info")}


def _package_directory_name(package_root: Path) -> str:
    package_id = _read_manifest_package_id(package_root)
    if package_id != "":
        return package_id
    return package_root.name


def _safe_upload_parts(path_text: str) -> tuple[str, ...]:
    clean_path = path_text.replace("\\", "/")
    pure_path = PurePosixPath(clean_path)
    if pure_path.is_absolute():
        raise ValueError(f"闈炴硶鏂囦欢璺緞: {path_text}")
    parts = tuple(part for part in pure_path.parts if part not in {"", "."})
    if parts == () or ".." in parts:
        raise ValueError(f"闈炴硶鏂囦欢璺緞: {path_text}")
    return parts


def _copy_package_to_managed_root(source_root: Path) -> tuple[Path, Path]:
    if _uses_quotemux_namespace(source_root):
        import_root = _managed_namespace_root()
        target_root = import_root / source_root.name
    else:
        import_root = _managed_source_packages_root()
        target_root = import_root / source_root.name
    import_root.mkdir(parents=True, exist_ok=True)
    if target_root == source_root:
        return import_root, target_root
    shutil.copytree(source_root, target_root, dirs_exist_ok=True, ignore=_copy_ignore_patterns)
    return import_root, target_root


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _iter_import_root_manifest_paths(import_root: Path) -> tuple[Path, ...]:
    if not import_root.exists():
        return ()
    candidates = [import_root / MANIFEST_FILE_NAME]
    candidates.extend(import_root.glob(f"*/{MANIFEST_FILE_NAME}"))
    candidates.extend(import_root.glob(f"*/*/{MANIFEST_FILE_NAME}"))
    return tuple(path for path in sorted(set(candidates)) if path.is_file())


def _read_import_root_package_ids(import_root: Path) -> tuple[str, ...]:
    package_ids: list[str] = []
    for manifest_path in _iter_import_root_manifest_paths(import_root):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            package_id = str(payload.get("package_id", ""))
            if package_id != "":
                package_ids.append(package_id)
    return tuple(package_ids)


def _keep_import_root(root_text: str, import_root: Path, package_ids: tuple[str, ...]) -> bool:
    root_path = Path(root_text)
    if _path_is_relative_to(root_path, import_root) or _path_is_relative_to(import_root, root_path):
        return False
    if package_ids == ():
        return True
    root_package_ids = set(_read_import_root_package_ids(root_path))
    return root_package_ids.isdisjoint(package_ids)


def _register_managed_import_root(import_root: Path, package_ids: tuple[str, ...]) -> tuple[str, ...]:
    store = _runtime()._store
    root_text = str(import_root)
    original_roots = store.read_import_roots()
    current_roots = tuple(root for root in original_roots if _keep_import_root(root, import_root, package_ids))
    next_roots = tuple(dict.fromkeys((root_text, *current_roots)))
    store.write_import_roots(next_roots)
    try:
        clear_loaded_source_package_modules()
        refresh_default_source_package_registry()
        _runtime().ensure_initialized()
    except Exception:
        store.write_import_roots(original_roots)
        clear_loaded_source_package_modules()
        refresh_default_source_package_registry()
        raise
    return store.read_import_roots()


def _is_uploaded_package_file(parts: tuple[str, ...]) -> bool:
    return not any(part == "__pycache__" or part.endswith(".pyc") for part in parts)


def _write_uploaded_file(managed_root: Path, parts: tuple[str, ...], content: bytes) -> Path:
    if len(parts) == 1:
        target_parts = ("uploaded_source_package", parts[0])
    else:
        target_parts = parts
    target_path = managed_root.joinpath(*target_parts)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(content)
    return managed_root / target_parts[0]


def _finish_imported_target_roots(target_roots: tuple[Path, ...]) -> dict[str, object]:
    if target_roots == ():
        raise KeyError("上传目录为空")
    namespace_roots = tuple(target_root for target_root in target_roots if _uses_quotemux_namespace(target_root))
    import_root = _managed_source_packages_root()
    if namespace_roots != ():
        import_root = _managed_namespace_root()
        import_root.mkdir(parents=True, exist_ok=True)
        for target_root in namespace_roots:
            shutil.copytree(target_root, import_root / _package_directory_name(target_root), dirs_exist_ok=True, ignore=_copy_ignore_patterns)
    imported_package_ids: list[str] = []
    imported_roots = namespace_roots if namespace_roots != () else target_roots
    for target_root in imported_roots:
        package_id = _read_manifest_package_id(target_root)
        imported_package_ids.extend(_candidate_package_ids(package_id, target_root.name))
    _register_managed_import_root(import_root, tuple(imported_package_ids))
    restored_package_ids = _restore_package_registration(tuple(imported_package_ids))
    if restored_package_ids == ():
        _remove_unregistered_ids(tuple(imported_package_ids))
    packages = refresh_source_packages()
    _record_admin_change("import_uploaded_source_package", "source_packages", {"package_ids": imported_package_ids, "restored_package_ids": list(restored_package_ids)})
    return {
        "import_roots": list(_runtime().list_import_roots()),
        "packages": packages,
    }


def _remove_unregistered_ids(package_ids: tuple[str, ...]) -> None:
    if package_ids == ():
        return
    unregistered = tuple(item for item in _unregistered_package_ids() if item not in package_ids)
    _save_unregistered_package_ids(unregistered)


def _known_package_ids() -> set[str]:
    return {manifest.package_id for manifest in _runtime().list_source_packages()}


def _candidate_package_ids(*values: str) -> tuple[str, ...]:
    package_ids: list[str] = []
    for value in values:
        if value == "":
            continue
        for candidate in (value, value.lower()):
            if candidate not in package_ids:
                package_ids.append(candidate)
    return tuple(package_ids)


def _restore_package_registration(package_ids: tuple[str, ...]) -> tuple[str, ...]:
    known_package_ids = _known_package_ids()
    hidden_package_ids = set(_unregistered_package_ids())
    restored_package_ids = tuple(package_id for package_id in package_ids if package_id in known_package_ids and package_id in hidden_package_ids)
    if restored_package_ids == ():
        return ()
    _remove_unregistered_ids(restored_package_ids)
    for instance in _runtime().list_source_instances():
        if instance.package_id in restored_package_ids:
            _runtime().update_source_instance_enabled(instance.instance_id, True)
    _restore_default_policy_sources(restored_package_ids)
    publish_runtime_profile("Console Package Import", f"鎭㈠娉ㄥ唽 {', '.join(restored_package_ids)}")
    return restored_package_ids


def _restore_default_policy_sources(package_ids: tuple[str, ...]) -> None:
    package_to_instance = _instance_package_index()
    instance_package_ids = {item.instance_id: item.package_id for item in _runtime().list_source_instances()}
    current_policies = _all_policies_by_contract()
    for default_policy in list_contract_policies():
        if not any(package_id in default_policy.source_order for package_id in package_ids):
            continue
        current_policy = current_policies[default_policy.name]
        current_packages = tuple(_package_id_for_source_id(source_id, instance_package_ids) for source_id in current_policy.source_order)
        ordered_packages: list[str] = []
        for default_package_id in default_policy.source_order:
            if default_package_id in current_packages or default_package_id in package_ids:
                ordered_packages.append(default_package_id)
        for current_package_id in current_packages:
            if current_package_id not in ordered_packages:
                ordered_packages.append(current_package_id)
        source_order = tuple(package_to_instance[package_id] for package_id in ordered_packages if package_id in package_to_instance)
        save_contract_policy_override(default_policy.name, current_policy.mode, source_order, current_policy.merge_strategy)


def _manifest_contract_matches(contract_name: str, manifest_contract_name: str) -> bool:
    if contract_name == manifest_contract_name:
        return True
    if contract_name.startswith(f"{manifest_contract_name}."):
        return True
    if contract_name == "reference.stock_basic" and manifest_contract_name in {"reference", "stocks.profile"}:
        return True
    return False


def _package_supports_contract(manifest, contract_name: str) -> bool:
    normalized = normalize_capability_id(contract_name)
    for manifest_contract_name in manifest.contract_names:
        if _manifest_contract_matches(normalized, manifest_contract_name):
            return True
    return False


def _instance_package_index() -> dict[str, str]:
    instances = sorted(_runtime().list_source_instances(), key=lambda item: (item.priority, item.instance_id))
    package_to_instance: dict[str, str] = {}
    for instance in instances:
        if instance.package_id not in package_to_instance:
            package_to_instance[instance.package_id] = instance.instance_id
    return package_to_instance


def _package_id_for_source_id(source_id: str, instance_package_ids: dict[str, str]) -> str:
    package_id = instance_package_ids.get(source_id, "")
    if package_id != "":
        return package_id
    return source_id


def _resolve_package_order_to_instances(package_ids: tuple[str, ...]) -> tuple[str, ...]:
    package_to_instance = _instance_package_index()
    source_order: list[str] = []
    for package_id in package_ids:
        instance_id = package_to_instance.get(package_id, "")
        if instance_id == "":
            continue
        source_order.append(instance_id)
        try:
            _runtime().update_source_instance_enabled(instance_id, True)
        except KeyError:
            continue
    return tuple(source_order)


def _contract_enabled_package_ids(policy: ContractPolicyOverride) -> tuple[str, ...]:
    instance_package_ids = {item.instance_id: item.package_id for item in _runtime().list_source_instances()}
    package_ids: list[str] = []
    for source_id in policy.source_order:
        package_id = _package_id_for_source_id(source_id, instance_package_ids)
        if package_id not in package_ids:
            package_ids.append(package_id)
    return tuple(package_ids)


def _all_policies_by_contract() -> dict[str, ContractPolicyOverride]:
    current = {item.contract_name: item for item in _runtime().list_draft_policies()}
    policies: dict[str, ContractPolicyOverride] = {}
    for policy in list_contract_policies():
        override = current.get(policy.name)
        policies[policy.name] = ContractPolicyOverride(
            contract_name=policy.name,
            mode=policy.mode if override is None else override.mode,
            source_order=policy.source_order if override is None else override.source_order,
            merge_strategy=policy.merge_strategy if override is None or override.merge_strategy == "" else override.merge_strategy,
        )
    return policies


def _serialize_package(manifest) -> dict[str, object]:
    payload = manifest.to_dict()
    payload["health"] = _runtime().get_package_health(manifest.package_id).to_dict()
    payload["capability_ids"] = [item["capability_id"] for item in payload.get("capabilities", []) if isinstance(item, dict)]
    return payload


def _serialize_instance(instance: SourceInstanceConfig) -> dict[str, object]:
    payload = instance.to_dict()
    payload["secret_values"] = {field_name: "***" for field_name in instance.secret_values}
    return payload


def _serialize_profile(profile) -> dict[str, object]:
    payload = profile.to_dict()
    source_instances: list[dict[str, object]] = []
    for instance in profile.source_instances:
        source_instances.append(_serialize_instance(instance))
    payload["source_instances"] = source_instances
    return payload


def _serialize_policy(policy: ContractPolicyOverride) -> dict[str, object]:
    capability_id = normalize_capability_id(policy.contract_name)
    base_policy = get_contract_policy(capability_id)
    return {
        "capability_id": capability_id,
        "contract_name": policy.contract_name,
        "mode": policy.mode,
        "source_order": list(policy.source_order),
        "merge_strategy": policy.merge_strategy,
        "stage_namespace": list(base_policy.stage_namespace),
        "api_paths": list(get_capability_definition(capability_id).api_paths),
    }


def _default_cache_policy_payload(capability_id: str) -> dict[str, object]:
    capability = get_capability_definition(capability_id)
    return {
        "capability_id": capability_id,
        "enabled": capability.store_enabled,
        "read_enabled": capability.store_enabled,
        "write_enabled": capability.store_enabled,
        "ttl_seconds": DEFAULT_TTL_SECONDS,
        "time_field": _time_field_for_capability(capability_id),
        "key_fields": list(_key_fields_for_capability(capability_id)),
        "request_scope_fields": list(_request_scope_fields_for_capability(capability_id)),
        "coverage_mode": _coverage_mode_for_capability(capability_id),
    }


def _cache_policy_payload(capability_id: str) -> dict[str, object]:
    try:
        return _CACHE_ADMIN.get_policy(capability_id)
    except Exception:
        return _default_cache_policy_payload(capability_id)


def ttl_days_from_cache_policy(cache_policy: dict[str, object]) -> int:
    ttl_seconds = int(cache_policy.get("ttl_seconds", 0))
    if ttl_seconds == CACHE_NEVER_EXPIRE_TTL_SECONDS:
        return CACHE_NEVER_EXPIRE_TTL_SECONDS
    if ttl_seconds <= 0:
        return 0
    return (ttl_seconds + SECONDS_PER_DAY - 1) // SECONDS_PER_DAY


def _cache_enabled_by_ttl_seconds(ttl_seconds: int) -> bool:
    return ttl_seconds == CACHE_NEVER_EXPIRE_TTL_SECONDS or ttl_seconds > 0


def _ttl_seconds_from_days(ttl_days: int) -> int:
    if ttl_days == CACHE_NEVER_EXPIRE_TTL_SECONDS:
        return CACHE_NEVER_EXPIRE_TTL_SECONDS
    return ttl_days * SECONDS_PER_DAY


def cache_effective_for_capability(capability_id: str, ttl_days: int) -> bool:
    ttl_keeps_cache_enabled = ttl_days == CACHE_NEVER_EXPIRE_TTL_SECONDS or ttl_days > 0
    if not ttl_keeps_cache_enabled:
        return False
    try:
        capture_policy = get_capture_policy(capability_id)
    except Exception:
        return True
    return not bool(capture_policy["enabled"])


def _sync_cache_policy_for_capture(capability_id: str, capture_enabled: bool) -> None:
    current_cache_policy = _cache_policy_payload(capability_id)
    ttl_seconds = int(current_cache_policy["ttl_seconds"])
    ttl_keeps_cache_enabled = _cache_enabled_by_ttl_seconds(ttl_seconds)
    cache_effective = ttl_keeps_cache_enabled and not capture_enabled
    _CACHE_ADMIN.update_policy(
        CachePolicyUpdate(
            capability_id=capability_id,
            enabled=cache_effective,
            ttl_seconds=ttl_seconds,
            read_enabled=cache_effective,
            write_enabled=ttl_keeps_cache_enabled,
        )
    )


def _capability_settings_row(
    capability_id: str,
    policies: dict[str, ContractPolicyOverride] | None = None,
    packages: tuple[object, ...] | None = None,
) -> dict[str, object]:
    normalized_capability_id = normalize_capability_id(capability_id)
    active_policies = _all_policies_by_contract() if policies is None else policies
    visible_packages = _visible_manifests() if packages is None else packages
    policy = active_policies.get(normalized_capability_id)
    enabled_package_ids = set(_contract_enabled_package_ids(policy)) if policy is not None else set(_runtime().get_active_snapshot().list_enabled_package_ids())
    package_rows: list[dict[str, object]] = []
    available_package_ids: list[str] = []
    for manifest in visible_packages:
        supported = _package_supports_contract(manifest, normalized_capability_id)
        package_rows.append(
            {
                "package_id": manifest.package_id,
                "supported": supported,
                "enabled": supported and manifest.package_id in enabled_package_ids,
            }
        )
        if supported:
            available_package_ids.append(manifest.package_id)
    base_policy = get_contract_policy(normalized_capability_id)
    capability = get_capability_definition(normalized_capability_id)
    cache_policy = _cache_policy_payload(normalized_capability_id)
    ttl_days = ttl_days_from_cache_policy(cache_policy)
    cache_effective = cache_effective_for_capability(normalized_capability_id, ttl_days)
    visible_cache_policy = dict(cache_policy)
    visible_cache_policy["enabled"] = cache_effective
    visible_cache_policy["read_enabled"] = cache_effective
    return {
        "capability_id": normalized_capability_id,
        "contract_name": normalized_capability_id,
        "api_paths": list(capability.api_paths),
        "mode": "" if policy is None else policy.mode,
        "policy_managed": policy is not None,
        "result_shape": get_contract_result_shape(normalized_capability_id),
        "merge_strategy": base_policy.merge_strategy if policy is None else policy.merge_strategy,
        "allowed_merge_strategies": list(get_contract_allowed_merge_strategies(normalized_capability_id)),
        "stage_namespace": list(base_policy.stage_namespace),
        "available_packages": available_package_ids,
        "enabled_packages": [item["package_id"] for item in package_rows if item["enabled"]],
        "store": {
            "enabled": capability.store_enabled,
            "freshness_seconds": capability.freshness_seconds,
        },
        "ttl_days": ttl_days,
        "cache_effective": cache_effective,
        "cache_policy": visible_cache_policy,
        "packages": package_rows,
    }


def _filter_source_instance_counts(
    summary: dict[str, object],
    profile_id: str,
    contract_name: str,
    package_id: str,
    source_instance_id: str,
    offset: int,
    limit: int,
) -> dict[str, object]:
    capability_id = normalize_capability_id(contract_name) if contract_name != "" else ""
    counts = summary.get("source_instance_counts", {})
    rows = list(counts.values()) if isinstance(counts, dict) else []
    filtered_rows: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if profile_id != "" and row.get("profile_id") != profile_id:
            continue
        if capability_id != "" and normalize_capability_id(str(row.get("contract_name", ""))) != capability_id:
            continue
        if package_id != "" and row.get("package_id") != package_id:
            continue
        if source_instance_id != "" and row.get("source_instance_id") != source_instance_id:
            continue
        row = dict(row)
        row["capability_id"] = normalize_capability_id(str(row.get("contract_name", "")))
        filtered_rows.append(row)
    actual_limit = max(1, min(limit, 500))
    actual_offset = max(0, offset)
    return {
        "total": len(filtered_rows),
        "offset": actual_offset,
        "limit": actual_limit,
        "items": filtered_rows[actual_offset:actual_offset + actual_limit],
    }


def _record_admin_change(action: str, target: str, payload: dict[str, object]) -> None:
    record_provider_event(
        "admin.runtime",
        "markethub_admin",
        "changed",
        {
            "action": action,
            "target": target,
            **payload,
        },
    )


def list_source_packages() -> list[dict[str, object]]:
    return [_serialize_package(item) for item in _visible_manifests()]


def refresh_source_packages() -> list[dict[str, object]]:
    unregistered = set(_unregistered_package_ids())
    packages = [_serialize_package(item) for item in _runtime().refresh_source_packages() if item.package_id not in unregistered]
    _record_admin_change("refresh_source_packages", "source_packages", {"package_count": len(packages)})
    return packages


def import_source_package(path_text: str) -> dict[str, object]:
    source_root = Path(path_text)
    if not source_root.is_dir():
        raise KeyError(f"鏈煡 source package 鐩綍: {path_text}")
    import_root, target_root = _copy_package_to_managed_root(source_root)
    imported_package_id = _read_manifest_package_id(target_root)
    candidate_package_ids = _candidate_package_ids(imported_package_id, target_root.name, source_root.name)
    _register_managed_import_root(import_root, candidate_package_ids)
    restored_package_ids = _restore_package_registration(candidate_package_ids)
    if restored_package_ids == ():
        _remove_unregistered_ids(candidate_package_ids)
    roots = _runtime().list_import_roots()
    packages = refresh_source_packages()
    _record_admin_change("import_source_package", "source_packages", {"path": path_text, "target_path": str(target_root), "restored_package_ids": list(restored_package_ids)})
    return {
        "import_roots": list(roots),
        "packages": packages,
    }


def import_uploaded_source_package(files: tuple[dict[str, str], ...]) -> dict[str, object]:
    if files == ():
        raise KeyError("涓婁紶鐩綍涓虹┖")
    managed_root = _managed_source_packages_root()
    managed_root.mkdir(parents=True, exist_ok=True)
    target_roots: list[Path] = []
    for file_payload in files:
        file_path = file_payload.get("path", "")
        content_base64 = file_payload.get("content_base64", "")
        parts = _safe_upload_parts(file_path)
        if not _is_uploaded_package_file(parts):
            continue
        try:
            target_root = _write_uploaded_file(managed_root, parts, base64.b64decode(content_base64, validate=True))
        except binascii.Error as exc:
            raise ValueError(f"涓婁紶鏂囦欢鍐呭涓嶆槸鍚堟硶 base64: {file_path}") from exc
        if target_root not in target_roots:
            target_roots.append(target_root)
    return _finish_imported_target_roots(tuple(target_roots))


def import_source_package_archive(filename: str, content_base64: str) -> dict[str, object]:
    if not filename.endswith(".zip"):
        raise ValueError("只支持 zip 格式的 source package 压缩包")
    try:
        archive_content = base64.b64decode(content_base64, validate=True)
    except binascii.Error as exc:
        raise ValueError("上传压缩包内容不是合法 base64") from exc
    upload_root = _managed_archive_upload_root() / datetime.now().strftime("archive-%Y%m%d%H%M%S%f")
    upload_root.mkdir(parents=True, exist_ok=False)
    target_roots: list[Path] = []
    try:
        archive_file = zipfile.ZipFile(BytesIO(archive_content))
    except zipfile.BadZipFile as exc:
        raise ValueError("上传内容不是合法 zip 压缩包") from exc
    with archive_file:
        for item in archive_file.infolist():
            if item.is_dir():
                continue
            parts = _safe_upload_parts(item.filename)
            if not _is_uploaded_package_file(parts):
                continue
            target_root = _write_uploaded_file(upload_root, parts, archive_file.read(item))
            if target_root not in target_roots:
                target_roots.append(target_root)
    return _finish_imported_target_roots(tuple(target_roots))


def unregister_source_package(package_id: str) -> dict[str, object]:
    package = get_source_package(package_id)
    unregistered = tuple((*_unregistered_package_ids(), package_id))
    _save_unregistered_package_ids(unregistered)
    removed_instance_ids: list[str] = []
    for instance in _runtime().list_source_instances():
        if instance.package_id != package_id:
            continue
        _runtime().update_source_instance_enabled(instance.instance_id, False)
        removed_instance_ids.append(instance.instance_id)
    for policy in list_contract_policy_overrides():
        source_order = tuple(source_id for source_id in policy["source_order"] if source_id not in removed_instance_ids and source_id != package_id)
        save_contract_policy_override(str(policy["contract_name"]), str(policy["mode"]), source_order, str(policy["merge_strategy"]))
    publish_runtime_profile("Console Package Unregister", f"鍙栨秷娉ㄥ唽 {package_id}")
    _record_admin_change("unregister_source_package", package_id, {})
    return {
        "package_id": package_id,
        "unregistered": True,
        "package": package,
    }


def get_source_package(package_id: str) -> dict[str, object]:
    for manifest in _visible_manifests():
        if manifest.package_id == package_id:
            return _serialize_package(manifest)
    raise KeyError(f"鏈煡 source package: {package_id}")


def get_source_package_health(package_id: str) -> dict[str, object]:
    get_source_package(package_id)
    return _runtime().get_package_health(package_id).to_dict()


def list_source_instances() -> list[dict[str, object]]:
    return [_serialize_instance(item) for item in _runtime().list_source_instances()]


def save_source_instance(
    instance_id: str,
    package_id: str,
    display_name: str,
    enabled: bool,
    priority: int,
    timeout_seconds: int | None,
    config_values: dict[str, str],
    secret_values: dict[str, str],
    tags: tuple[str, ...],
) -> dict[str, object]:
    instance = SourceInstanceConfig(
        instance_id=instance_id,
        package_id=package_id,
        display_name=display_name,
        enabled=enabled,
        priority=priority,
        timeout_seconds=timeout_seconds,
        config_values=config_values,
        secret_values=secret_values,
        tags=tags,
    )
    saved = _runtime().save_source_instance(instance)
    _record_admin_change("save_source_instance", instance_id, {"package_id": package_id})
    return _serialize_instance(saved)


def set_source_instance_enabled(instance_id: str, enabled: bool) -> dict[str, object]:
    updated = _runtime().update_source_instance_enabled(instance_id, enabled)
    _record_admin_change("set_source_instance_enabled", instance_id, {"enabled": enabled})
    return _serialize_instance(updated)


def delete_source_instance(instance_id: str) -> dict[str, object]:
    _runtime().delete_source_instance(instance_id)
    _record_admin_change("delete_source_instance", instance_id, {})
    return {"instance_id": instance_id, "deleted": True}


def list_runtime_profiles() -> list[dict[str, object]]:
    return [_serialize_profile(item) for item in _runtime().list_profiles()]


def validate_runtime_profile_draft() -> dict[str, object]:
    return _runtime().validate_draft_profile()


def diff_runtime_profile_draft() -> dict[str, object]:
    return _runtime().diff_draft_profile()


def publish_runtime_profile(display_name: str, note: str) -> dict[str, object]:
    profile = _runtime().publish_profile(display_name, note)
    _record_admin_change("publish_runtime_profile", profile.profile_id, {"version": profile.version})
    return _serialize_profile(profile)


def rollback_runtime_profile(profile_id: str) -> dict[str, object]:
    profile = _runtime().rollback_profile(profile_id)
    _record_admin_change("rollback_runtime_profile", profile.profile_id, {"version": profile.version})
    return _serialize_profile(profile)


def list_contract_policy_overrides() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for payload in _all_policies_by_contract().values():
        rows.append(_serialize_policy(payload))
    return rows


def save_contract_policy_override(contract_name: str, mode: str, source_order: tuple[str, ...], merge_strategy: str = "") -> dict[str, object]:
    capability_id = normalize_capability_id(contract_name)
    base_policy = get_contract_policy(capability_id)
    actual_merge_strategy = merge_strategy if merge_strategy != "" else base_policy.merge_strategy
    policy = ContractPolicyOverride(contract_name=capability_id, mode=mode, source_order=source_order, merge_strategy=actual_merge_strategy)
    saved = _runtime().save_draft_policy(policy)
    _record_admin_change("save_contract_policy_override", capability_id, {"mode": mode, "source_order": list(source_order), "merge_strategy": actual_merge_strategy})
    return _serialize_policy(saved)


def get_contract_matrix() -> dict[str, object]:
    packages = _visible_manifests()
    policies = _all_policies_by_contract()
    rows = [_capability_settings_row(contract_name, policies, packages) for contract_name in list_contract_names()]
    return {
        "packages": [_serialize_package(item) for item in packages],
        "capabilities": rows,
        "contracts": rows,
        "merge_strategies": list(list_merge_strategies()),
    }


def get_capability_settings(capability_id: str) -> dict[str, object]:
    return _capability_settings_row(capability_id)


def save_capability_settings(
    capability_id: str,
    merge_strategy: str,
    ttl_days: int | None,
    cache_enabled: bool | None = None,
) -> dict[str, object]:
    normalized_capability_id = normalize_capability_id(capability_id)
    policy = _all_policies_by_contract().get(normalized_capability_id)
    if policy is not None:
        actual_merge_strategy = merge_strategy if merge_strategy != "" else policy.merge_strategy
        save_contract_policy_override(normalized_capability_id, policy.mode, policy.source_order, actual_merge_strategy)
        publish_runtime_profile("Console Capability Settings", f"更新 {normalized_capability_id} 配置")
    current_cache_policy = _cache_policy_payload(normalized_capability_id)
    actual_ttl_days = _resolve_ttl_days(ttl_days, cache_enabled, current_cache_policy)
    actual_cache_ttl_seconds = _ttl_seconds_from_days(actual_ttl_days)
    try:
        capture_policy = get_capture_policy(normalized_capability_id)
        capture_enabled = bool(capture_policy["enabled"])
    except Exception:
        capture_enabled = False
    ttl_keeps_cache_enabled = _cache_enabled_by_ttl_seconds(actual_cache_ttl_seconds)
    cache_effective = ttl_keeps_cache_enabled and not capture_enabled
    _CACHE_ADMIN.update_policy(
        CachePolicyUpdate(
            capability_id=normalized_capability_id,
            enabled=cache_effective,
            ttl_seconds=actual_cache_ttl_seconds,
            read_enabled=cache_effective,
            write_enabled=ttl_keeps_cache_enabled,
        )
    )
    _record_admin_change(
        "save_capability_settings",
        normalized_capability_id,
        {
            "merge_strategy": merge_strategy,
            "ttl_days": actual_ttl_days,
            "cache_effective": cache_effective,
            "cache_ttl_seconds": actual_cache_ttl_seconds,
        },
    )
    return get_capability_settings(normalized_capability_id)


def _resolve_ttl_days(ttl_days: int | None, cache_enabled: bool | None, current_cache_policy: dict[str, object]) -> int:
    if ttl_days is not None:
        return ttl_days
    if cache_enabled is None:
        return ttl_days_from_cache_policy(current_cache_policy)
    if not cache_enabled:
        return 0
    current_ttl_days = ttl_days_from_cache_policy(current_cache_policy)
    if current_ttl_days == CACHE_NEVER_EXPIRE_TTL_SECONDS or current_ttl_days > 0:
        return current_ttl_days
    return DEFAULT_TTL_DAYS


def list_capture_policies() -> list[dict[str, object]]:
    return list(_capture_admin().list_policies())


def list_capture_overview() -> list[dict[str, object]]:
    return list(_capture_admin().list_overview())


def get_capture_policy(capability_id: str) -> dict[str, object]:
    return _capture_admin().get_policy(capability_id)


def save_capture_policy(capability_id: str, payload: dict[str, object]) -> dict[str, object]:
    normalized_capability_id = normalize_capability_id(capability_id)
    current = get_capture_policy(normalized_capability_id)
    schedule = _fixed_capture_schedule(payload, current)
    updated = _capture_admin().update_policy(
        CapturePolicyPayload(
            capability_id=normalized_capability_id,
            enabled=schedule["enabled"],
            cadence=schedule["cadence"],
            run_time=_time_from_text("00:00:00"),
            timezone=str(payload.get("timezone", current["timezone"])),
            weekday=schedule["weekday"],
            month=schedule["month"],
            month_day=schedule["month_day"],
            scope_profile=str(payload.get("scope_profile", current["scope_profile"])),
            window_count=int(payload.get("window_count", current["window_count"])),
            batch_size=int(payload.get("batch_size", current["batch_size"])),
            notes=str(payload.get("notes", current["notes"])),
        )
    )
    _sync_cache_policy_for_capture(normalized_capability_id, bool(schedule["enabled"]))
    return updated


def _fixed_capture_schedule(payload: dict[str, object], current: dict[str, object]) -> dict[str, object]:
    enabled = bool(payload.get("enabled", current["enabled"]))
    cadence = str(payload.get("cadence", current["cadence"]))
    if not enabled:
        return {"enabled": False, "cadence": cadence, "weekday": None, "month": None, "month_day": None}
    if cadence == "weekly":
        return {"enabled": True, "cadence": "weekly", "weekday": 6, "month": None, "month_day": None}
    if cadence == "monthly":
        return {"enabled": True, "cadence": "monthly", "weekday": None, "month": None, "month_day": 31}
    if cadence == "yearly":
        return {"enabled": True, "cadence": "yearly", "weekday": None, "month": 12, "month_day": 31}
    return {"enabled": True, "cadence": cadence, "weekday": None, "month": None, "month_day": None}


def list_capture_runs(capability_id: str = "", status: str = "", limit: int = 100) -> list[dict[str, object]]:
    return list(_capture_admin().list_runs(capability_id, status, limit))


def run_capture(capability_id: str) -> dict[str, object]:
    return _capture_admin().run_capture(capability_id)


def run_due_captures() -> list[dict[str, object]]:
    return list(_capture_admin().run_due_captures())


def _time_from_text(value: str):
    from datetime import time

    parts = value.split(":")
    if len(parts) == 2:
        return time(int(parts[0]), int(parts[1]))
    return time(int(parts[0]), int(parts[1]), int(parts[2]))


def save_contract_matrix(contracts: tuple[dict[str, object], ...]) -> dict[str, object]:
    packages = {item.package_id: item for item in _visible_manifests()}
    policies = _all_policies_by_contract()
    changed_contracts: list[str] = []
    for contract_payload in contracts:
        contract_name = str(contract_payload.get("capability_id", "") or contract_payload.get("contract_name", ""))
        capability_id = normalize_capability_id(contract_name)
        policy = policies.get(capability_id)
        if policy is None:
            continue
        package_ids_payload = contract_payload.get("enabled_package_ids", [])
        package_ids = tuple(str(item) for item in package_ids_payload) if isinstance(package_ids_payload, list) else ()
        merge_strategy = str(contract_payload.get("merge_strategy", ""))
        supported_package_ids: list[str] = []
        for package_id in package_ids:
            manifest = packages.get(package_id)
            if manifest is None:
                raise KeyError(f"鏈煡 source package: {package_id}")
            if not _package_supports_contract(manifest, capability_id):
                raise ValueError(f"{package_id} 涓嶆敮鎸?{contract_name}")
            supported_package_ids.append(package_id)
        source_order = _resolve_package_order_to_instances(tuple(supported_package_ids))
        save_contract_policy_override(capability_id, policy.mode, source_order, merge_strategy if merge_strategy != "" else policy.merge_strategy)
        changed_contracts.append(capability_id)
    publish_runtime_profile("Console Contract Matrix", "Console 淇濆瓨 Contract Matrix 鑷姩鍙戝竷")
    _record_admin_change("save_contract_matrix", "contract_policies", {"contracts": changed_contracts})
    return get_contract_matrix()


def get_runtime_health() -> dict[str, object]:
    instances = _runtime().list_source_instances()
    return {
        "active_profile": _runtime().get_active_snapshot().profile_id,
        "package_health": [item.to_dict() for item in _runtime().list_package_health()],
        "source_instances": [
            {
                "source_instance_id": item.instance_id,
                "package_id": item.package_id,
                "enabled": item.enabled,
                "status": "enabled" if item.enabled else "disabled",
            }
            for item in instances
        ],
        "provider_runtime": get_provider_metrics(),
    }


def get_runtime_audit(day_text: str = "", profile_id: str = "", contract_name: str = "", package_id: str = "", source_instance_id: str = "", offset: int = 0, limit: int = 100) -> dict[str, object]:
    summary = read_fallback_summary(day_text)
    return {
        "active_profile": _runtime().get_active_snapshot().profile_id,
        "fallback_summary": summary,
        "store_summary": summary.get("store_counts", {}),
        "source_instance_page": _filter_source_instance_counts(summary, profile_id, contract_name, package_id, source_instance_id, offset, limit),
    }


def get_runtime_report(day_text: str = "", profile_id: str = "", contract_name: str = "", package_id: str = "", source_instance_id: str = "", offset: int = 0, limit: int = 100) -> dict[str, object]:
    snapshot = _runtime().get_active_snapshot()
    summary = read_fallback_summary(day_text)
    return {
        "active_profile": snapshot.profile_id,
        "version": snapshot.version,
        "published_at": snapshot.published_at,
        "enabled_packages": list(snapshot.list_enabled_package_ids()),
        "draft_instances": list_source_instances(),
        "draft_policies": list_contract_policy_overrides(),
        "package_health": [item.to_dict() for item in _runtime().list_package_health()],
        "health": get_provider_metrics(),
        "audit": summary,
        "store_summary": summary.get("store_counts", {}),
        "capability_count": len(list_public_api_bindings()),
        "source_instance_page": _filter_source_instance_counts(summary, profile_id, contract_name, package_id, source_instance_id, offset, limit),
        "profile_transitions": list(_runtime().list_profile_transitions()),
    }

