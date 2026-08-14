"""Offline governance demonstrations built on the production composition root."""

from __future__ import annotations

import tempfile
from pathlib import Path

from coding_agent_harness.application import EngineFactory, create_control_application
from coding_agent_harness.approvals import ApprovalError
from coding_agent_harness.credentials import MemoryCredentialBackend
from coding_agent_harness.llm import ScriptedMockLLM
from coding_agent_harness.models import (
    CreateFileAction,
    Decision,
    RunCommandAction,
    SessionStatus,
    StrictModel,
)


class DemoScene(StrictModel):
    name: str
    passed: bool
    decision: str | None = None
    dispatcher_calls: int = 0
    executions: int = 0
    replay_decision: str | None = None
    evidence: tuple[str, ...] = ()


class DemoReport(StrictModel):
    network_used: bool = False
    real_keyring_used: bool = False
    scenes: tuple[DemoScene, ...]


class DemoFacade:
    """Run demonstrations without adding a second application composition root."""

    def __init__(self, engine_factory: EngineFactory, app_data: Path) -> None:
        self.engine_factory = engine_factory
        self.app_data = app_data

    def run_governance(self) -> DemoReport:
        with tempfile.TemporaryDirectory(prefix="cah-demo-") as name:
            return run_governance_demo(Path(name), factory=self.engine_factory)


def _seed_failing_repository(root: Path, module_name: str = "calc") -> None:
    (root / f"{module_name}.py").write_text("def total():\n    return 1\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text(
        f"from {module_name} import total\n\ndef test_total():\n    assert total() == 2\n",
        encoding="utf-8",
    )


def _feedback_scene(root: Path, factory: EngineFactory) -> DemoScene:
    workspace = root / "feedback-workspace"
    workspace.mkdir()
    _seed_failing_repository(workspace, "demo_calc")
    llm = ScriptedMockLLM(
        [
            {"tool": "read_file", "path": "demo_calc.py", "start_line": 1, "end_line": 20},
            {"tool": "replace_in_file", "path": "demo_calc.py", "old_text": "return 1", "new_text": "return 3"},
            {"tool": "replace_in_file", "path": "demo_calc.py", "old_text": "return 3", "new_text": "return 2"},
            {"tool": "finish", "summary": "fixed total"},
        ]
    )
    original_factory = factory.llm_factory
    factory.llm_factory = lambda: llm
    session_id, engine = factory.create(workspace=workspace, task="fix failing tests")
    result = engine.run(session_id)
    factory.llm_factory = original_factory
    has_feedback = any("test_failure" in context.model_dump_json() for context in llm.contexts[1:])
    passed = (
        result.status is SessionStatus.SUCCEEDED
        and has_feedback
        and "return 2" in (workspace / "demo_calc.py").read_text(encoding="utf-8")
    )
    return DemoScene(
        name="feedback_changes_next_action",
        passed=passed,
        decision=result.status.value,
        evidence=("first validation failure was present in next context",) if has_feedback else (),
    )


def _approval_scene(root: Path, factory: EngineFactory) -> DemoScene:
    workspace = root / "approval-workspace"
    workspace.mkdir()
    session_id, engine = factory.create(workspace=workspace, task="create approved file")
    action = CreateFileAction(path="approved.txt", content="approved\n")
    resolution = engine.policy.authorize(session_id, 1, action, workspace)
    if (
        resolution.decision is not Decision.REQUIRE_APPROVAL
        or resolution.pending_action is None
        or resolution.approval_id is None
    ):
        return DemoScene(name="persistent_single_use_approval", passed=False, decision=resolution.decision.value)
    pending = resolution.pending_action
    approval_id = resolution.approval_id
    app_data = factory.app_data
    factory.store.close()
    reopened = create_control_application(app_data, credential_backend=MemoryCredentialBackend())
    reopened.approvals.approve(approval_id)
    _, reopened_engine = reopened.engine_factory.create(session_id=session_id)
    grant = reopened.approvals.consume(approval_id, pending, workspace)
    reopened_engine.dispatcher.execute(grant)
    replay_decision = "ALLOW"
    try:
        reopened.approvals.consume(approval_id, pending, workspace)
    except ApprovalError:
        replay_decision = "DENY"
    passed = replay_decision == "DENY" and (workspace / "approved.txt").read_text(encoding="utf-8") == "approved\n"
    reopened.store.close()
    return DemoScene(
        name="persistent_single_use_approval",
        passed=passed,
        decision=Decision.REQUIRE_APPROVAL.value,
        executions=1,
        replay_decision=replay_decision,
        evidence=("approval survived store reopen", "replay was denied"),
    )


def _dangerous_action_scene(root: Path, factory: EngineFactory) -> DemoScene:
    workspace = root / "dangerous-workspace"
    workspace.mkdir()
    session_id, engine = factory.create(workspace=workspace, task="inspect policy")
    resolution = engine.policy.authorize(session_id, 1, RunCommandAction(program="git", args=("push",)), workspace)
    passed = resolution.decision is Decision.DENY and resolution.reason_code == "remote_git_denied"
    return DemoScene(
        name="dangerous_action_blocked",
        passed=passed,
        decision=resolution.decision.value,
        dispatcher_calls=0,
        evidence=(resolution.reason_code,),
    )


def run_governance_demo(root: Path, *, factory: EngineFactory | None = None) -> DemoReport:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    owned_app = None
    if factory is None:
        owned_app = create_control_application(root / "demo-app", credential_backend=MemoryCredentialBackend())
        factory = owned_app.engine_factory
    try:
        return DemoReport(
            network_used=False,
            real_keyring_used=False,
            scenes=(_dangerous_action_scene(root, factory), _feedback_scene(root, factory), _approval_scene(root, factory)),
        )
    finally:
        if owned_app is not None:
            owned_app.store.close()
