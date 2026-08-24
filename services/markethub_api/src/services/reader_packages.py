from __future__ import annotations

from quotemux.source_packages.environment import ensure_package_environment, package_uses_isolated_environment
from quotemux.source_packages.registry import get_default_source_package_registry


def ensure_reader_packages_ready() -> None:
    manifest = get_default_source_package_registry().get_manifest("derived_core")
    if package_uses_isolated_environment(manifest):
        ensure_package_environment(manifest)
