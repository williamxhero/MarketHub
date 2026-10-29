"""Pure, offline MH36 phase-aware admission evaluator v3.

This module performs no subprocess, filesystem, network, SSH, or service IO.
This evaluator runs inside ONE trusted deployment process, not a Python sandbox.
The upcoming runtime binder must establish producer freshness, complete OS/task/
journal inventories, trusted approval-file/profile contents, real service-event
provenance and live lease continuity. Typed evidence only records that binder
contract; dataclasses, sequence numbers and synthetic test flags do NOT establish
reality or authenticate events. Missing/malformed contract data rejects.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace, is_dataclass
from copy import deepcopy
from functools import wraps
import types
import typing
from enum import Enum
import re
from typing import Optional


REQUIRED_LOCK_PATHS = frozenset(
    {
        "/data/markethub/audit/stock-bar-maintenance.lock",
        "/data/markethub/locks/global-data-update.lock",
    }
)
REQUIRED_USER_UNITS = frozenset(
    f"xdn-task-markethub_global_data_update{suffix}.service"
    for suffix in ("", "_0030", "_0400")
)
REQUIRED_OWNER_TASKS = frozenset(unit.removesuffix('.service').removeprefix('xdn-task-') for unit in REQUIRED_USER_UNITS)
REQUIRED_SYSTEM_UNITS = frozenset({'markethub-api.service', 'markethub-stock-money-flow-backfill.service'})


class AdmissionRejected(RuntimeError):
    """Sanitized refusal from the pure admission state machine."""


class Phase(Enum):
    PRELEASE = "prelease"
    OWNED_PAUSING = "owned-pausing"
    BEFORE_STOP = "before-stop"
    STOPPED_BEFORE_COMMIT = "stopped-before-commit"
    CANDIDATE_HEALTH_BEFORE_COMMIT = "candidate-health-before-commit"


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    start_ticks: int

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("positive pid required")
        if type(self.start_ticks) is not int or self.start_ticks <= 0:
            raise ValueError("positive start_ticks required")


@dataclass(frozen=True, slots=True)
class ActorIdentity:
    pid: int
    start_ticks: int

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("positive actor pid required")
        if type(self.start_ticks) is not int or self.start_ticks <= 0:
            raise ValueError("positive actor start_ticks required")


@dataclass(frozen=True, slots=True)
class ServiceProfile:
    release: str
    cwd: str
    environment: tuple[tuple[str, Optional[str]], ...]

    @classmethod
    def from_mapping(
        cls,
        *,
        release: str,
        cwd: str,
        environment: Mapping[str, Optional[str]],
    ) -> "ServiceProfile":
        return cls(release, cwd, tuple(sorted(environment.items())))

    def __post_init__(self) -> None:
        if not isinstance(self.release, str) or not self.release:
            raise ValueError("release required")
        if not isinstance(self.cwd, str) or not self.cwd.startswith("/"):
            raise ValueError("absolute cwd required")
        if not isinstance(self.environment, tuple) or not self.environment:
            raise ValueError("nonempty immutable environment required")
        keys: set[str] = set()
        for item in self.environment:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("environment entries must be pairs")
            key, value = item
            if (
                not isinstance(key, str)
                or not key
                or key in keys
                or (value is not None and not isinstance(value, str))
            ):
                raise ValueError("invalid environment entry")
            keys.add(key)


@dataclass(frozen=True, slots=True)
class ApiObservation:
    state: str
    identity: Optional[ProcessIdentity]
    profile: Optional[ServiceProfile]

    @classmethod
    def running(cls, identity: ProcessIdentity, profile: ServiceProfile) -> "ApiObservation":
        return cls("running", identity, profile)

    @classmethod
    def stopped(cls) -> "ApiObservation":
        return cls("stopped", None, None)

    def __post_init__(self) -> None:
        if self.state not in {"running", "stopped"}:
            raise ValueError("unsupported API state")
        if self.state == "running" and (
            not isinstance(self.identity, ProcessIdentity)
            or not isinstance(self.profile, ServiceProfile)
        ):
            raise ValueError("running API requires identity and profile")
        if self.state == "stopped" and (self.identity is not None or self.profile is not None):
            raise ValueError("stopped API cannot carry a live identity")


@dataclass(frozen=True, slots=True)
class TaskRun:
    status: str
    finished_at: str
    task_id: str = ''
    run_id: str = ''
    journal_id: str = ''
    journal_digest: str = ''
    start_event_id: str = ''
    finish_event_id: str = ''
    api_identity: Optional[ProcessIdentity] = None

    def __post_init__(self) -> None:
        if self.status not in {"success", "failed"} or not isinstance(self.finished_at, str) or not self.finished_at:
            raise ValueError("owner task must be terminal with finished_at")


@dataclass(frozen=True, slots=True)
class UnitObservation:
    unit: str
    active: str
    sub: str
    main_pid: str
    control_pid: str
    job: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.unit, str) or not self.unit:
            raise ValueError("unit name required")
        if not all(isinstance(value, str) for value in (self.active, self.sub, self.main_pid, self.control_pid, self.job)):
            raise ValueError("unit fields must be strings")
        if not self.main_pid.isascii() or not self.main_pid.isdigit():
            raise ValueError("numeric MainPID required")
        if not self.control_pid.isascii() or not self.control_pid.isdigit():
            raise ValueError("numeric ControlPID required")


@dataclass(frozen=True, slots=True)
class OwnerEvidence:
    run_id: str
    start_event_id: str
    finish_event_id: str
    evidence_digest: str
    start_count: int
    finish_count: int
    finish_after_start: bool
    api_identity: ProcessIdentity

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in (
            self.run_id,
            self.start_event_id,
            self.finish_event_id,
            self.evidence_digest,
        )):
            raise ValueError("immutable owner evidence identifiers required")
        if type(self.start_count) is not int or type(self.finish_count) is not int:
            raise ValueError("owner evidence counts must be integers")
        if self.start_count < 1 or self.start_count != self.finish_count:
            raise ValueError("owner evidence must contain matching nonempty pairs")
        if self.finish_after_start is not True:
            raise ValueError("owner finish must follow owner start")
        if not isinstance(self.api_identity, ProcessIdentity):
            raise ValueError("owner evidence API identity required")


@dataclass(frozen=True, slots=True)
class OwnerCompletionProof:
    api_identity: ProcessIdentity
    evidence: OwnerEvidence
    task_id: str = ''
    journal_id: str = ''
    provenance: Optional[ProducerEvidence] = None

    def __post_init__(self) -> None:
        if not isinstance(self.api_identity, ProcessIdentity) or not isinstance(self.evidence, OwnerEvidence):
            raise ValueError("typed owner completion evidence required")
        if self.evidence.api_identity != self.api_identity:
            raise ValueError("owner evidence must bind to the API PID and start_ticks")


@dataclass(frozen=True, slots=True)
class LockIdentity:
    path: str
    device: int
    inode: int

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.startswith("/"):
            raise ValueError("absolute lock path required")
        if type(self.device) is not int or self.device <= 0:
            raise ValueError("positive device required")
        if type(self.inode) is not int or self.inode <= 0:
            raise ValueError("positive inode required")


@dataclass(frozen=True, slots=True)
class LockRow:
    identity: LockIdentity
    owning_pid: int
    kind: str
    mode: str
    owning_start_ticks: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.identity, LockIdentity):
            raise ValueError("typed lock identity required")
        if type(self.owning_pid) is not int or self.owning_pid <= 0:
            raise ValueError("positive lock owning pid required")
        if self.kind != "flock" or self.mode != "WRITE":
            raise ValueError("only exclusive flock owner rows are admissible")


@dataclass(frozen=True, slots=True)
class LiveLeaseProof:
    lease_id: str
    owner: ActorIdentity
    valid: bool
    locks: tuple[LockIdentity, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.lease_id, str) or not self.lease_id:
            raise ValueError("lease id required")
        if not isinstance(self.owner, ActorIdentity):
            raise ValueError("typed lease owner required")
        if type(self.valid) is not bool:
            raise ValueError("live lease validity must be an observed bool")
        if not isinstance(self.locks, tuple) or len(self.locks) != len(REQUIRED_LOCK_PATHS):
            raise ValueError("exact required lock identities required")
        if any(not isinstance(lock, LockIdentity) for lock in self.locks):
            raise ValueError("typed lease lock identities required")
        if len(set(self.locks)) != len(self.locks):
            raise ValueError("duplicate lease lock identity")


@dataclass(frozen=True, slots=True)
class AdapterStopProof:
    adapter_actor: ActorIdentity
    lease_id: str
    prior_api_identity: ProcessIdentity
    stop_event_id: str
    live_adapter_event: bool
    stopped_verified: bool
    session_id: str = ''
    snapshot_sequence: int = 0
    observed_ns: int = 0
    prior_snapshot_sequence: int = 0
    evidence: Optional[ProducerEvidence] = None

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_actor, ActorIdentity) or not isinstance(self.prior_api_identity, ProcessIdentity):
            raise ValueError("typed stop provenance required")
        if not isinstance(self.lease_id, str) or not self.lease_id:
            raise ValueError("stop lease id required")
        if not isinstance(self.stop_event_id, str) or not self.stop_event_id:
            raise ValueError("adapter-owned stop event required")
        if type(self.live_adapter_event) is not bool or type(self.stopped_verified) is not bool:
            raise ValueError("stop provenance must be observed booleans")


@dataclass(frozen=True, slots=True)
class CandidateHealthObservation:
    start_event_id: str
    health_observation_id: str
    identity: ProcessIdentity
    profile: ServiceProfile
    data_version: str
    live_adapter_start_event: bool
    health_ok: bool

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ProcessIdentity) or not isinstance(self.profile, ServiceProfile):
            raise ValueError("typed candidate health identity/profile required")
        if not all(isinstance(value, str) and value for value in (
            self.start_event_id,
            self.health_observation_id,
            self.data_version,
        )):
            raise ValueError("candidate health observation identifiers and data_version required")
        if type(self.live_adapter_start_event) is not bool or type(self.health_ok) is not bool:
            raise ValueError("candidate health observation booleans required")


@dataclass(frozen=True, slots=True)
class CandidateHealthProof:
    adapter_actor: ActorIdentity
    lease_id: str
    start_event_id: str
    health_observation_id: str
    identity: ProcessIdentity
    profile: ServiceProfile
    data_version: str
    live_adapter_start_event: bool
    health_ok: bool
    session_id: str = ''
    snapshot_sequence: int = 0
    observed_ns: int = 0
    prior_snapshot_sequence: int = 0
    stop_event_id: str = ''
    approval_id: str = ''
    evidence: Optional[ProducerEvidence] = None

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_actor, ActorIdentity) or not isinstance(self.identity, ProcessIdentity) or not isinstance(self.profile, ServiceProfile):
            raise ValueError("typed candidate provenance required")
        if not all(isinstance(value, str) and value for value in (
            self.lease_id,
            self.start_event_id,
            self.health_observation_id,
            self.data_version,
        )):
            raise ValueError("candidate provenance identifiers and data_version required")
        if type(self.live_adapter_start_event) is not bool or type(self.health_ok) is not bool:
            raise ValueError("candidate provenance must be observed booleans")

    def as_observation(self) -> CandidateHealthObservation:
        return CandidateHealthObservation(
            start_event_id=self.start_event_id,
            health_observation_id=self.health_observation_id,
            identity=self.identity,
            profile=self.profile,
            data_version=self.data_version,
            live_adapter_start_event=self.live_adapter_start_event,
            health_ok=self.health_ok,
        )


@dataclass(frozen=True, slots=True)
class DynamicExternalSnapshot:
    observation_status: str
    gate_version: int
    raw_safe_to_switch_release: bool
    raw_safe_to_launch_second_writer: bool
    running_captures: tuple[str, ...]
    latest_owner_tasks: tuple[TaskRun, ...]
    pending_due_owner_path_exists: bool
    system_units: tuple[UnitObservation, ...]
    user_units: tuple[UnitObservation, ...]
    api: ApiObservation
    relevant_lock_rows: tuple[LockRow, ...]
    candidate_health: Optional[CandidateHealthObservation] = None
    session_id: str = ''
    sequence: int = 0
    observed_ns: int = 0
    live_lease: Optional[LiveLeaseProof] = None
    inventory: Optional[CompleteInventory] = None
    owner_journal: Optional[OwnerJournal] = None
    stop_event: Optional[AdapterStopProof] = None
    evidence: Optional[ProducerEvidence] = None

    def __post_init__(self) -> None:
        if self.observation_status not in {"ok", "missing", "failed"}:
            raise ValueError("observation status required")
        if type(self.gate_version) is not int:
            raise ValueError("gate version required")
        if type(self.raw_safe_to_switch_release) is not bool or type(self.raw_safe_to_launch_second_writer) is not bool:
            raise ValueError("raw v2 booleans must be observed booleans")
        if not isinstance(self.running_captures, tuple) or any(not isinstance(item, str) for item in self.running_captures):
            raise ValueError("typed capture observations required")
        if not isinstance(self.latest_owner_tasks, tuple) or any(not isinstance(item, TaskRun) for item in self.latest_owner_tasks):
            raise ValueError("typed owner task observations required")
        if type(self.pending_due_owner_path_exists) is not bool:
            raise ValueError("pending owner path must be observed bool")
        if not isinstance(self.system_units, tuple) or any(not isinstance(item, UnitObservation) for item in self.system_units):
            raise ValueError("typed system unit observations required")
        if not isinstance(self.user_units, tuple) or any(not isinstance(item, UnitObservation) for item in self.user_units):
            raise ValueError("typed user unit observations required")
        if not isinstance(self.api, ApiObservation):
            raise ValueError("typed API observation required")
        if not isinstance(self.relevant_lock_rows, tuple) or any(not isinstance(item, LockRow) for item in self.relevant_lock_rows):
            raise ValueError("typed lock-row observations required")
        if self.candidate_health is not None and not isinstance(self.candidate_health, CandidateHealthObservation):
            raise ValueError("typed candidate health observation required")

    @classmethod
    def strict_test_snapshot(
        cls,
        *,
        api: ApiObservation,
        raw_safe_to_switch_release: bool,
        raw_safe_to_launch_second_writer: bool,
        relevant_lock_rows: tuple[LockRow, ...] = (),
        observation_status: str = "ok",
        candidate_health: Optional[CandidateHealthObservation] = None,
    ) -> "DynamicExternalSnapshot":
        system = (
            UnitObservation(
                "markethub-api.service",
                "active" if api.state == "running" else "inactive",
                "running" if api.state == "running" else "dead",
                str(api.identity.pid) if api.identity else "0",
                "0",
            ),
            UnitObservation("markethub-stock-money-flow-backfill.service", "inactive", "dead", "0", "0"),
        )
        user = tuple(
            UnitObservation(unit, "inactive", "dead", "0", "0")
            for unit in sorted(REQUIRED_USER_UNITS)
        )
        return cls(
            observation_status=observation_status,
            gate_version=2,
            raw_safe_to_switch_release=raw_safe_to_switch_release,
            raw_safe_to_launch_second_writer=raw_safe_to_launch_second_writer,
            running_captures=(),
            latest_owner_tasks=(TaskRun("success", "2026-10-29T10:00:00Z"),),
            pending_due_owner_path_exists=False,
            system_units=system,
            user_units=user,
            api=api,
            relevant_lock_rows=relevant_lock_rows,
            candidate_health=candidate_health,
        )


@dataclass(frozen=True, slots=True)
class AdmissionObservation:
    phase: Phase
    owner_api_identity: ProcessIdentity
    candidate_api_identity: Optional[ProcessIdentity]


def _reject(message: str) -> None:
    raise AdmissionRejected(message)


def _is_idle(unit: UnitObservation) -> bool:
    return (
        (unit.active, unit.sub) in {("inactive", "dead"), ("failed", "failed")}
        and unit.main_pid == "0"
        and unit.control_pid == "0"
        and unit.job == ""
    )


def _validate_external(
    external: DynamicExternalSnapshot,
    *,
    expected_api_state: str,
    expected_identity: Optional[ProcessIdentity],
    expected_profile: Optional[ServiceProfile],
    require_strict_flags: bool,
) -> None:
    if not isinstance(external, DynamicExternalSnapshot) or external.observation_status != "ok":
        _reject("external observation missing or failed")
    if external.gate_version != 2:
        _reject("v2 gate version required")
    if require_strict_flags and not (
        external.raw_safe_to_switch_release is True
        and external.raw_safe_to_launch_second_writer is True
    ):
        _reject("strict prelease v2 booleans required")
    if external.running_captures:
        _reject("running capture observed")
    if not external.latest_owner_tasks or any(
        task.status not in {"success", "failed"} or not task.finished_at
        for task in external.latest_owner_tasks
    ):
        _reject("owner task is not complete")
    if external.pending_due_owner_path_exists:
        _reject("pending due owner path exists")
    if not external.system_units or not external.user_units:
        _reject("complete unit inventory required")

    system_names = [row.unit for row in external.system_units]
    user_names = [row.unit for row in external.user_units]
    if len(set(system_names)) != len(system_names) or len(set(user_names)) != len(user_names):
        _reject("duplicate unit inventory")
    if any(not re.fullmatch(r"[A-Za-z0-9_@.\-]+\.service", name) for name in system_names + user_names):
        _reject("invalid unit identifier")
    if any(not name.startswith("markethub") for name in system_names):
        _reject("unexpected system service")
    if any(not name.startswith("xdn-task-markethub") for name in user_names):
        _reject("unexpected Task Center service")
    if not REQUIRED_SYSTEM_UNITS.issubset(system_names) or not REQUIRED_USER_UNITS.issubset(user_names):
        _reject("required service inventory incomplete")

    api_rows = [row for row in external.system_units if row.unit == "markethub-api.service"]
    if len(api_rows) != 1:
        _reject("API service inventory incomplete")
    api_row = api_rows[0]
    if api_row.control_pid != "0" or api_row.job != "":
        _reject("API service has active control/job state")
    if expected_api_state == "running":
        if external.api.state != "running" or external.api.identity is None or external.api.profile is None:
            _reject("running API observation required")
        if (api_row.active, api_row.sub) != ("active", "running") or api_row.main_pid != str(external.api.identity.pid):
            _reject("API unit does not match running API identity")
    elif expected_api_state == "stopped":
        if external.api.state != "stopped" or (api_row.active, api_row.sub, api_row.main_pid) != ("inactive", "dead", "0"):
            _reject("stopped API observation required")
    else:
        _reject("unsupported expected API state")

    for row in external.system_units:
        if row.unit != "markethub-api.service" and not _is_idle(row):
            _reject("active or unknown system writer")
    for row in external.user_units:
        if not _is_idle(row):
            _reject("active or unknown Task Center writer")

    if expected_identity is not None and external.api.identity != expected_identity:
        _reject("API identity mismatch")
    if expected_profile is not None and external.api.profile != expected_profile:
        _reject("API profile mismatch")


def _same_lease_identity(left: LiveLeaseProof, right: LiveLeaseProof) -> bool:
    return (isinstance(left, LiveLeaseProof) and isinstance(right, LiveLeaseProof)
        and left.lease_id == right.lease_id
        and left.owner == right.owner
        and set(left.locks) == set(right.locks))


def _validate_lease(actor: ActorIdentity, lease: LiveLeaseProof, external: DynamicExternalSnapshot) -> None:
    try:
        if not isinstance(lease, LiveLeaseProof) or lease.valid is not True:
            _reject("live lease.valid required")
        if lease.owner != actor:
            _reject("lease owner is not the same process")
        if {lock.path for lock in lease.locks} != REQUIRED_LOCK_PATHS:
            _reject("lease does not own the exact required lock paths")
        if any(lock.path not in REQUIRED_LOCK_PATHS for lock in lease.locks):
            _reject("lease contains unexpected lock")
        if (not _same_lease_identity(external.live_lease, lease)
                or external.live_lease.valid != lease.valid):
            _reject("lease must match binder live lease observation")
        rows = external.relevant_lock_rows
        if len(rows) != len(REQUIRED_LOCK_PATHS):
            _reject("lock-row inventory has a foreign or missing owner")
        if len({row.identity for row in rows}) != len(rows):
            _reject("duplicate lock-row identity")
        lease_identities = set(lease.locks)
        if {row.identity for row in rows} != lease_identities:
            _reject("lock rows do not match live lease identities")
        if any((row.owning_pid, row.owning_start_ticks) != (actor.pid, actor.start_ticks) for row in rows):
            _reject("lock row is not attributed to the owning process PID/start_ticks")
    except AdmissionRejected:
        raise
    except Exception:
        _reject("malformed lease or lock observation")




@dataclass(frozen=True, slots=True)
class ProducerEvidence:
    """Binder-provided provenance metadata, NOT cryptographic authentication."""
    producer_id: str
    kind: str
    record_id: str


@dataclass(frozen=True, slots=True)
class CandidateApproval:
    approval_id: str
    session_id: str
    actor: ActorIdentity
    profile: ServiceProfile
    not_before_ns: int
    expires_ns: int
    approval_file: str
    approval_digest: str


@dataclass(frozen=True, slots=True)
class TrustContract:
    """Pinned by the trusted deployment caller, never taken from candidate claims.

    The binder verifies approval_file ownership/content/digest and actual profile,
    authenticates producer events, supplies complete inventory, and supplies a live
    monotonic now_ns in the same clock domain as observed_ns. This pure evaluator
    checks internal consistency only. It cannot detect a producer that lies.
    """
    approval: CandidateApproval
    trusted_adapter_id: str
    required_owner_tasks: frozenset[str] = REQUIRED_OWNER_TASKS
    owner_task_id: str = 'markethub_global_data_update'
    max_snapshot_age_ns: int = 5_000_000_000


@dataclass(frozen=True, slots=True)
class CompleteInventory:
    complete: bool
    scopes: frozenset[str]
    system_units: tuple[str, ...]
    user_units: tuple[str, ...]
    task_ids: tuple[str, ...]


INVENTORY_SCOPES = frozenset({'all-markethub-system-services',
    'all-markethub-user-services', 'all-required-owner-tasks',
    'all-relevant-locks', 'all-running-captures', 'owner-journal', 'pending-owner-path'})


@dataclass(frozen=True, slots=True)
class OwnerJournal:
    task_id: str
    journal_id: str
    evidence: OwnerEvidence


def _typed(value, annotation):
    """Revalidate detached, exact typed values, including forged/corrupted dataclasses."""
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        for arg in args:
            try:
                _typed(value, arg)
                return
            except (AdmissionRejected, TypeError, ValueError):
                pass
        _reject('malformed optional/union evidence')
    if origin in (tuple, frozenset):
        if type(value) is not origin:
            _reject('immutable typed collection required')
        if origin is tuple and len(args) > 1 and args[-1] is not Ellipsis:
            if len(value) != len(args):
                _reject('malformed typed tuple')
            for item, arg in zip(value, args):
                _typed(item, arg)
        else:
            for item in value:
                _typed(item, args[0])
        return
    if type(value) is not annotation:
        _reject('exact typed evidence required')
    if is_dataclass(value):
        hints = typing.get_type_hints(type(value))
        for field in fields(value):
            _typed(getattr(value, field.name), hints[field.name])
        post = getattr(value, '__post_init__', None)
        if post:
            post()


def _producer_evidence(evidence, kind, producer_id, record_id):
    if (type(evidence) is not ProducerEvidence or not producer_id
            or evidence.producer_id != producer_id or evidence.kind != kind
            or not record_id or evidence.record_id != record_id):
        _reject('typed binder provenance metadata required')


def _inventory(external, contract):
    marker = external.inventory
    if (marker is None or marker.complete is not True or marker.scopes != INVENTORY_SCOPES):
        _reject('complete binder inventory marker required')
    tasks = external.latest_owner_tasks
    ids = tuple(task.task_id for task in tasks)
    if (len(set(ids)) != len(ids) or frozenset(ids) != contract.required_owner_tasks
            or tuple(sorted(ids)) != marker.task_ids
            or tuple(sorted(row.unit for row in external.system_units)) != marker.system_units
            or tuple(sorted(row.unit for row in external.user_units)) != marker.user_units):
        _reject('complete required task/service inventory mismatch')
    if not REQUIRED_OWNER_TASKS.issubset(contract.required_owner_tasks):
        _reject('trusted required task set cannot weaken the baseline')
    if any(not task.run_id or not task.journal_id or not task.journal_digest
           or not task.start_event_id or not task.finish_event_id or task.api_identity is None
           for task in tasks):
        _reject('complete task/journal metadata required')


def _owner_binding(owner, external, contract):
    journal = external.owner_journal
    if journal is None or journal.task_id != contract.owner_task_id:
        _reject('owner journal observation required')
    if owner.task_id != journal.task_id or owner.journal_id != journal.journal_id or owner.evidence != journal.evidence:
        _reject('owner proof must match observed journal metadata')
    tasks = [task for task in external.latest_owner_tasks if task.task_id == owner.task_id]
    if len(tasks) != 1:
        _reject('unique owner task required')
    task, evidence = tasks[0], owner.evidence
    if (task.run_id != evidence.run_id or task.journal_id != owner.journal_id
            or task.journal_digest != evidence.evidence_digest
            or task.start_event_id != evidence.start_event_id
            or task.finish_event_id != evidence.finish_event_id
            or task.api_identity != owner.api_identity
            or evidence.start_event_id == evidence.finish_event_id):
        _reject('owner proof is not bound to task/run/journal/events/API metadata')


def _boundary(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except AdmissionRejected:
            raise
        except Exception:
            raise AdmissionRejected('malformed admission input') from None
    return call


@dataclass(frozen=True, slots=True)
class _State:
    actor: ActorIdentity
    owner: OwnerCompletionProof
    initial_profile: ServiceProfile
    contract: TrustContract
    phase: Phase
    sequence: int
    observed_ns: int
    now_ns: int
    replay_sequence: int = 0
    replay_observed_ns: int = 0
    replay_now_ns: int = 0
    lease: Optional[LiveLeaseProof] = None
    candidate: Optional[CandidateHealthProof] = None
    stop: Optional[AdapterStopProof] = None
    invalidated: bool = False


def _fresh(state, external, now_ns):
    _typed(external, DynamicExternalSnapshot)
    if type(now_ns) is not int or now_ns <= 0 or now_ns < state.now_ns:
        _reject('trusted monotonic evaluation time required')
    if (external.session_id != state.contract.approval.session_id
            or type(external.sequence) is not int or external.sequence <= state.replay_sequence
            or type(external.observed_ns) is not int or external.observed_ns <= state.replay_observed_ns
            or external.observed_ns > now_ns
            or now_ns < state.replay_now_ns
            or now_ns - external.observed_ns > state.contract.max_snapshot_age_ns):
        _reject('stale, replayed, future or wrong-session dynamic snapshot')
    if not state.contract.approval.not_before_ns <= now_ns < state.contract.approval.expires_ns:
        _reject('trusted approval expired or not yet valid')
    _producer_evidence(external.evidence, 'snapshot', state.contract.trusted_adapter_id, f'snapshot-{external.sequence}')
    _inventory(external, state.contract)
    _owner_binding(state.owner, external, state.contract)
    return replace(state, replay_sequence=external.sequence,
        replay_observed_ns=external.observed_ns, replay_now_ns=now_ns)


class AdmissionSession:
    """Single-use, same-actor phase contract; not a same-process security enclave.

    Ordinary attribute writes are blocked to prevent accidental phase/lease reset.
    Arbitrary bytecode, object.__setattr__, imports and introspection in this trusted
    process are deliberately not a threat model. Binder provenance is trusted.
    """
    __slots__ = ('_state',)

    def __new__(cls, *args, **kwargs):
        raise TypeError('use AdmissionSession.begin for strict prelease admission')

    def __init_subclass__(cls, **kwargs):
        raise TypeError('admission sessions cannot be subclassed')

    def __setattr__(self, name, value):
        raise AttributeError('admission state is not an instance attribute')

    def __delattr__(self, name):
        raise AttributeError('admission state is not an instance attribute')

    def _get_state(self):
        state = getattr(self, '_state', None)
        if type(state) is not _State:
            _reject('session was not admitted through begin')
        return state

    @classmethod
    @_boundary
    def begin(cls, *, actor, external, owner, approved_candidate_profile,
              contract=None, now_ns=None):
        actor, external, owner, approved_candidate_profile, contract = deepcopy(
            (actor, external, owner, approved_candidate_profile, contract))
        _typed(actor, ActorIdentity)
        _typed(owner, OwnerCompletionProof)
        _typed(approved_candidate_profile, ServiceProfile)
        _typed(contract, TrustContract)
        approval = contract.approval
        if (approval.actor != actor or not approval.session_id or not approval.approval_id
                or approval.profile != approved_candidate_profile
                or contract.owner_task_id not in contract.required_owner_tasks
                or type(contract.max_snapshot_age_ns) is not int
                or not 0 < contract.max_snapshot_age_ns <= 60_000_000_000):
            _reject('explicit trusted approval contract mismatch')
        if (not approval.approval_file.startswith('/')
                or not re.fullmatch(r'[0-9a-f]{64}', approval.approval_digest)
                or not contract.trusted_adapter_id):
            _reject('explicit trusted approval-file/profile contract required')
        _producer_evidence(owner.provenance, 'owner', contract.trusted_adapter_id, owner.journal_id)
        state = _State(actor, owner, external.api.profile if type(external) is DynamicExternalSnapshot else None,
            contract, Phase.PRELEASE, 0, 0, 0)
        state = _fresh(state, external, now_ns)
        _validate_external(external, expected_api_state='running', expected_identity=owner.api_identity,
            expected_profile=state.initial_profile, require_strict_flags=True)
        if external.relevant_lock_rows:
            _reject('prelease requires no relevant locks')
        session = object.__new__(cls)
        object.__setattr__(session, '_state', replace(state, sequence=external.sequence,
            observed_ns=external.observed_ns, now_ns=now_ns))
        return session

    @property
    @_boundary
    def phase(self):
        return self._get_state().phase

    @property
    @_boundary
    def invalidated(self):
        return self._get_state().invalidated

    @property
    @_boundary
    def owner(self):
        return deepcopy(self._get_state().owner)

    @_boundary
    def _advance(self, phase, *, actor, lease, external, now_ns=None, stop=None, candidate=None, _allow_same_phase=False):
        state = self._get_state()
        if state.invalidated:
            _reject('admission session permanently invalidated')
        order = {Phase.OWNED_PAUSING: (Phase.PRELEASE, Phase.OWNED_PAUSING),
            Phase.BEFORE_STOP: (Phase.OWNED_PAUSING,),
            Phase.STOPPED_BEFORE_COMMIT: (Phase.BEFORE_STOP,),
            Phase.CANDIDATE_HEALTH_BEFORE_COMMIT: (Phase.STOPPED_BEFORE_COMMIT,)}
        if state.phase not in order[phase]:
            _reject('phase transition out of order')
        _typed(actor, ActorIdentity)
        if actor != state.actor:
            _reject('same-process actor identity required')
        try:
            lease = deepcopy(lease)
            _typed(lease, LiveLeaseProof)
            if state.lease is not None and not _same_lease_identity(lease, state.lease):
                _reject('continuous lease identity changed')
            # Never touch malformed external fields in lease validation.
            if lease.valid is not True:
                _reject('live lease.valid required')
            if type(external) is not DynamicExternalSnapshot:
                _reject('typed external snapshot required')
            _validate_lease(actor, lease, external)
        except Exception:
            object.__setattr__(self, '_state', replace(state, invalidated=True))
            raise
        external, stop, candidate = deepcopy((external, stop, candidate))
        state = _fresh(state, external, now_ns)
        # Persist the replay watermark before phase-specific safety/candidate checks.
        # A newer observation that is rejected below must not leave an older snapshot
        # eligible for a later retry. Accepted transition fields remain separate.
        object.__setattr__(self, '_state', state)
        expected_prior = {Phase.OWNED_PAUSING: (Phase.PRELEASE, Phase.OWNED_PAUSING) if _allow_same_phase else (Phase.PRELEASE,),
            Phase.BEFORE_STOP: (Phase.OWNED_PAUSING,),
            Phase.STOPPED_BEFORE_COMMIT: (Phase.BEFORE_STOP,),
            Phase.CANDIDATE_HEALTH_BEFORE_COMMIT: (Phase.STOPPED_BEFORE_COMMIT,)}
        if state.phase not in expected_prior.get(phase, ()):
            _reject('phase transition out of order')
        if phase in (Phase.OWNED_PAUSING, Phase.BEFORE_STOP):
            _validate_external(external, expected_api_state='running', expected_identity=state.owner.api_identity,
                expected_profile=state.initial_profile, require_strict_flags=False)
        elif phase is Phase.STOPPED_BEFORE_COMMIT:
            _typed(stop, AdapterStopProof)
            _producer_evidence(stop.evidence, 'stop', state.contract.trusted_adapter_id, stop.stop_event_id)
            if (stop.adapter_actor != actor or stop.lease_id != lease.lease_id
                    or stop.prior_api_identity != state.owner.api_identity
                    or stop.live_adapter_event is not True or stop.stopped_verified is not True
                    or not stop.stop_event_id or external.stop_event != stop
                    or stop.session_id != external.session_id
                    or stop.snapshot_sequence != external.sequence
                    or stop.observed_ns != external.observed_ns
                    or stop.prior_snapshot_sequence != state.sequence):
                _reject('stop proof must bind binder event and current snapshot/lease')
            _validate_external(external, expected_api_state='stopped', expected_identity=None,
                expected_profile=None, require_strict_flags=False)
        else:
            _typed(candidate, CandidateHealthProof)
            _producer_evidence(candidate.evidence, 'candidate', state.contract.trusted_adapter_id, candidate.health_observation_id)
            if (candidate.adapter_actor != actor or candidate.lease_id != lease.lease_id
                    or candidate.live_adapter_start_event is not True or candidate.health_ok is not True
                    or candidate.identity == state.owner.api_identity
                    or candidate.profile != state.contract.approval.profile
                    or candidate.approval_id != state.contract.approval.approval_id
                    or candidate.session_id != external.session_id
                    or candidate.snapshot_sequence != external.sequence
                    or candidate.observed_ns != external.observed_ns
                    or candidate.prior_snapshot_sequence != state.sequence
                    or state.stop is None or candidate.stop_event_id != state.stop.stop_event_id
                    or candidate.start_event_id == candidate.stop_event_id
                    or candidate.start_event_id == candidate.health_observation_id
                    or candidate.as_observation() != external.candidate_health):
                _reject('candidate proof must bind approved profile, binder start/health/stop and snapshot')
            _validate_external(external, expected_api_state='running', expected_identity=candidate.identity,
                expected_profile=state.contract.approval.profile, require_strict_flags=False)
        state = replace(state, phase=phase, lease=lease, sequence=external.sequence,
            observed_ns=external.observed_ns, now_ns=now_ns,
            stop=stop if phase is Phase.STOPPED_BEFORE_COMMIT else state.stop,
            candidate=candidate if phase is Phase.CANDIDATE_HEALTH_BEFORE_COMMIT else state.candidate)
        object.__setattr__(self, '_state', state)
        return deepcopy(AdmissionObservation(state.phase, state.owner.api_identity,
            state.candidate.identity if state.candidate else None))

    def admit_owned(self, **kwargs):
        return self._advance(Phase.OWNED_PAUSING, **kwargs)

    def observe_owned_pausing(self, **kwargs):
        return self._advance(Phase.OWNED_PAUSING, _allow_same_phase=True, **kwargs)

    def enter_before_stop(self, **kwargs):
        return self._advance(Phase.BEFORE_STOP, **kwargs)

    def enter_stopped_before_commit(self, **kwargs):
        return self._advance(Phase.STOPPED_BEFORE_COMMIT, **kwargs)

    def enter_candidate_health_before_commit(self, **kwargs):
        return self._advance(Phase.CANDIDATE_HEALTH_BEFORE_COMMIT, **kwargs)

