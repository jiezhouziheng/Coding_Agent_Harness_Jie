"""The governed agent feedback loop."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from coding_agent_harness.approvals import BudgetTracker
from coding_agent_harness.context import ContextBuilder
from coding_agent_harness.llm import LLMError
from coding_agent_harness.models import (
    Decision,
    FinishAction,
    Observation,
    SessionStatus,
    StrictModel,
)
from coding_agent_harness.security import redact_text
from coding_agent_harness.validation import ValidationStage, observation_from_validation


class SessionResult(StrictModel):
    """Stable value returned by an engine run."""
    session_id: str
    status: SessionStatus
    stop_reason: str
    next_commands: tuple[str, ...] = ()


class HarnessEngine:
    def __init__(
        self,
        *,
        llm: Any,
        store: Any,
        policy: Any,
        dispatcher: Any,
        validators: Any,
        workspace: Path,
        context_builder: ContextBuilder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.llm = llm
        self.store = store
        self.policy = policy
        self.dispatcher = dispatcher
        self.validators = validators
        self.workspace = workspace
        self.context_builder = context_builder or ContextBuilder()
        self.clock = clock

    def _tracker(self, session_id: str) -> BudgetTracker:
        return BudgetTracker.from_session(
            self.store.get_session(session_id), clock=self.clock
        )

    def _pause(self, session_id: str, status: SessionStatus, reason: str) -> SessionResult:
        current = self.store.get_session(session_id).status
        if current is not status:
            self.store.transition_session(session_id, status)
        return SessionResult(session_id=session_id, status=status, stop_reason=reason)

    def _context(self, session_id: str) -> Any:
        return self.context_builder.from_store(self.store, session_id)

    def run(self, session_id: str) -> SessionResult:
        session = self.store.get_session(session_id)
        if session.status is SessionStatus.CREATED:
            baseline = self.validators.run(ValidationStage.BASELINE, self.workspace)
            self.store.record_validations(session_id, baseline)
            if not self.validators.success_gate_open(baseline):
                self.store.record_observation(
                    session_id, observation_from_validation("", baseline)
                )
            self.store.transition_session(session_id, SessionStatus.RUNNING)

        tracker = self._tracker(session_id)
        protocol_errors = 0
        while True:
            if reason := tracker.stop_reason():
                self.store.save_budget_tracker(session_id, tracker)
                return self._pause(session_id, SessionStatus.PAUSED_LIMIT_REACHED, reason)

            try:
                context = self._context(session_id)
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                summary = redact_text(str(error))[:400]
                self.store.record_observation(
                    session_id,
                    Observation(category="tool_error", summary="context_build_failed", evidence=summary),
                )
                return self._pause(session_id, SessionStatus.PAUSED_INTERNAL_ERROR, "context_build_failed")
            tracker.record_llm_call()
            self.store.save_budget_tracker(session_id, tracker)
            try:
                action = self.llm.next_action(context)
            except LLMError as error:
                summary = redact_text(str(error))[:400]
                self.store.record_observation(
                    session_id,
                    Observation(category="tool_error", summary="llm_error", evidence=summary),
                )
                return self._pause(session_id, SessionStatus.PAUSED_INTERNAL_ERROR, "llm_error")
            except (ValidationError, ValueError, TypeError):
                protocol_errors += 1
                self.store.record_observation(
                    session_id,
                    Observation(category="tool_error", summary="invalid_llm_action"),
                )
                if protocol_errors >= 2:
                    return self._pause(
                        session_id, SessionStatus.PAUSED_PROTOCOL_ERROR, "two_protocol_errors"
                    )
                continue

            protocol_errors = 0
            try:
                resolution = self.policy.authorize(
                    session_id, tracker.steps + 1, action, self.workspace
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                self.store.record_observation(
                    session_id,
                    Observation(category="tool_error", summary="policy_authorization_failed"),
                )
                return self._pause(
                    session_id, SessionStatus.PAUSED_INTERNAL_ERROR, "policy_authorization_failed"
                )

            tracker.record_step(resolution.fingerprint)
            self.store.save_budget_tracker(session_id, tracker)
            if resolution.decision is Decision.DENY:
                self.store.record_observation(
                    session_id,
                    Observation(
                        action_id=resolution.action_id,
                        category="policy_blocked",
                        summary=resolution.reason_code,
                    ),
                )
                continue
            if resolution.decision is Decision.REQUIRE_APPROVAL:
                if resolution.pending_action is None or resolution.approval_id is None:
                    return self._pause(
                        session_id, SessionStatus.PAUSED_INTERNAL_ERROR, "missing_pending_action"
                    )
                return self._pause(
                    session_id, SessionStatus.PAUSED_APPROVAL, resolution.reason_code
                )

            if isinstance(resolution.action, FinishAction):
                final = self.validators.run(ValidationStage.FINAL, self.workspace)
                self.store.record_validations(session_id, final)
                if self.validators.success_gate_open(final):
                    self.store.transition_session(session_id, SessionStatus.SUCCEEDED)
                    return SessionResult(
                        session_id=session_id,
                        status=SessionStatus.SUCCEEDED,
                        stop_reason="final_validation_passed",
                    )
                self.store.record_observation(
                    session_id, observation_from_validation(resolution.action_id, final)
                )
                self.store.transition_session(session_id, SessionStatus.NEEDS_USER_DECISION)
                return SessionResult(
                    session_id=session_id,
                    status=SessionStatus.NEEDS_USER_DECISION,
                    stop_reason="final_validation_failed",
                    next_commands=(
                        f"cah changes show {session_id}",
                        f"cah changes keep {session_id}",
                        f"cah changes rollback {session_id}",
                    ),
                )

            if resolution.grant is None:
                return self._pause(
                    session_id, SessionStatus.PAUSED_INTERNAL_ERROR, "missing_authorization_grant"
                )
            try:
                observation = self.dispatcher.execute(resolution.grant)
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                summary = redact_text(str(error))[:400]
                self.store.record_observation(
                    session_id,
                    Observation(category="tool_error", summary="tool_execution_failed", evidence=summary),
                )
                return self._pause(session_id, SessionStatus.NEEDS_USER_DECISION, "tool_execution_failed")
            self.store.record_observation(session_id, observation)
            if resolution.action.tool in {"replace_in_file", "create_file", "delete_file"}:
                fast = self.validators.run(ValidationStage.FAST, self.workspace)
                self.store.record_validations(session_id, fast)
                passed = self.validators.success_gate_open(fast)
                tracker.record_validation(passed)
                self.store.save_budget_tracker(session_id, tracker)
                self.store.record_observation(
                    session_id, observation_from_validation(resolution.action_id, fast)
                )
