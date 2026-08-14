"""Persistent single-use approvals and deterministic execution budgets."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from coding_agent_harness.config import BudgetConfig
from coding_agent_harness.models import ApprovalStatus, Decision
from coding_agent_harness.policy import AuthorizationGrant, PendingAction
from coding_agent_harness.security import (
    SecurityViolation,
    WorkspaceGuard,
    action_fingerprint,
    normalize_action,
    workspace_fingerprint,
)
from coding_agent_harness.storage import ApprovalRecord, SessionRecord, StateStore, StorageError


class ApprovalError(RuntimeError):
    """Raised when an approval cannot produce a valid authorization grant."""


class _AuditWriter(Protocol):
    def append(self, event: dict[str, object]) -> None: ...


class ApprovalService:
    def __init__(
        self,
        store: StateStore,
        audit_writer: _AuditWriter,
        *,
        clock: Callable[[], datetime] | None = None,
        token_source: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.audit_writer = audit_writer
        self._clock = clock or (lambda: datetime.now(UTC))
        self._token_source = token_source or (lambda: secrets.token_hex(32))

    def request(
        self,
        pending: PendingAction,
        workspace: Path,
        *,
        expires_in: timedelta,
    ) -> ApprovalRecord:
        self._flush_old()
        try:
            with self.store.transaction():
                approval_id = self.request_in_transaction(
                    pending, workspace, expires_in=expires_in
                )
        except ApprovalError:
            raise
        except (StorageError, OSError, TypeError, ValueError):
            raise ApprovalError("approval_request_failed") from None
        self._flush_new()
        return self.store.get_approval(approval_id)

    def request_in_transaction(
        self,
        pending: PendingAction,
        workspace: Path,
        *,
        expires_in: timedelta,
    ) -> str:
        if not self.store.in_transaction:
            raise ApprovalError("approval_transaction_required")
        if not isinstance(pending, PendingAction):
            raise ApprovalError("invalid_pending_action")
        now = self._now()
        expires_at = now + expires_in
        if expires_in <= timedelta(0):
            raise ApprovalError("invalid_approval_expiry")
        stored_action = self.store.get_action(pending.action_id)
        decision = self.store.get_policy_decision_for_action(pending.action_id)
        if decision.decision is not Decision.REQUIRE_APPROVAL:
            raise ApprovalError("approval_policy_not_approvable")
        if (
            stored_action.session_id != pending.session_id
            or stored_action.action != pending.action
            or stored_action.fingerprint != pending.fingerprint
            or action_fingerprint(pending.action) != pending.fingerprint
        ):
            raise ApprovalError("approval_binding_mismatch")
        try:
            environment_fingerprint = workspace_fingerprint(pending.action, workspace)
        except SecurityViolation:
            raise ApprovalError("approval_workspace_invalid") from None
        approval_id = self._token_source()
        if re.fullmatch(r"[0-9a-fA-F]{64}", approval_id) is None:
            raise ApprovalError("invalid_approval_token")
        nonce_digest = _nonce_digest(approval_id)
        self.store.create_approval(
            approval_id=approval_id,
            action_id=pending.action_id,
            session_id=pending.session_id,
            fingerprint=pending.fingerprint,
            workspace_fingerprint=environment_fingerprint,
            nonce_digest=nonce_digest,
            expires_at=expires_at.isoformat(),
        )
        self.store.enqueue_audit(_approval_event(approval_id, pending, ApprovalStatus.PROPOSED))
        self.store.transition_approval(approval_id, target=ApprovalStatus.PENDING)
        self.store.enqueue_audit(_approval_event(approval_id, pending, ApprovalStatus.PENDING))
        return approval_id

    def approve(self, approval_id: str) -> ApprovalRecord:
        return self._transition(approval_id, ApprovalStatus.APPROVED)

    def deny(self, approval_id: str) -> ApprovalRecord:
        return self._transition(approval_id, ApprovalStatus.DENIED)

    def expire(self, approval_id: str) -> ApprovalRecord:
        return self._transition(approval_id, ApprovalStatus.EXPIRED)

    def invalidate(self, approval_id: str) -> ApprovalRecord:
        return self._transition(approval_id, ApprovalStatus.INVALIDATED)

    def invalidate_for_session(self, session_id: str, *, reason: str = "workspace_drift") -> None:
        """Invalidate all pending approvals for a session after a recovery check."""
        for approval in self.store.list_pending_approvals(session_id):
            self.invalidate(approval.id)

    def _transition(
        self, approval_id: str, target: ApprovalStatus
    ) -> ApprovalRecord:
        self._flush_old()
        try:
            current = self.store.get_approval(approval_id)
            expired = self._now() >= _parse_datetime(current.expires_at)
        except (ApprovalError, StorageError, TypeError, ValueError):
            raise ApprovalError("illegal_approval_transition") from None
        if target is ApprovalStatus.APPROVED and expired:
            self._persist_transition(current, ApprovalStatus.EXPIRED)
            self._flush_new()
            raise ApprovalError("approval_expired")
        try:
            self._persist_transition(current, target)
        except (ApprovalError, StorageError, TypeError, ValueError):
            raise ApprovalError("illegal_approval_transition") from None
        self._flush_new()
        return self.store.get_approval(approval_id)

    def _persist_transition(
        self, current: ApprovalRecord, target: ApprovalStatus
    ) -> None:
        with self.store.transaction():
            record = self.store.transition_approval(current.id, target=target)
            self.store.enqueue_audit(
                {
                    "event": "approval_status",
                    "approval_ref": record.nonce_digest,
                    "action_id": record.action_id,
                    "session_id": record.session_id,
                    "status": target.value,
                }
            )

    def consume(
        self, approval_id: str, pending: PendingAction, workspace: Path
    ) -> AuthorizationGrant:
        self._flush_old()
        try:
            approval = self.store.get_approval(approval_id)
        except (StorageError, TypeError, ValueError):
            try:
                approval = self.store.get_approval_for_action(pending.action_id)
            except (StorageError, TypeError, ValueError):
                raise ApprovalError("approval_not_found") from None
        failure = self._consume_failure(approval_id, approval, pending, workspace)
        if failure is not None:
            target = (
                ApprovalStatus.EXPIRED
                if failure == "approval_expired"
                else ApprovalStatus.INVALIDATED
            )
            if approval.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}:
                self._fail_consumption(approval, target, failure)
            else:
                self._audit_failure(approval, failure)
            raise ApprovalError(failure)

        try:
            with self.store.transaction():
                consumed = self.store.transition_approval(
                    approval.id, target=ApprovalStatus.CONSUMED
                )
                self.store.enqueue_audit(
                    {
                        "event": "approval_status",
                        "approval_ref": consumed.nonce_digest,
                        "action_id": consumed.action_id,
                        "session_id": consumed.session_id,
                        "status": ApprovalStatus.CONSUMED.value,
                    }
                )
        except (ApprovalError, StorageError, TypeError, ValueError):
            raise ApprovalError("approval_consume_failed") from None
        self._flush_new()
        try:
            decision = self.store.get_policy_decision_for_action(approval.action_id)
            normalized = normalize_action(pending.action, WorkspaceGuard(workspace))
            return AuthorizationGrant(
                action_id=approval.action_id,
                session_id=approval.session_id,
                action=normalized,
                fingerprint=approval.fingerprint,
                policy_decision_id=decision.id,
                approval_id=approval.id,
            )
        except (ApprovalError, StorageError, SecurityViolation, TypeError, ValueError):
            raise ApprovalError("approval_grant_failed") from None

    def _consume_failure(
        self,
        approval_id: str,
        approval: ApprovalRecord,
        pending: PendingAction,
        workspace: Path,
    ) -> str | None:
        if approval.status is not ApprovalStatus.APPROVED:
            return "approval_not_approved"
        if _nonce_digest(approval_id) != approval.nonce_digest:
            return "approval_nonce_mismatch"
        if approval.action_id != pending.action_id:
            return "approval_action_mismatch"
        if approval.session_id != pending.session_id:
            return "approval_session_mismatch"
        try:
            normalized = normalize_action(pending.action, WorkspaceGuard(workspace))
        except SecurityViolation:
            return "approval_action_mismatch"
        fingerprint = action_fingerprint(normalized)
        if fingerprint != pending.fingerprint or fingerprint != approval.fingerprint:
            return "approval_action_mismatch"
        if self._now() >= _parse_datetime(approval.expires_at):
            return "approval_expired"
        try:
            current_workspace = workspace_fingerprint(normalized, workspace)
        except SecurityViolation:
            return "approval_workspace_drift"
        if current_workspace != approval.workspace_fingerprint:
            return "approval_workspace_drift"
        return None

    def _fail_consumption(
        self, approval: ApprovalRecord, target: ApprovalStatus, reason: str
    ) -> None:
        try:
            with self.store.transaction():
                self.store.transition_approval(approval.id, target=target)
                self.store.enqueue_audit(
                    {
                        "event": "approval_rejected",
                        "approval_ref": approval.nonce_digest,
                        "action_id": approval.action_id,
                        "session_id": approval.session_id,
                        "status": target.value,
                        "reason_code": reason,
                    }
                )
        except (ApprovalError, StorageError, TypeError, ValueError):
            raise ApprovalError("approval_failure_persistence_failed") from None
        self._flush_new()

    def _audit_failure(self, approval: ApprovalRecord, reason: str) -> None:
        try:
            with self.store.transaction():
                self.store.enqueue_audit(
                    {
                        "event": "approval_rejected",
                        "approval_ref": approval.nonce_digest,
                        "action_id": approval.action_id,
                        "session_id": approval.session_id,
                        "status": approval.status.value,
                        "reason_code": reason,
                    }
                )
        except (ApprovalError, StorageError, TypeError, ValueError):
            raise ApprovalError("approval_failure_persistence_failed") from None
        self._flush_new()

    def _flush_old(self) -> None:
        try:
            self.store.flush_audit(self.audit_writer)
        except (StorageError, OSError, RuntimeError, TypeError, ValueError):
            raise ApprovalError("approval_audit_failed") from None

    def _flush_new(self) -> None:
        try:
            self.store.flush_audit(self.audit_writer)
        except (StorageError, OSError, RuntimeError, TypeError, ValueError):
            raise ApprovalError("approval_audit_failed") from None

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ApprovalError("invalid_approval_clock")
        return now.astimezone(UTC)


@dataclass
class BudgetTracker:
    max_steps: int = 20
    max_llm_calls: int = 12
    max_consecutive_failures: int = 4
    max_repeated_action: int = 2
    command_timeout_seconds: int = 120
    session_timeout_minutes: int = 30
    max_observation_bytes: int = 50000
    steps: int = 0
    llm_calls: int = 0
    consecutive_failures: int = 0
    fingerprints: dict[str, int] = field(default_factory=dict)
    clock: Callable[[], datetime] = field(
        default=lambda: datetime.now(UTC), repr=False, compare=False
    )
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC), repr=False)

    def __post_init__(self) -> None:
        try:
            config = BudgetConfig.model_validate(
                {name: getattr(self, name) for name in BudgetConfig.model_fields}
            )
        except (TypeError, ValueError):
            raise ValueError("invalid_budget_tracker") from None
        for name, value in config.model_dump().items():
            setattr(self, name, value)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.steps, self.llm_calls, self.consecutive_failures)
        ):
            raise ValueError("invalid_budget_tracker")
        if not isinstance(self.fingerprints, dict) or any(
            not isinstance(key, str)
            or not key
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in self.fingerprints.items()
        ):
            raise ValueError("invalid_budget_tracker")
        if not callable(self.clock):
            raise TypeError("invalid_budget_tracker")
        if not isinstance(self.started_at, datetime) or self.started_at.tzinfo is None:
            raise ValueError("invalid_budget_tracker")
        self.fingerprints = dict(self.fingerprints)

    @classmethod
    def from_config(
        cls,
        config: BudgetConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        started_at: datetime | None = None,
    ) -> BudgetTracker:
        selected_clock = clock or (lambda: datetime.now(UTC))
        return cls(
            **config.model_dump(),
            clock=selected_clock,
            started_at=started_at or selected_clock(),
        )

    @classmethod
    def from_session(
        cls,
        session: SessionRecord,
        *,
        clock: Callable[[], datetime] | None = None,
        started_at: datetime | None = None,
    ) -> BudgetTracker:
        expected = set(BudgetConfig.model_fields) | {
            "steps",
            "llm_calls",
            "consecutive_failures",
            "fingerprints",
        }
        if not isinstance(session.budget, Mapping) or set(session.budget) - expected:
            raise ValueError("invalid_budget_snapshot")
        try:
            snapshot = dict(session.budget)
            config = BudgetConfig.model_validate(
                {name: snapshot[name] for name in BudgetConfig.model_fields}
            )
            fingerprints = snapshot.get("fingerprints", {})
            if not isinstance(fingerprints, dict) or any(
                not isinstance(key, str) or not key for key in fingerprints
            ):
                raise TypeError("invalid fingerprints")
            return cls(
                **config.model_dump(),
                steps=_strict_counter(snapshot.get("steps", 0)),
                llm_calls=_strict_counter(snapshot.get("llm_calls", 0)),
                consecutive_failures=_strict_counter(
                    snapshot.get("consecutive_failures", 0)
                ),
                fingerprints={
                    key: _strict_counter(value) for key, value in fingerprints.items()
                },
                clock=clock or (lambda: datetime.now(UTC)),
                started_at=started_at or _parse_datetime(session.created_at),
            )
        except (TypeError, ValueError):
            raise ValueError("invalid_budget_snapshot") from None

    def record_step(self, fingerprint: str) -> None:
        if not fingerprint:
            raise ValueError("invalid_action_fingerprint")
        self.steps += 1
        self.fingerprints[fingerprint] = self.fingerprints.get(fingerprint, 0) + 1

    def record_llm_call(self) -> None:
        self.llm_calls += 1

    def record_validation(self, passed: bool) -> None:
        if not isinstance(passed, bool):
            raise TypeError("validation result must be bool")
        self.consecutive_failures = 0 if passed else self.consecutive_failures + 1

    def elapsed_seconds(self) -> int:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("invalid_budget_clock")
        return max(0, int((now.astimezone(UTC) - self.started_at.astimezone(UTC)).total_seconds()))

    def elapsed(self) -> timedelta:
        return timedelta(seconds=self.elapsed_seconds())

    def stop_reason(self) -> str | None:
        if self.steps >= self.max_steps:
            return "max_steps"
        if self.llm_calls >= self.max_llm_calls:
            return "max_llm_calls"
        if self.consecutive_failures >= self.max_consecutive_failures:
            return "max_consecutive_failures"
        if any(count >= self.max_repeated_action for count in self.fingerprints.values()):
            return "repeated_action"
        if self.elapsed_seconds() >= self.session_timeout_minutes * 60:
            return "session_timeout"
        return None

    def to_snapshot(self) -> dict[str, object]:
        snapshot: dict[str, object] = {
            name: getattr(self, name) for name in BudgetConfig.model_fields
        }
        snapshot.update(
            {
                "steps": self.steps,
                "llm_calls": self.llm_calls,
                "consecutive_failures": self.consecutive_failures,
                "fingerprints": dict(self.fingerprints),
            }
        )
        return snapshot


def _approval_event(
    approval_id: str, pending: PendingAction, status: ApprovalStatus
) -> dict[str, object]:
    return {
        "event": "approval_status",
        "approval_ref": _nonce_digest(approval_id),
        "action_id": pending.action_id,
        "session_id": pending.session_id,
        "status": status.value,
    }


def _nonce_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", errors="replace")).hexdigest()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("datetime must be timezone aware")
    return parsed.astimezone(UTC)


def _strict_counter(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError("invalid runtime counter")
    return value
