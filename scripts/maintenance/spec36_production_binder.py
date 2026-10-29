"""Trusted SPEC36 T2 production binder seam.

The module is stdlib-only.  ``LinuxEvidenceCollector`` reads live service,
process, journal, API, Task Center, release and approval state on every call.
It never reads a prior receipt as authority.  ``Binder.apply`` is a library-only
transaction seam: callers must supply the exact confirmation and transaction
hooks; the CLI deliberately exposes probe only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from spec36_owner_journal import OwnerJournalRejected
from spec36_owner_completion import normalize_capture_response, verify_pending_owner
from spec36_runtime_binding import BindingError, LinuxTimerIO
from spec36_safe_switch_gate import _units as reviewed_service_inventory
from spec36_admission_core import Phase as AdmissionPhase
from spec36_private_receipt import PrivateReceiptStore, ReceiptError
from spec36_maintenance_lease import MaintenanceLease, LeaseError, _start_ticks as _lease_start_ticks

REQUIRED_LOCK_PATHS = (
    "/data/markethub/locks/global-data-update.lock",
    "/data/markethub/audit/stock-bar-maintenance.lock",
)
REQUIRED_SYSTEM_SERVICES = (
    "markethub-api.service",
    "markethub-stock-money-flow-backfill.service",
)
REQUIRED_USER_SERVICES = (
    "xdn-task-markethub_global_data_update.service",
    "xdn-task-markethub_global_data_update_0030.service",
    "xdn-task-markethub_global_data_update_0400.service",
)
REQUIRED_TASK_IDS = tuple(name.removesuffix(".service").removeprefix("xdn-task-") for name in REQUIRED_USER_SERVICES)
TERMINAL = frozenset(("success", "failed", "skipped"))
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


class BinderError(RuntimeError):
    """Sanitized binder failure; never contains subprocess/API payloads."""


class GateRejected(BinderError):
    def __init__(self, code: str):
        if not isinstance(code, str) or not _REASON_RE.fullmatch(code):
            code = "gate_rejected"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Process:
    pid: int
    start_ticks: int
    boot: str


@dataclass(frozen=True, slots=True)
class Approval:
    digest: str
    candidate_digest: str
    candidate: str
    profile: tuple
    expires_ns: int
    old_profile: tuple | None = None
    candidate_profile: tuple | None = None
    candidate_manifest: str = ""
    manifest_bound: bool = False
    candidate_version: str = ""
    candidate_data_version: str = ""


@dataclass(frozen=True, slots=True)
class Unit:
    scope: str
    name: str
    active: str = "inactive"
    sub: str = "dead"
    main_pid: int = 0
    control_pid: int = 0
    job: str = ""
    dependents: tuple = ()


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    run_id: int
    status: str
    started: str
    finished: str
    digest: str


@dataclass(frozen=True, slots=True)
class Marker:
    exists: bool = False
    digest: str = ""
    identity: tuple = ()


@dataclass(frozen=True, slots=True)
class OwnerJournal:
    complete: bool
    pid: int
    start_ticks: int
    boot: str
    digest: str
    verifier: str = "none"
    verified_pairs: int = 0


@dataclass(frozen=True, slots=True)
class LockRow:
    path: str
    device: int
    inode: int
    owner_pid: int = 0
    owner_start_ticks: int = 0


@dataclass(frozen=True, slots=True)
class Observation:
    session: str
    sequence: int
    monotonic_ns: int
    observed_at_ns: int = 0
    api: Process | None = None
    api_state: str = "running"
    profile: tuple = ()
    units: tuple[Unit, ...] = ()
    timers: tuple[Unit, ...] = ()
    inventory_complete: bool = False
    tasks: tuple[Task, ...] = ()
    captures: tuple = ()
    marker: Marker = field(default_factory=Marker)
    release: str = ""
    candidate: str = ""
    approval: Approval | None = None
    owner_journal: OwnerJournal | tuple | None = None
    lock_rows: tuple[LockRow, ...] = ()
    phase: str = "prelease"
    health_ok: bool = True
    health_version: str = ""
    health_data_version: str = ""


@dataclass(frozen=True, slots=True)
class Result:
    status: str
    code: str
    recovery: str = "none"
    durable: bool = False
    receipt: str = ""
    recovery_attempted: bool = False
    recovery_result: str = "not_attempted"


class TransactionHooks:
    """Explicit mutation boundary.  Production callers must implement all hooks."""

    def pause_owners(self) -> None:
        raise BinderError("transaction_hooks_missing")

    def stop_api(self) -> None:
        raise BinderError("transaction_hooks_missing")

    def switch_candidate(self) -> None:
        raise BinderError("transaction_hooks_missing")

    def start_api(self) -> None:
        raise BinderError("transaction_hooks_missing")

    def archive_marker(self) -> None:
        raise BinderError("transaction_hooks_missing")

    def stop_candidate_api(self) -> None:
        raise BinderError("recovery_hooks_missing")

    def restore_candidate(self) -> None:
        raise BinderError("recovery_hooks_missing")

    def restart_original_api(self) -> None:
        raise BinderError("recovery_hooks_missing")

    def resume_owners(self) -> None:
        raise BinderError("recovery_hooks_missing")

    def restore_marker(self) -> None:
        raise BinderError("recovery_hooks_missing")


def process_identity() -> tuple[int, int]:
    """Return current PID/start_ticks for lease identity validation."""
    ticks = _lease_start_ticks()
    return os.getpid(), ticks if ticks > 0 else 1


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()


def _safe_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _profile(value: object) -> tuple:
    """Canonicalize an approved cwd/environment whitelist without deriving it.

    Legacy tuple profiles remain accepted by the fake/library seam.  Live
    production profiles use the explicit cwd + environment representation and
    preserve JSON null as None.
    """
    if isinstance(value, tuple):
        return value
    if not isinstance(value, dict):
        raise BinderError("approval_profile_invalid")
    if "cwd" in value or "environment" in value:
        cwd = value.get("cwd")
        environment = value.get("environment")
        if not isinstance(cwd, str) or not cwd.startswith("/") or not isinstance(environment, dict):
            raise BinderError("approval_profile_invalid")
        items = []
        for key, item in environment.items():
            if not isinstance(key, str) or not key or (item is not None and not isinstance(item, str)):
                raise BinderError("approval_profile_invalid")
            items.append((key, item))
        return (("cwd", cwd), ("environment", tuple(sorted(items))))
    return tuple(sorted((str(key), str(item)) for key, item in value.items()))


def _profile_parts(value: tuple) -> tuple[str, tuple[tuple[str, str | None], ...]] | None:
    if not isinstance(value, tuple):
        return None
    mapping = dict(value) if all(isinstance(item, tuple) and len(item) == 2 for item in value) else {}
    cwd = mapping.get("cwd")
    env = mapping.get("environment")
    if not isinstance(cwd, str) or not isinstance(env, tuple):
        return None
    if not all(isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str) and (item[1] is None or isinstance(item[1], str)) for item in env):
        return None
    return cwd, env


def _terminal_capture(value: object) -> bool:
    if isinstance(value, dict):
        return value.get("status") in TERMINAL and bool(value.get("finished_at"))
    return isinstance(value, str) and value in TERMINAL


def _owner_tuple(value: OwnerJournal | tuple | None) -> OwnerJournal | None:
    if isinstance(value, OwnerJournal):
        return value
    if isinstance(value, tuple) and len(value) == 5:
        complete, pid, ticks, boot, digest = value
        if type(complete) is bool and type(pid) is int and type(ticks) is int and isinstance(boot, str) and isinstance(digest, str):
            return OwnerJournal(complete, pid, ticks, boot, digest, "legacy", 0)
    return None


class Binder:
    def __init__(self, evidence: object, *, lease_factory: Callable[[], object] | None = None,
                 receipt_writer: Callable[[dict[str, object]], object] | None = None,
                 lock_paths: tuple[str, ...] = REQUIRED_LOCK_PATHS):
        collect_for_phase = getattr(evidence, "collect_for_phase", None)
        collect = getattr(evidence, "collect", None)
        if callable(collect_for_phase):
            self._collect = collect_for_phase
            self._collector_phase_aware = True
        elif callable(collect):
            self._collect = collect
            self._collector_phase_aware = False
        else:
            raise ValueError("evidence collector required")
        self._lease_factory = lease_factory or (lambda: MaintenanceLease.acquire(lock_paths))
        self._receipt_writer = receipt_writer
        self._session: str | None = None
        self._sequence = 0
        self._last_api: Process | None = None
        self._original_api: Process | None = None
        self._owner_proof: OwnerJournal | None = None
        self._last_marker_exists = False
        self._phase = AdmissionPhase.PRELEASE

    def _validate_lease_identity(self, lease: object) -> None:
        identities = getattr(lease, "identities", None)
        if not callable(identities):
            raise GateRejected("lease_identity_missing")
        try:
            rows = tuple(identities())
        except Exception:
            raise GateRejected("lease_identity_invalid") from None
        if len(rows) != len(REQUIRED_LOCK_PATHS):
            raise GateRejected("lease_identity_incomplete")
        expected_pid, expected_ticks = process_identity()
        seen = set()
        for row in rows:
            if not isinstance(row, tuple) or len(row) != 5:
                raise GateRejected("lease_identity_invalid")
            path, device, inode, pid, ticks = row
            if path not in REQUIRED_LOCK_PATHS or path in seen or type(device) is not int or device <= 0 or type(inode) is not int or inode <= 0 or pid != expected_pid or ticks != expected_ticks:
                raise GateRejected("lease_identity_invalid")
            seen.add(path)
        if seen != set(REQUIRED_LOCK_PATHS):
            raise GateRejected("lease_identity_incomplete")

    def _advance_phase(self, phase: AdmissionPhase) -> None:
        order = {
            AdmissionPhase.PRELEASE: (AdmissionPhase.PRELEASE,),
            AdmissionPhase.OWNED_PAUSING: (AdmissionPhase.PRELEASE, AdmissionPhase.OWNED_PAUSING),
            AdmissionPhase.BEFORE_STOP: (AdmissionPhase.OWNED_PAUSING,),
            AdmissionPhase.STOPPED_BEFORE_COMMIT: (AdmissionPhase.BEFORE_STOP, AdmissionPhase.STOPPED_BEFORE_COMMIT),
            AdmissionPhase.CANDIDATE_HEALTH_BEFORE_COMMIT: (AdmissionPhase.STOPPED_BEFORE_COMMIT, AdmissionPhase.CANDIDATE_HEALTH_BEFORE_COMMIT),
        }
        if self._phase not in order[phase]:
            raise GateRejected("phase_transition_invalid")
        self._phase = phase

    def _fresh(self, now_ns: Callable[[], int], *, phase: AdmissionPhase = AdmissionPhase.PRELEASE) -> Observation:
        self._advance_phase(phase)
        try:
            obs = self._collect(phase) if self._collector_phase_aware else self._collect()
        except GateRejected:
            raise
        except Exception:
            raise GateRejected("observation_failed") from None
        if not isinstance(obs, Observation):
            raise GateRejected("observation_invalid")
        if not isinstance(obs.session, str) or not obs.session or obs.sequence <= self._sequence:
            raise GateRejected("observation_replayed")
        if obs.monotonic_ns <= 0 or obs.monotonic_ns <= getattr(self, "_last_monotonic", 0):
            raise GateRejected("observation_not_fresh")
        now = now_ns()
        if type(now) is not int or obs.observed_at_ns <= 0 or obs.observed_at_ns > now + 5_000_000_000:
            raise GateRejected("observation_clock_invalid")
        if self._session is None:
            self._session = obs.session
        elif obs.session != self._session:
            raise GateRejected("observation_session_changed")
        self._sequence = obs.sequence
        self._last_monotonic = obs.monotonic_ns
        if phase not in (AdmissionPhase.STOPPED_BEFORE_COMMIT, AdmissionPhase.CANDIDATE_HEALTH_BEFORE_COMMIT) and self._last_api is not None and obs.api != self._last_api:
            raise GateRejected("api_identity_changed")
        self._validate(obs, now, phase=phase)
        if phase is not AdmissionPhase.STOPPED_BEFORE_COMMIT and obs.api is not None:
            self._last_api = obs.api
        if self._original_api is None and obs.api is not None:
            self._original_api = obs.api
        owner = _owner_tuple(obs.owner_journal)
        if owner is not None:
            self._owner_proof = owner
        self._last_marker_exists = obs.marker.exists
        return obs

    def _validate(self, obs: Observation, now_ns: int, *, phase: AdmissionPhase) -> None:
        if obs.marker.exists:
            owner = _owner_tuple(obs.owner_journal)
            if owner is None or owner.complete is not True or owner.verifier != "owner_journal_v2" or owner.verified_pairs <= 0:
                raise GateRejected("pending_marker_owner_unproven")
        if not obs.inventory_complete:
            raise GateRejected("inventory_incomplete")
        names = {(row.scope, row.name) for row in obs.units}
        if len(names) != len(obs.units):
            raise GateRejected("service_inventory_duplicate")
        required = {("system", item) for item in REQUIRED_SYSTEM_SERVICES} | {("user", item) for item in REQUIRED_USER_SERVICES}
        if not required.issubset(names):
            raise GateRejected("service_inventory_incomplete")
        if any(row.scope not in ("system", "user") or not row.name.endswith(".service") for row in obs.units):
            raise GateRejected("service_inventory_invalid")
        for row in obs.units:
            if row.scope == "system" and row.name == "markethub-api.service":
                if row.control_pid != 0 or row.job:
                    raise GateRejected("api_control_or_job_busy")
                continue
            if ((row.active, row.sub) not in (("inactive", "dead"), ("failed", "failed"))
                    or row.main_pid != 0 or row.control_pid != 0 or row.job):
                raise GateRejected("writer_not_idle")
        if len({(row.scope, row.name) for row in obs.timers}) != len(obs.timers):
            raise GateRejected("timer_inventory_duplicate")
        if phase is not AdmissionPhase.PRELEASE:
            for row in obs.timers:
                if row.active not in ("inactive", "unknown") or row.sub not in ("dead", "dead-waiting", "unknown") or row.job or row.dependents:
                    raise GateRejected("timer_not_idle")
        if any(not _terminal_capture(value) for value in obs.captures):
            raise GateRejected("active_capture")
        tasks = {task.task_id: task for task in obs.tasks}
        if set(tasks) != set(REQUIRED_TASK_IDS):
            raise GateRejected("task_inventory_incomplete")
        if any(task.status not in TERMINAL or not task.finished for task in tasks.values()):
            raise GateRejected("task_not_terminal")
        if obs.lock_rows:
            for raw_row in obs.lock_rows:
                row = raw_row if isinstance(raw_row, LockRow) else LockRow(*raw_row)
                if row.path not in REQUIRED_LOCK_PATHS or row.owner_pid not in (0, os.getpid()):
                    raise GateRejected("foreign_lock")
                if row.device <= 0 or row.inode <= 0:
                    raise GateRejected("lock_identity_invalid")
        if phase in (AdmissionPhase.PRELEASE, AdmissionPhase.OWNED_PAUSING, AdmissionPhase.BEFORE_STOP, AdmissionPhase.CANDIDATE_HEALTH_BEFORE_COMMIT):
            if obs.api_state != "running" or not isinstance(obs.api, Process):
                raise GateRejected("api_not_running")
            if obs.api.pid <= 0 or obs.api.start_ticks <= 0 or not _HEX32.fullmatch(obs.api.boot):
                raise GateRejected("api_identity_invalid")
            api_rows = [row for row in obs.units if row.name == "markethub-api.service" and row.scope == "system"]
            if len(api_rows) != 1 or api_rows[0].active != "active" or api_rows[0].sub != "running" or api_rows[0].main_pid != obs.api.pid:
                raise GateRejected("api_service_mismatch")
            if phase is AdmissionPhase.CANDIDATE_HEALTH_BEFORE_COMMIT:
                if self._original_api is not None and obs.api == self._original_api:
                    raise GateRejected("candidate_api_identity_reused")
                if obs.health_ok is not True:
                    raise GateRejected("candidate_health_invalid")
        elif phase in (AdmissionPhase.STOPPED_BEFORE_COMMIT,):
            if obs.api_state != "stopped" or obs.api is not None:
                raise GateRejected("api_not_stopped")
        if not isinstance(obs.release, str) or not obs.release.startswith("/"):
            raise GateRejected("release_invalid")
        if not isinstance(obs.candidate, str) or not obs.candidate.startswith("/"):
            raise GateRejected("candidate_invalid")
        if phase is AdmissionPhase.PRELEASE and obs.candidate == obs.release:
            raise GateRejected("candidate_invalid")
        approval = obs.approval
        if not isinstance(approval, Approval) or not _HEX64.fullmatch(approval.digest) or not isinstance(approval.candidate_digest, str) or not approval.candidate_digest:
            raise GateRejected("approval_invalid")
        old_profile = approval.old_profile or approval.profile
        candidate_profile = approval.candidate_profile or approval.profile
        expected_profile = candidate_profile if phase is AdmissionPhase.CANDIDATE_HEALTH_BEFORE_COMMIT else old_profile
        if obs.profile != expected_profile:
            raise GateRejected("candidate_profile_mismatch" if phase is AdmissionPhase.CANDIDATE_HEALTH_BEFORE_COMMIT else "old_profile_mismatch")
        if approval.candidate != obs.candidate or approval.expires_ns <= now_ns:
            raise GateRejected("stale_candidate")
        if phase is AdmissionPhase.CANDIDATE_HEALTH_BEFORE_COMMIT:
            if obs.release != obs.candidate:
                raise GateRejected("candidate_release_mismatch")
            if approval.manifest_bound is not True:
                raise GateRejected("candidate_digest_unbound")
            if not approval.candidate_version or not approval.candidate_data_version:
                raise GateRejected("candidate_health_invalid")
            if obs.health_version != approval.candidate_version or obs.health_data_version != approval.candidate_data_version:
                raise GateRejected("candidate_health_invalid")
        owner = _owner_tuple(obs.owner_journal)
        if owner is None or not _HEX64.fullmatch(owner.digest):
            raise GateRejected("owner_journal_invalid")
        if owner.complete is True:
            if owner.verifier != "owner_journal_v2" or owner.verified_pairs <= 0:
                raise GateRejected("owner_journal_invalid")
        elif owner.verifier != "no_pending_marker" or owner.verified_pairs != 0:
            raise GateRejected("owner_journal_invalid")
        expected_owner = self._original_api or obs.api
        if expected_owner is not None and (owner.pid, owner.start_ticks, owner.boot) != (expected_owner.pid, expected_owner.start_ticks, expected_owner.boot):
            raise GateRejected("owner_journal_api_mismatch")

    def probe(self, *, now_ns: Callable[[], int] = time.time_ns) -> Result:
        if self._session is None:
            self._sequence = 0
            self._last_api = None
            self._phase = AdmissionPhase.PRELEASE
        try:
            self._fresh(now_ns, phase=AdmissionPhase.PRELEASE)
            return Result("probe", "ready")
        except GateRejected as error:
            return Result("refused", error.code, recovery="operator_review")

    def _recover(self, hooks: TransactionHooks, lease: object, attempted: dict[str, bool]) -> tuple[str, str, bool, tuple[str, ...]]:
        """Best-effort inverse transaction under the same still-valid lease.

        A lost lease is never reacquired or bypassed.  In that case recovery
        is explicitly required rather than performing unowned mutations.
        """
        if not any(attempted.values()):
            return "not_needed", "not_attempted", False, ()
        try:
            if not lease.valid():
                return "recovery_required", "lease_lost_before_recovery", False, ()
        except Exception:
            return "recovery_required", "lease_lost_before_recovery", False, ()
        actions: list[str] = []
        if attempted.get("archive_marker"):
            actions.append("restore_marker")
        if attempted.get("start_api"):
            actions.append("stop_candidate_api")
        if attempted.get("switch_candidate"):
            actions.append("restore_candidate")
        if attempted.get("stop_api") or attempted.get("switch_candidate") or attempted.get("start_api"):
            actions.append("restart_original_api")
        if attempted.get("pause_owners"):
            actions.append("resume_owners")
        steps: list[str] = []
        failed = False
        for name in actions:
            try:
                if not lease.valid():
                    steps.append("lease_lost_before_" + name)
                    failed = True
                    break
                getattr(hooks, name)()
                steps.append(name + ":ok")
                if not lease.valid():
                    steps.append(name + ":lease_lost")
                    failed = True
                    break
            except Exception:
                steps.append(name + ":failed")
                failed = True
                break
        if failed:
            return "recovery_required", ";".join(steps) or "recovery_failed", True, tuple(steps)
        return "recovered", "recovered", True, tuple(steps)

    def apply(self, *, confirmation: str, hooks: TransactionHooks,
              now_ns: Callable[[], int] = time.time_ns) -> Result:
        """Run the trusted transaction and explicitly recover partial work.

        This is intentionally a library-only executor.  The CLI remains probe
        only.  Every forward mutation has an inverse hook, and a lost lease
        prevents inverse mutation instead of reacquiring or bypassing it.
        """
        if confirmation != "SPEC36-T2-APPLY":
            return Result("refused", "confirmation_required", recovery="not_needed")
        required = (
            "pause_owners", "stop_api", "switch_candidate", "start_api", "archive_marker",
            "resume_owners", "restart_original_api", "restore_candidate", "stop_candidate_api", "restore_marker",
        )
        if not all(callable(getattr(hooks, name, None)) for name in required):
            return Result("refused", "transaction_hooks_missing", recovery="not_needed")
        lease = None
        phase = "prelease"
        attempted = {name: False for name in required}
        receipt_data: dict[str, object] = {
            "contract": "spec36-t2-binder-v2", "status": "refused", "phase": phase,
            "recovery": "not_needed", "recovery_attempted": False,
            "recovery_result": "not_attempted", "recovery_steps": [],
            "recovery_attempts": [], "recovery_errors": [],
        }
        try:
            lease = self._lease_factory()
            if lease is None or not callable(getattr(lease, "valid", None)) or not callable(getattr(lease, "close", None)):
                raise GateRejected("lease_invalid")
            self._validate_lease_identity(lease)
            if not lease.valid():
                raise GateRejected("lease_lost")
            self._session = None
            self._sequence = 0
            self._last_api = None
            self._original_api = None
            self._owner_proof = None
            self._phase = AdmissionPhase.PRELEASE
            self._fresh(now_ns, phase=AdmissionPhase.PRELEASE)
            if not lease.valid():
                raise GateRejected("lease_lost")
            phase = "pause_owners"
            attempted["pause_owners"] = True
            try:
                hooks.pause_owners()
            except Exception:
                raise GateRejected("transaction_hook_failed") from None
            if not lease.valid():
                raise GateRejected("lease_lost")
            self._fresh(now_ns, phase=AdmissionPhase.OWNED_PAUSING)
            self._fresh(now_ns, phase=AdmissionPhase.BEFORE_STOP)
            if not lease.valid():
                raise GateRejected("lease_lost")
            phase = "stop_api"
            attempted["stop_api"] = True
            try:
                hooks.stop_api()
            except Exception:
                raise GateRejected("transaction_hook_failed") from None
            if not lease.valid():
                raise GateRejected("lease_lost")
            self._fresh(now_ns, phase=AdmissionPhase.STOPPED_BEFORE_COMMIT)
            phase = "switch_candidate"
            attempted["switch_candidate"] = True
            try:
                hooks.switch_candidate()
            except Exception:
                raise GateRejected("transaction_hook_failed") from None
            if not lease.valid():
                raise GateRejected("lease_lost")
            self._fresh(now_ns, phase=AdmissionPhase.STOPPED_BEFORE_COMMIT)
            phase = "start_api"
            attempted["start_api"] = True
            try:
                hooks.start_api()
            except Exception:
                raise GateRejected("transaction_hook_failed") from None
            if not lease.valid():
                raise GateRejected("lease_lost")
            self._fresh(now_ns, phase=AdmissionPhase.CANDIDATE_HEALTH_BEFORE_COMMIT)
            if self._last_marker_exists:
                phase = "archive_marker"
                attempted["archive_marker"] = True
                try:
                    hooks.archive_marker()
                except Exception:
                    raise GateRejected("transaction_hook_failed") from None
                if not lease.valid():
                    raise GateRejected("lease_lost")
            if not lease.valid():
                raise GateRejected("lease_lost")
            receipt_data.update({
                "status": "applied", "phase": "complete", "code": "complete",
                "recovery": "none", "recovery_result": "not_needed",
            })
            receipt = self._write_receipt(receipt_data)
            return Result("applied", "complete", recovery="none", durable=receipt is not None,
                          receipt=receipt or "", recovery_attempted=False, recovery_result="not_needed")
        except GateRejected as error:
            recovery, recovery_result, recovery_attempted, recovery_steps = self._recover(hooks, lease, attempted) if lease is not None else ("not_needed", "not_attempted", False, ())
            receipt_data.update({
                "status": "refused", "phase": phase, "code": error.code,
                "recovery": recovery, "recovery_attempted": recovery_attempted,
                "recovery_result": recovery_result, "recovery_steps": list(recovery_steps),
                "recovery_attempts": [step.split(":", 1)[0] for step in recovery_steps],
                "recovery_errors": [step for step in recovery_steps if step.endswith(":failed") or "lease_lost" in step],
            })
            receipt = self._write_receipt(receipt_data)
            return Result("refused", error.code, recovery=recovery, durable=receipt is not None,
                          receipt=receipt or "", recovery_attempted=recovery_attempted,
                          recovery_result=recovery_result)
        except (LeaseError, BinderError):
            recovery, recovery_result, recovery_attempted, recovery_steps = self._recover(hooks, lease, attempted) if lease is not None else ("not_needed", "not_attempted", False, ())
            receipt_data.update({
                "status": "refused", "phase": phase, "code": "lease_unavailable",
                "recovery": recovery, "recovery_attempted": recovery_attempted,
                "recovery_result": recovery_result, "recovery_steps": list(recovery_steps),
                "recovery_attempts": [step.split(":", 1)[0] for step in recovery_steps],
                "recovery_errors": [step for step in recovery_steps if step.endswith(":failed") or "lease_lost" in step],
            })
            receipt = self._write_receipt(receipt_data)
            return Result("refused", "lease_unavailable", recovery=recovery, durable=receipt is not None,
                          receipt=receipt or "", recovery_attempted=recovery_attempted,
                          recovery_result=recovery_result)
        except Exception:
            recovery, recovery_result, recovery_attempted, recovery_steps = self._recover(hooks, lease, attempted) if lease is not None else ("not_needed", "not_attempted", False, ())
            receipt_data.update({
                "status": "refused", "phase": phase, "code": "binder_failure",
                "recovery": recovery, "recovery_attempted": recovery_attempted,
                "recovery_result": recovery_result, "recovery_steps": list(recovery_steps),
                "recovery_attempts": [step.split(":", 1)[0] for step in recovery_steps],
                "recovery_errors": [step for step in recovery_steps if step.endswith(":failed") or "lease_lost" in step],
            })
            receipt = self._write_receipt(receipt_data)
            return Result("refused", "binder_failure", recovery=recovery, durable=receipt is not None,
                          receipt=receipt or "", recovery_attempted=recovery_attempted,
                          recovery_result=recovery_result)
        finally:
            if lease is not None:
                try:
                    lease.close()
                except Exception:
                    pass

    def _write_receipt(self, value: dict[str, object]) -> str | None:
        if self._receipt_writer is None:
            return None
        safe = {key: value[key] for key in ("contract", "status", "phase", "code", "recovery",
                                             "recovery_attempted", "recovery_result", "recovery_steps",
                                             "recovery_attempts", "recovery_errors") if key in value}
        try:
            result = self._receipt_writer(safe)
            return result if isinstance(result, str) else "injected"
        except Exception:
            return None


class DurableReceiptWriter:
    """Continuous create-only writer backed by the reviewed Linux private store.

    The caller supplies an existing owned 0700, symlink-free parent. No
    fallback mkdir, pathname-reopen, saved-receipt adoption or replay exists.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self._store = None
        self._name = "binder-" + uuid.uuid4().hex
        self._sequence = 0

    def __call__(self, value: dict[str, object]) -> str:
        if set(value) - {"contract", "status", "phase", "code", "recovery", "recovery_attempted",
                         "recovery_result", "recovery_steps", "recovery_attempts", "recovery_errors"}:
            raise BinderError("receipt_fields_invalid")
        try:
            if self._store is None:
                self._store = PrivateReceiptStore(str(self.directory), self._name).create()
            if self._store.save(value) is not True:
                raise BinderError("receipt_not_durable")
            self._sequence += 1
            return str(self.directory / self._name / f"receipt-{self._sequence:06d}.json")
        except (ReceiptError, ValueError, OSError):
            raise BinderError("receipt_not_durable") from None

    def close(self) -> None:
        if self._store is not None:
            self._store.close()


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    runtime_root: Path = Path("/data/markethub")
    release_root: Path = Path("/data/MarketHub2/current")
    api_url: str = "http://127.0.0.1:8803"
    task_center_url: str = "http://127.0.0.1:8810"
    approval_path: Path = Path("/data/markethub/audit/spec36-approved-candidate.json")
    marker_path: Path = Path("/data/markethub/data-update/pending-due-owner")


class LinuxEvidenceCollector:
    """Live production adapter; all observations are recollected from source."""

    def __init__(self, paths: RuntimePaths = RuntimePaths(), *, run: Callable[..., bytes] | None = None,
                 now_ns: Callable[[], int] = time.time_ns, monotonic_ns: Callable[[], int] = time.monotonic_ns):
        self.paths = paths
        self.run = run or self._run
        self.now_ns = now_ns
        self.monotonic_ns = monotonic_ns
        self.session = uuid.uuid4().hex
        self.sequence = 0
        self.last_api: Process | None = None
        self.owner_api: Process | None = None
        self.last_profile: tuple | None = None
        self.last_capture_rows: tuple[dict[str, Any], ...] | None = None
        self.last_tasks: tuple[Task, ...] | None = None

    @staticmethod
    def _run(args: tuple[str, ...]) -> bytes:
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"}
        try:
            return subprocess.check_output(args, stderr=subprocess.DEVNULL, env=env, timeout=30)
        except (OSError, subprocess.SubprocessError):
            raise BinderError("command_failed") from None

    def _systemctl(self, scope: str, args: tuple[str, ...]) -> bytes:
        if scope == "system":
            command = ("/usr/bin/systemctl",) + args
        elif scope == "user" and os.geteuid() == 1000:
            command = ("/usr/bin/systemctl", "--user") + args
        elif scope == "user" and os.geteuid() == 0:
            command = ("/usr/bin/sudo", "-n", "-u", "yosef", "/usr/bin/env", "XDG_RUNTIME_DIR=/run/user/1000", "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus", "/usr/bin/systemctl", "--user") + args
        else:
            raise BinderError("service_manager_identity_invalid")
        return self.run(command)

    def _read_live_profile(self, pid: int, expected: tuple | None) -> tuple:
        if expected is None or _profile_parts(expected) is None:
            raise GateRejected("live_profile_missing")
        expected_cwd, expected_env = _profile_parts(expected)  # type: ignore[misc]
        try:
            cwd = str(Path(f"/proc/{pid}/cwd").resolve(strict=True))
            raw = Path(f"/proc/{pid}/environ").read_bytes()
            environment: dict[str, str] = {}
            for item in raw.split(b"\0"):
                if not item:
                    continue
                key_raw, value_raw = item.split(b"=", 1)
                key = os.fsdecode(key_raw)
                if key in environment:
                    raise ValueError
                environment[key] = os.fsdecode(value_raw)
        except (OSError, UnicodeError, ValueError):
            raise GateRejected("api_profile_read_failed") from None
        if cwd != expected_cwd:
            raise GateRejected("api_profile_mismatch")
        whitelist = tuple((key, environment.get(key)) for key, _ in expected_env)
        return (("cwd", cwd), ("environment", whitelist))

    def _show_api(self, *, allow_stopped: bool = False, expected_profile: tuple | None = None) -> tuple[dict[str, str], Process | None, tuple]:
        fields = {"ActiveState", "SubState", "MainPID", "Environment", "ExecStart", "WorkingDirectory", "FragmentPath"}
        result: dict[str, str] = {}
        for line in self._systemctl("system", ("show", "markethub-api.service", "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", "-p", "Environment", "-p", "ExecStart", "-p", "WorkingDirectory", "-p", "FragmentPath")).decode("ascii", "replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                if key in fields:
                    result[key] = value
        state = (result.get("ActiveState"), result.get("SubState"))
        if state == ("inactive", "dead") and allow_stopped:
            profile = self.last_profile
            if profile is None:
                raise GateRejected("old_profile_missing")
            return result, None, profile
        pid_text = result.get("MainPID", "")
        if state != ("active", "running") or not pid_text.isdigit() or int(pid_text) <= 0:
            raise GateRejected("api_not_running")
        pid = int(pid_text)
        try:
            stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields_after_comm = stat_text.rsplit(")", 1)[1].split()
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip().replace("-", "")
        except (OSError, UnicodeError):
            raise GateRejected("api_identity_invalid") from None
        if len(fields_after_comm) <= 19 or not fields_after_comm[19].isdigit() or not _HEX32.fullmatch(boot):
            raise GateRejected("api_identity_invalid")
        profile = self._read_live_profile(pid, expected_profile)
        value = Process(pid, int(fields_after_comm[19]), boot)
        self.last_api = value
        self.last_profile = profile
        if self.owner_api is None:
            self.owner_api = value
        return result, value, profile

    def _inventory(self, scope: str) -> tuple[tuple[Unit, ...], tuple[Unit, ...]]:
        try:
            raw = reviewed_service_inventory(scope == "user")
            services = tuple(Unit(scope, row["unit"], row["active"], row["sub"], int(row["main_pid"]), int(row["control_pid"]), row.get("job", "")) for row in raw)
            return services, ()
        except Exception:
            raise GateRejected("service_inventory_failed") from None

    def _timer_inventory(self) -> tuple[Unit, ...]:
        try:
            rows = LinuxTimerIO().inventory()
            return tuple(Unit(row["scope"], row["unit"], row["active"], row["sub"], 0, 0, row["job"], tuple(row["stop_dependents"])) for row in rows)
        except (BindingError, OSError, ValueError):
            raise GateRejected("timer_inventory_failed") from None

    def _api_json(self, path: str) -> object:
        try:
            with urllib.request.urlopen(self.paths.api_url.rstrip("/") + path, timeout=20) as response:
                return json.load(response)
        except Exception:
            raise BinderError("api_read_failed") from None

    def _task_json(self, path: str) -> object:
        try:
            with urllib.request.urlopen(self.paths.task_center_url.rstrip("/") + path, timeout=20) as response:
                return json.load(response)
        except Exception:
            raise BinderError("task_center_read_failed") from None

    def _approval(self) -> tuple[str, str, Approval]:
        try:
            raw = self.paths.approval_path.read_bytes()
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            candidate = data["candidate"]
            candidate_digest = data["candidate_digest"]
            old_raw = data.get("old_profile", data.get("profile"))
            candidate_raw = data.get("candidate_profile", data.get("profile"))
            old_profile = _profile(old_raw)
            candidate_profile = _profile(candidate_raw)
            expires_ns = int(data["expires_ns"])
            if not isinstance(candidate, str) or not candidate.startswith("/") or not isinstance(candidate_digest, str):
                raise ValueError
            manifest_path = data.get("candidate_manifest", data.get("candidate_manifest_path", data.get("manifest_path", "")))
            manifest_bound = False
            if manifest_path:
                manifest_file = Path(str(manifest_path))
                manifest_data = json.loads(manifest_file.read_bytes())
                if not isinstance(manifest_data, dict):
                    raise ValueError
                bound_values = tuple(manifest_data.get(key) for key in (
                    "source_hash", "source_sha256", "source_digest", "input_archive_sha256", "sha256"))
                if candidate_digest not in bound_values:
                    raise ValueError
                manifest_path = str(manifest_file)
                manifest_bound = True
            approval = Approval(
                _safe_digest(raw), candidate_digest, candidate, candidate_profile, expires_ns,
                old_profile=old_profile, candidate_profile=candidate_profile,
                candidate_manifest=str(manifest_path), manifest_bound=manifest_bound,
                candidate_version=str(data.get("candidate_version", data.get("version", ""))),
                candidate_data_version=str(data.get("candidate_data_version", data.get("data_version", ""))),
            )
            return candidate, _canonical_digest(dict(candidate_profile)), approval
        except GateRejected:
            raise
        except Exception:
            raise GateRejected("approval_read_failed") from None

    def _marker(self) -> tuple[Marker, bytes]:
        try:
            raw = self.paths.marker_path.read_bytes()
        except FileNotFoundError:
            return Marker(), b""
        except OSError:
            raise GateRejected("marker_read_failed") from None
        return Marker(True, _safe_digest(raw), (len(raw),)), raw

    def _journal_proof(self, api: Process, marker_raw: bytes, marker: Marker) -> OwnerJournal:
        try:
            raw = self.run(("/usr/bin/journalctl", "-b", api.boot, "-u", "markethub-api.service", f"_PID={api.pid}", f"_BOOT_ID={api.boot}", "--no-pager", "-o", "json", "--grep", "memory_(start|finish) label=capture.run_due"))
            text = raw.decode("utf-8", "strict")
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
            if not isinstance(records, list) or len(records) > 1024:
                raise ValueError
            if marker.exists:
                clock = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
                proof = verify_pending_owner(marker_raw, records, pid=api.pid, boot_id=api.boot, start_ticks=api.start_ticks, clock_ticks_per_second=clock)
                return OwnerJournal(True, api.pid, api.start_ticks, api.boot, proof["journal_sha256"], "owner_journal_v2", len(proof["completed_pairs"]))
            # No pending owner exists.  Still validate that any returned rows are
            # current-process rows; a digest alone is never completion authority.
            for row in records:
                if not isinstance(row, dict) or row.get("_PID") != str(api.pid) or row.get("_BOOT_ID") != api.boot or row.get("_SYSTEMD_UNIT") != "markethub-api.service":
                    raise ValueError
            return OwnerJournal(False, api.pid, api.start_ticks, api.boot, _safe_digest(raw), "no_pending_marker", 0)
        except (OwnerJournalRejected, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise GateRejected("owner_journal_invalid") from None

    def _lock_owner(self, st: os.stat_result) -> tuple[int, int]:
        try:
            lines = Path("/proc/locks").read_text(encoding="ascii").splitlines()
        except OSError:
            raise GateRejected("lock_owner_unavailable") from None
        expected_device = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
        owners = []
        for line in lines:
            fields = line.split()
            waiting = len(fields) > 1 and fields[1] == "->"
            if waiting:
                fields = fields[:1] + fields[2:]
            if len(fields) < 8:
                raise GateRejected("lock_inventory_invalid")
            try:
                major, minor, inode = fields[5].split(":")
                identity = (int(major, 16), int(minor, 16), int(inode))
            except (ValueError, TypeError):
                raise GateRejected("lock_inventory_invalid") from None
            if identity != expected_device:
                continue
            if waiting:
                raise GateRejected("lock_waiter_present")
            if not fields[4].isdigit() or int(fields[4]) <= 0:
                raise GateRejected("lock_owner_invalid")
            pid = int(fields[4])
            try:
                stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
                stat_fields = stat_text.rsplit(")", 1)[1].split()
                ticks = int(stat_fields[19]) if len(stat_fields) > 19 and stat_fields[19].isdigit() else 0
            except (OSError, ValueError):
                ticks = 0
            if ticks <= 0:
                raise GateRejected("lock_owner_invalid")
            owners.append((pid, ticks))
        if len(owners) > 1:
            raise GateRejected("lock_inventory_ambiguous")
        return owners[0] if owners else (0, 0)

    def _locks(self) -> tuple[LockRow, ...]:
        rows: list[LockRow] = []
        for path in REQUIRED_LOCK_PATHS:
            try:
                st = os.stat(path, follow_symlinks=False)
            except OSError:
                raise GateRejected("lock_identity_invalid") from None
            pid, ticks = self._lock_owner(st)
            rows.append(LockRow(path, st.st_dev, st.st_ino, pid, ticks))
        return tuple(rows)

    def collect(self) -> Observation:
        return self.collect_for_phase(AdmissionPhase.PRELEASE)

    def collect_for_phase(self, phase: AdmissionPhase) -> Observation:
        self.sequence += 1
        observed = self.now_ns()
        monotonic = self.monotonic_ns()
        candidate, approved_profile, approval = self._approval()
        expected_profile = approval.candidate_profile if phase is AdmissionPhase.CANDIDATE_HEALTH_BEFORE_COMMIT else approval.old_profile
        if expected_profile is None:
            expected_profile = approval.profile
        allow_stopped = phase is AdmissionPhase.STOPPED_BEFORE_COMMIT
        props, api, live_profile = self._show_api(allow_stopped=allow_stopped, expected_profile=expected_profile)
        if api is not None and self.owner_api is None:
            self.owner_api = api
        system_services, _ = self._inventory("system")
        user_services, _ = self._inventory("user")
        timers = self._timer_inventory()
        if api is not None:
            capture_rows = self._api_json("/api/admin/capture-runs?limit=500")
            running_rows = self._api_json("/api/admin/capture-runs?status=running&limit=500")
            try:
                capture_rows = normalize_capture_response(capture_rows)
                running_rows = normalize_capture_response(running_rows)
            except OwnerJournalRejected:
                raise GateRejected("capture_inventory_invalid") from None
            if running_rows:
                raise GateRejected("active_capture")
            if any(not isinstance(row, dict) for row in capture_rows):
                raise GateRejected("capture_inventory_invalid")
            captures = tuple(capture_rows)
            self.last_capture_rows = captures
        elif self.last_capture_rows is not None:
            # This is an in-process retained observation, never a receipt or
            # persisted JSON authority; stopped phase intentionally makes no HTTP call.
            captures = self.last_capture_rows
        else:
            raise GateRejected("terminal_state_unavailable")
        if api is not None:
            task_rows = self._task_json("/api/tasks")
            if not isinstance(task_rows, list):
                raise GateRejected("task_inventory_invalid")
            tasks: list[Task] = []
            for task_id in REQUIRED_TASK_IDS:
                row = next((item for item in task_rows if isinstance(item, dict) and item.get("task_id") == task_id), None)
                if not isinstance(row, dict):
                    raise GateRejected("task_inventory_incomplete")
                runs = self._task_json("/api/tasks/" + task_id + "/runs?limit=1")
                try:
                    runs = normalize_capture_response(runs)
                except OwnerJournalRejected:
                    raise GateRejected("task_run_inventory_invalid") from None
                run = runs[0] if runs else {}
                tasks.append(Task(task_id, int(run.get("id", 0) or 0), str(run.get("status", row.get("last_status", "unknown"))), str(run.get("started_at", row.get("last_started_at", ""))), str(run.get("finished_at", row.get("last_finished_at", ""))), _canonical_digest(run)))
            self.last_tasks = tuple(tasks)
        elif self.last_tasks is not None:
            tasks = list(self.last_tasks)
        else:
            raise GateRejected("terminal_state_unavailable")
        marker, marker_raw = self._marker()
        owner_api = self.owner_api
        if owner_api is None:
            raise GateRejected("owner_journal_api_missing")
        owner = self._journal_proof(owner_api, marker_raw, marker)
        health_ok = True
        health_version = ""
        health_data_version = ""
        if phase is AdmissionPhase.CANDIDATE_HEALTH_BEFORE_COMMIT:
            health = self._api_json("/api/health")
            if isinstance(health, dict):
                health_version = str(health.get("version", ""))
                health_data_version = str(health.get("data_version", ""))
            health_ok = (
                isinstance(health, dict)
                and health.get("status") in ("ok", "healthy")
                and bool(approval.candidate_version)
                and bool(approval.candidate_data_version)
                and health_version == approval.candidate_version
                and health_data_version == approval.candidate_data_version
            )
        try:
            release = str(self.paths.release_root.resolve(strict=True))
        except OSError:
            raise GateRejected("release_read_failed") from None
        api_state = "stopped" if (props.get("ActiveState"), props.get("SubState")) == ("inactive", "dead") and api is None else props.get("ActiveState", "")
        return Observation(self.session, self.sequence, monotonic, observed, api, api_state, live_profile, system_services + user_services, timers, True, tuple(tasks), tuple(captures), marker, release, candidate, approval, owner, self._locks(), phase=phase.value, health_ok=health_ok, health_version=health_version, health_data_version=health_data_version)



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SPEC36 T2 read-only production binder probe")
    parser.add_argument("--probe", action="store_true", help="explicit read-only probe; also the default")
    args = parser.parse_args(argv)
    del args
    result = Binder(LinuxEvidenceCollector()).probe()
    print(json.dumps({"status": result.status, "code": result.code, "recovery": result.recovery, "durable": result.durable}, sort_keys=True))
    return 0 if result.status == "probe" else 2


if __name__ == "__main__":
    raise SystemExit(main())





