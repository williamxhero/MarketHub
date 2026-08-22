from __future__ import annotations

"""Version-aware, resumable orchestration for the MarketHub storage-v2 migration."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Iterator

import timescale_tables as core


PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "manifest.json"


def _manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _load_env(path: Path | None) -> None:
    if path is None:
        return
    if not path.is_file():
        raise RuntimeError(f"environment file does not exist: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name, value)


def _require_database_env() -> None:
    missing = [
        name
        for name in (
            "MARKETHUB_DB_HOST",
            "MARKETHUB_DB_PORT",
            "MARKETHUB_DB_NAME",
            "MARKETHUB_DB_USER",
            "MARKETHUB_DB_PASSWORD",
        )
        if not os.getenv(name)
    ]
    if missing:
        raise RuntimeError("missing database environment: " + ", ".join(missing))


def _write(path: Path | None, payload: object) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, path)
    print(encoded, end="")


def inspect() -> dict[str, object]:
    return {
        "migration": _manifest(),
        "inspected_at_utc": datetime.now(timezone.utc).isoformat(),
        "tables": [core.relation_status(spec) for spec in core.SPECS.values()],
    }


def _journal_ready(spec: core.TableSpec, direction: str) -> bool:
    status = core.journal_status(spec, direction)
    return bool(status["present"] and status["triggers"] == 4)


def _prepare_ordinary_table(spec: core.TableSpec, *, conversion_workers: int) -> dict[str, object]:
    state = core.relation_status(spec)
    if not state["relations"]["canonical"]:
        raise RuntimeError(f"bootstrap is incomplete; missing {spec.source}")
    if state["canonical_hypertable"]:
        return {"table": spec.name, "stage": "already_hypertable"}
    if state["relations"]["legacy"] or state["relations"]["failed"]:
        raise RuntimeError(f"ambiguous pre-cutover state for {spec.name}: {state['relations']}")
    if not state["relations"]["shadow"]:
        core.create_shadow(spec)
    if not _journal_ready(spec, "forward"):
        status = core.journal_status(spec, "forward")
        if status["remaining"]:
            core.reconcile_journal(spec, "forward")
        core.install_journal(spec, "forward")
    core.set_secondary_indexes(spec, present=False, apply=True)
    backfill = core.backfill(spec)
    core.set_secondary_indexes(spec, present=True, apply=True)
    conversion = core.convert_historical(spec, workers=conversion_workers)
    return {
        "table": spec.name,
        "stage": "prepared",
        "months": len(backfill["months"]),
        "conversion": conversion,
    }


def _service_exists(service_name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "show", f"{service_name}.service", "--property=LoadState", "--value"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "loaded"


def _systemctl(*arguments: str) -> None:
    command = ["sudo", "-n", "systemctl", *arguments]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(command)}\n{result.stderr.strip()}")


@contextmanager
def _writers_paused(service_name: str | None, asserted_paused: bool) -> Iterator[None]:
    restarted = False
    if service_name and _service_exists(service_name):
        active = subprocess.run(["systemctl", "is-active", "--quiet", f"{service_name}.service"]).returncode == 0
        if active:
            _systemctl("stop", f"{service_name}.service")
            restarted = True
    elif not asserted_paused:
        raise RuntimeError("writer pause cannot be proven; provide --service-name or --writers-paused")
    try:
        yield
    finally:
        if restarted:
            _systemctl("start", f"{service_name}.service")


def _acceptance_hash(spec: core.TableSpec, verification: dict[str, object], probe: dict[str, object]) -> str:
    payload = {
        "migration_id": _manifest()["migration_id"],
        "target_storage_version": _manifest()["target_storage_version"],
        "table": spec.name,
        "verification_evidence_sha256": verification["evidence_sha256"],
        "verified_rows": verification["source_rows"],
        "reverse_probe": probe,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _finalize_table(spec: core.TableSpec, *, verification_workers: int) -> dict[str, object]:
    state = core.relation_status(spec)
    if state["canonical_hypertable"]:
        cutover = state["cutover"]
        if not state["relations"]["legacy"]:
            return {"table": spec.name, "stage": "fresh_hypertable"}
        if cutover and cutover["accelerated_acceptance_sha256"] and cutover["reverse_mirror_removed_at_utc"]:
            return {
                "table": spec.name,
                "stage": "already_accepted",
                "acceptance_sha256": cutover["accelerated_acceptance_sha256"],
            }
        if not _journal_ready(spec, "reverse"):
            raise RuntimeError(f"cut over table lacks the reverse journal required to resume: {spec.name}")
        probe = core.probe_journal(spec, "reverse")
        reconcile = core.reconcile_journal(spec, "reverse")
        verification = {
            "evidence_sha256": cutover["verification_evidence_sha256"],
            "source_rows": cutover["verified_rows"],
        }
        acceptance = _acceptance_hash(spec, verification, probe)
        core.remove_reverse(spec, apply=True, evidence_sha256=acceptance)
        return {"table": spec.name, "stage": "accepted_after_resume", "probe": probe, "reconcile": reconcile, "acceptance_sha256": acceptance}
    verification = core.verify(spec, workers=verification_workers, writers_paused=True)
    cutover = core.cutover(spec, apply=True)
    probe = core.probe_journal(spec, "reverse")
    reconcile = core.reconcile_journal(spec, "reverse")
    if reconcile["remaining"]:
        raise RuntimeError(f"reverse journal did not drain for {spec.name}")
    acceptance = _acceptance_hash(spec, verification, probe)
    core.remove_reverse(spec, apply=True, evidence_sha256=acceptance)
    return {
        "table": spec.name,
        "stage": "cutover_accepted",
        "verification": verification,
        "cutover": cutover,
        "probe": probe,
        "acceptance_sha256": acceptance,
    }


def apply(*, service_name: str | None, writers_paused: bool, verification_workers: int, conversion_workers: int) -> dict[str, object]:
    started = datetime.now(timezone.utc)
    prepared = [
        _prepare_ordinary_table(spec, conversion_workers=conversion_workers)
        for spec in core.SPECS.values()
    ]
    with _writers_paused(service_name, writers_paused):
        finalized = [
            _finalize_table(spec, verification_workers=verification_workers)
            for spec in core.SPECS.values()
        ]
    verified = verify()
    return {
        "migration_id": _manifest()["migration_id"],
        "source_storage_version": _manifest()["source_storage_version"],
        "target_storage_version": _manifest()["target_storage_version"],
        "started_at_utc": started.isoformat(),
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "prepared": prepared,
        "finalized": finalized,
        "verification": verified,
    }


def verify() -> dict[str, object]:
    failures: list[str] = []
    tables: list[dict[str, object]] = []
    for spec in core.SPECS.values():
        state = core.relation_status(spec)
        tables.append(state)
        relations = state["relations"]
        if not relations["canonical"] or not state["canonical_hypertable"]:
            failures.append(f"{spec.name}: canonical hypertable missing")
        if relations["shadow"] or relations["failed"]:
            failures.append(f"{spec.name}: migration residue exists")
        if relations["legacy"]:
            cutover = state["cutover"]
            if not cutover or not cutover["accelerated_acceptance_sha256"] or not cutover["reverse_mirror_removed_at_utc"]:
                failures.append(f"{spec.name}: legacy retained without completed acceptance")
        for direction in ("forward", "reverse"):
            journal = core.journal_status(spec, direction)
            if journal["present"] or journal["triggers"]:
                failures.append(f"{spec.name}: {direction} journal residue exists")
    if failures:
        raise RuntimeError("storage-v2 verification failed: " + "; ".join(failures))
    return {"target_storage_version": _manifest()["target_storage_version"], "status": "complete", "tables": tables}


def cleanup(*, confirmation: str) -> dict[str, object]:
    target = str(_manifest()["target_storage_version"])
    if confirmation != target:
        raise RuntimeError(f"cleanup confirmation must exactly equal {target}")
    verify()
    results: list[dict[str, object]] = []
    for spec in core.SPECS.values():
        state = core.relation_status(spec)
        if not state["relations"]["legacy"]:
            results.append({"table": spec.name, "legacy": "not_present"})
            continue
        acceptance = state["cutover"]["accelerated_acceptance_sha256"]
        results.append(core.cleanup_legacy(spec, apply=True, acceptance_sha256=acceptance))
    verify()
    return {"target_storage_version": target, "status": "legacy_cleanup_complete", "tables": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("inspect")
    migrate = actions.add_parser("apply")
    migrate.add_argument("--service-name")
    migrate.add_argument("--writers-paused", action="store_true")
    migrate.add_argument("--verification-workers", type=int, default=4)
    migrate.add_argument("--conversion-workers", type=int, default=4)
    actions.add_parser("verify")
    cleanup_action = actions.add_parser("cleanup-legacy")
    cleanup_action.add_argument("--confirm-target-version", required=True)
    args = parser.parse_args()
    _load_env(args.env_file)
    _require_database_env()
    if args.action == "inspect":
        result = inspect()
    elif args.action == "apply":
        result = apply(
            service_name=args.service_name,
            writers_paused=args.writers_paused,
            verification_workers=args.verification_workers,
            conversion_workers=args.conversion_workers,
        )
    elif args.action == "verify":
        result = verify()
    else:
        result = cleanup(confirmation=args.confirm_target_version)
    _write(args.output, result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise
