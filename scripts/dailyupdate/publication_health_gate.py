#!/usr/bin/env python3
"""Evaluate a publication's declared data-health dependencies.

The platform-wide data-health report covers every registered capability.  A
publication depends on a small, explicitly declared subset of it.  Gating a
publication on the platform-wide aggregate status turns any durable, unrelated
quality alert into a permanent publication outage: the source dataset keeps
advancing its immutable version every day while nothing is ever published for
it, so the public resolve/manifest routes fail closed forever.

This gate therefore evaluates only the declared dependencies and fails closed
on anything it cannot positively confirm as healthy.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any


EXIT_PASSED = 0
EXIT_CONFIG_ERROR = 64
EXIT_DEFERRED = 70

CHECKED_AT_FORMAT = "%Y-%m-%d %H:%M:%S"
BLOCKING_STATUSES = ("unhealthy", "unknown", "")


class ConfigError(RuntimeError):
    """The gate was invoked with an unusable declaration or payload."""


def parse_dependencies(declaration: str) -> list[str]:
    dependencies = [item.strip() for item in declaration.split(",")]
    dependencies = [item for item in dependencies if item]
    if not dependencies:
        raise ConfigError("publication health dependencies are not declared")
    deduplicated: list[str] = []
    for dependency in dependencies:
        if dependency not in deduplicated:
            deduplicated.append(dependency)
    return deduplicated


def _status_of(entry: Any) -> str:
    return str(entry.get("status", "")) if isinstance(entry, dict) else ""


def _worst(statuses: list[str]) -> str:
    if not statuses:
        return "unknown"
    for candidate in BLOCKING_STATUSES:
        if candidate in statuses:
            return candidate or "unknown"
    return "warning" if "warning" in statuses else "healthy"


def build_status_index(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Index every addressable health identifier to the statuses reported for it.

    A check identifier can legitimately appear under several capabilities (for
    example a shared table check).  All occurrences are collected so the gate
    can take the worst one.
    """
    index: dict[str, list[str]] = {}

    def record(identifier: str, status: str) -> None:
        if identifier:
            index.setdefault(identifier, []).append(status)

    def record_checks(container: Any) -> None:
        if not isinstance(container, dict):
            return
        checks = container.get("checks", [])
        if isinstance(checks, list):
            for check in checks:
                if isinstance(check, dict):
                    record(str(check.get("check_id", "")), _status_of(check))

    dependencies = payload.get("dependencies", {})
    if isinstance(dependencies, dict):
        for name, block in dependencies.items():
            record(str(name), _status_of(block))
            record_checks(block)

    capabilities = payload.get("capabilities", [])
    if isinstance(capabilities, list):
        for capability in capabilities:
            if not isinstance(capability, dict):
                continue
            record(str(capability.get("capability_id", "")), _status_of(capability))
            record_checks(capability)

    return index


def load_payload(path: Path, not_before: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"data health payload is unreadable: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"data health payload is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("data health payload is not an object")
    checked_at = str(payload.get("checked_at", ""))
    if checked_at == "":
        raise ConfigError("data health payload has no checked_at")
    if not_before:
        try:
            checked = datetime.strptime(checked_at, CHECKED_AT_FORMAT)
            threshold = datetime.strptime(not_before, CHECKED_AT_FORMAT)
        except ValueError as exc:
            raise ConfigError(f"unparsable checked_at boundary: checked_at={checked_at} not_before={not_before}") from exc
        if checked < threshold:
            raise ConfigError(f"data health payload is stale: checked_at={checked_at} not_before={not_before}")
    return payload


def evaluate(payload: dict[str, Any], dependencies: list[str]) -> list[tuple[str, str]]:
    """Return one (identifier, status) row per declared dependency.

    An identifier the report does not contain resolves to ``unknown`` so an
    unregistered or renamed dependency defers the publication instead of
    silently passing it.
    """
    index = build_status_index(payload)
    return [(dependency, _worst(index.get(dependency, []))) for dependency in dependencies]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate a publication on its declared data-health dependencies")
    parser.add_argument("--payload", type=Path, required=True, help="Path to the latest data-health report")
    parser.add_argument("--dependencies", required=True, help="Comma separated declared health identifiers")
    parser.add_argument("--not-before", default="", help="Reject a report older than this 'YYYY-MM-DD HH:MM:SS' local timestamp")
    args = parser.parse_args(argv)

    try:
        dependencies = parse_dependencies(args.dependencies)
        payload = load_payload(args.payload, args.not_before.strip())
    except ConfigError as exc:
        print(f"publication_health_gate=config_error reason={exc}")
        return EXIT_CONFIG_ERROR

    results = evaluate(payload, dependencies)
    blocking = [identifier for identifier, status in results if status in ("unhealthy", "unknown")]
    for identifier, status in results:
        print(f"publication_health_dependency id={identifier} status={status}")
    if blocking:
        print(f"publication_health_gate=deferred blocking={','.join(blocking)}")
        return EXIT_DEFERRED
    print(f"publication_health_gate=passed dependencies={len(results)}")
    return EXIT_PASSED


if __name__ == "__main__":
    sys.exit(main())
