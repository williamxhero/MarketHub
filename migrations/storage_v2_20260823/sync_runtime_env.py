from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile


def sync_runtime_env(
    *,
    env_path: Path,
    app_root: Path,
    runtime_root: Path,
    release_root: Path,
    package_venv_root: Path,
) -> None:
    updates = {
        "MARKETHUB_RUNTIME_ROOT": str(runtime_root),
        "MARKETHUB_DATA_ROOT": str(runtime_root / "store"),
        "MARKETHUB_LOG_ROOT": str(runtime_root / "logs"),
        "MARKETHUB_DATA_UPDATE_ROOT": str(runtime_root / "data-update"),
        "MARKETHUB_EXPORT_ROOT": str(app_root / "exports"),
        # Keep maintenance entrypoints independent of a particular release name while
        # still letting them import code from the atomically switched current release.
        "MARKETHUB_CODE_ROOT": str(app_root / "current"),
        "QUOTEMUX_RUNTIME_ROOT": str(runtime_root / "runtime"),
        "QUOTEMUX_CACHE_PAYLOAD_ROOT": str(runtime_root / "cache_payloads"),
        "QUOTEMUX_PACKAGE_REPO_SPEC": str(release_root / "QuoteMux_Packages"),
        "QUOTEMUX_PACKAGE_VENV_ROOT": str(package_venv_root),
        "QUOTEMUX_ALLOW_LOCAL_PACKAGE_REPO": "true",
    }
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    output_lines: list[str] = []
    written: set[str] = set()
    for line in existing_lines:
        key, separator, _ = line.partition("=")
        if separator and key in updates:
            if key not in written:
                output_lines.append(f"{key}={updates[key]}")
                written.add(key)
            continue
        output_lines.append(line)
    for key, value in updates.items():
        if key not in written:
            output_lines.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = env_path.stat().st_mode if env_path.exists() else 0o600
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=env_path.parent,
        prefix=f".{env_path.name}.",
        delete=False,
    ) as handle:
        handle.write("\n".join(output_lines) + "\n")
        temporary_path = Path(handle.name)
    os.chmod(temporary_path, previous_mode)
    os.replace(temporary_path, env_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 release-scoped MarketHub 运行环境路径")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--package-venv-root", type=Path, required=True)
    args = parser.parse_args()
    sync_runtime_env(
        env_path=args.env_file,
        app_root=args.app_root,
        runtime_root=args.runtime_root,
        release_root=args.release_root,
        package_venv_root=args.package_venv_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
