from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from coding_agent_harness.models import (
    Decision,
    FinishAction,
    Observation,
    ReplaceInFileAction,
    SessionStatus,
    ValidationResult,
)
from coding_agent_harness.policy import AuthorizationGrant, PolicyResolution
from coding_agent_harness.validation import ValidationStage


class MemoryStore:
    def __init__(self, budget: dict[str, object] | None = None) -> None:
        self.session = SimpleNamespace(
            id="session-1",
            project_id="project-1",
            task="fix bug",
            status=SessionStatus.CREATED,
            budget=budget
            or {
                "max_steps": 20,
                "max_llm_calls": 12,
                "max_consecutive_failures": 4,
                "max_repeated_action": 2,
                "command_timeout_seconds": 120,
                "session_timeout_minutes": 30,
                "max_observation_bytes": 50000,
                "steps": 0,
                "llm_calls": 0,
                "consecutive_failures": 0,
                "fingerprints": {},
            },
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )
        self.observations: list[Observation] = []
        self.validations: list[ValidationResult] = []
        self.pending = False
        self.budget_snapshots: list[dict[str, object]] = []

    def get_session(self, session_id: str):
        assert session_id == self.session.id
        return self.session

    def transition_session(self, session_id: str, status: SessionStatus):
        self.session.status = status
        return self.session

    def save_budget_tracker(self, session_id: str, tracker) -> None:
        snapshot = tracker.to_snapshot() if hasattr(tracker, "to_snapshot") else dict(tracker)
        self.session.budget = snapshot
        self.budget_snapshots.append(snapshot)

    def record_validations(self, session_id: str, results) -> list[str]:
        self.validations.extend(results)
        return [f"validation-{len(self.validations)}"]

    def record_observation(self, session_id: str, observation: Observation) -> str:
        self.observations.append(observation)
        return f"observation-{len(self.observations)}"

    def latest_observation(self, session_id: str):
        return self.observations[-1] if self.observations else None

    def query_context_inputs(self, session_id: str, *, memory_limit: int = 5):
        latest = self.latest_observation(session_id)
        return {
            "task": self.session.task,
            "completion_criteria": "all required validators pass",
            "policy_summary": "governed",
            "validator_summary": self.validations[-1].summary if self.validations else "",
            "current_failure": latest.summary if latest and latest.category != "success" else "",
            "observations": tuple(item.summary for item in self.observations[-5:]),
            "memories": (),
        }

    def list_pending_approvals(self, session_id: str):
        return (object(),) if self.pending else ()


class ScriptedLLM:
    def __init__(self, *actions: object) -> None:
        self.actions = list(actions)
        self.contexts = []

    def next_action(self, context):
        self.contexts.append(context)
        if not self.actions:
            raise AssertionError("script exhausted")
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


class FakePolicy:
    def __init__(self, *decisions: Decision) -> None:
        self.decisions = list(decisions)
        self.calls = 0

    def authorize(self, session_id: str, step: int, action, workspace):
        decision = self.decisions.pop(0) if self.decisions else Decision.ALLOW
        self.calls += 1
        action_id = f"action-{self.calls}"
        if decision is Decision.DENY:
            return PolicyResolution(
                action_id=action_id,
                action=action,
                fingerprint=f"fp-{self.calls}",
                decision=decision,
                reason_code="blocked_by_policy",
            )
        if decision is Decision.REQUIRE_APPROVAL:
            self.pending = True
            from coding_agent_harness.policy import PendingAction

            return PolicyResolution(
                action_id=action_id,
                action=action,
                fingerprint=f"fp-{self.calls}",
                decision=decision,
                reason_code="approval_required",
                pending_action=PendingAction(
                    action_id=action_id,
                    session_id=session_id,
                    action=action,
                    fingerprint=f"fp-{self.calls}",
                ),
                approval_id="approval-1",
            )
        grant = AuthorizationGrant(
            action_id=action_id,
            session_id=session_id,
            action=action,
            fingerprint=f"fp-{self.calls}",
            policy_decision_id=f"decision-{self.calls}",
        )
        return PolicyResolution(
            action_id=action_id,
            action=action,
            fingerprint=f"fp-{self.calls}",
            decision=decision,
            reason_code="allowed",
            grant=grant,
        )


class FakeDispatcher:
    def __init__(self) -> None:
        self.call_count = 0

    def execute(self, grant) -> Observation:
        self.call_count += 1
        return Observation(action_id=grant.action_id, category="success", summary="tool completed")


class FakeValidators:
    def __init__(self, *, final_pass: bool = True, fast_pass: bool = True) -> None:
        self.final_pass = final_pass
        self.fast_pass = fast_pass
        self.final_failures: list[str] = []
        self.calls: list[ValidationStage] = []

    def run(self, stage: ValidationStage, workspace):
        self.calls.append(stage)
        passed = self.final_pass if stage is ValidationStage.FINAL else self.fast_pass
        if stage is ValidationStage.FINAL and self.final_failures:
            passed = False
        return [
            ValidationResult(
                validator_id="pytest",
                stage=stage.value,
                status="passed" if passed else "failed",
                exit_code=0 if passed else 1,
                duration_ms=1,
                summary="passed" if passed else (self.final_failures[0] if self.final_failures else "failed"),
            )
        ]

    def success_gate_open(self, results) -> bool:
        return bool(results) and all(result.status == "passed" for result in results)


@pytest.fixture
def engine_dependencies(workspace):
    store = MemoryStore()
    return {
        "store": store,
        "policy": FakePolicy(),
        "dispatcher": FakeDispatcher(),
        "validators": FakeValidators(),
        "workspace": workspace,
    }


def test_engine_feeds_validation_failure_back_before_success(engine_dependencies):
    deps = engine_dependencies
    deps["validators"].fast_pass = False
    llm = ScriptedLLM(ReplaceInFileAction(path="target.py", old_text="a", new_text="b"), FinishAction(summary="done"))
    from coding_agent_harness.engine import HarnessEngine

    result = HarnessEngine(llm=llm, context_builder=None, **deps).run("session-1")
    assert result.status is SessionStatus.SUCCEEDED
    assert any(context.current_failure for context in llm.contexts[1:])


def test_denied_action_never_reaches_dispatcher(engine_dependencies):
    deps = engine_dependencies
    deps["policy"] = FakePolicy(Decision.DENY, Decision.ALLOW)
    llm = ScriptedLLM(ReplaceInFileAction(path="target.py", old_text="a", new_text="b"), FinishAction(summary="done"))
    from coding_agent_harness.engine import HarnessEngine

    result = HarnessEngine(llm=llm, context_builder=None, **deps).run("session-1")
    assert result.status is SessionStatus.SUCCEEDED
    assert deps["dispatcher"].call_count == 0
    assert deps["store"].latest_observation("session-1").category == "policy_blocked"


def test_finish_with_failed_final_validation_needs_user_decision(engine_dependencies):
    deps = engine_dependencies
    deps["validators"].final_pass = False
    llm = ScriptedLLM(FinishAction(summary="done"))
    from coding_agent_harness.engine import HarnessEngine

    result = HarnessEngine(llm=llm, context_builder=None, **deps).run("session-1")
    assert result.status is SessionStatus.NEEDS_USER_DECISION
    assert result.next_commands
    assert deps["store"].session.status is not SessionStatus.SUCCEEDED


def test_approval_request_is_persisted_before_pause(engine_dependencies):
    deps = engine_dependencies
    deps["policy"] = FakePolicy(Decision.REQUIRE_APPROVAL)
    llm = ScriptedLLM(ReplaceInFileAction(path="target.py", old_text="a", new_text="b"))
    from coding_agent_harness.engine import HarnessEngine

    result = HarnessEngine(llm=llm, context_builder=None, **deps).run("session-1")
    assert result.status is SessionStatus.PAUSED_APPROVAL
    assert deps["dispatcher"].call_count == 0


def test_second_protocol_error_pauses(engine_dependencies):
    deps = engine_dependencies
    llm = ScriptedLLM(ValueError("bad action"), ValueError("bad action"))
    from coding_agent_harness.engine import HarnessEngine

    result = HarnessEngine(llm=llm, context_builder=None, **deps).run("session-1")
    assert result.status is SessionStatus.PAUSED_PROTOCOL_ERROR


def test_budget_limit_pauses_before_next_llm_call(engine_dependencies):
    deps = engine_dependencies
    deps["store"] = MemoryStore({
        "max_steps": 1,
        "max_llm_calls": 1,
        "max_consecutive_failures": 4,
        "max_repeated_action": 2,
        "command_timeout_seconds": 120,
        "session_timeout_minutes": 30,
        "max_observation_bytes": 50000,
        "steps": 0,
        "llm_calls": 0,
        "consecutive_failures": 0,
        "fingerprints": {},
    })
    llm = ScriptedLLM(ReplaceInFileAction(path="target.py", old_text="a", new_text="b"))
    from coding_agent_harness.engine import HarnessEngine

    result = HarnessEngine(llm=llm, context_builder=None, **deps).run("session-1")
    assert result.status is SessionStatus.PAUSED_LIMIT_REACHED


@pytest.mark.parametrize(
    "status",
    [
        SessionStatus.PAUSED_APPROVAL,
        SessionStatus.PAUSED_LIMIT_REACHED,
        SessionStatus.PAUSED_PROTOCOL_ERROR,
        SessionStatus.PAUSED_WORKSPACE_DRIFT,
        SessionStatus.PAUSED_INTERNAL_ERROR,
        SessionStatus.SUCCEEDED,
        SessionStatus.NEEDS_USER_DECISION,
        SessionStatus.CHANGES_KEPT,
        SessionStatus.ROLLED_BACK,
    ],
)
def test_engine_rejects_non_runnable_session_before_llm_or_tool_calls(
    engine_dependencies, status: SessionStatus
) -> None:
    deps = engine_dependencies
    deps["store"].session.status = status
    llm = ScriptedLLM(FinishAction(summary="must not run"))
    from coding_agent_harness.engine import HarnessEngine

    with pytest.raises(ValueError, match="session_not_runnable"):
        HarnessEngine(llm=llm, context_builder=None, **deps).run("session-1")

    assert llm.contexts == []
    assert deps["dispatcher"].call_count == 0
