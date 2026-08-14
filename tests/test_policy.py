from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from coding_agent_harness.config import BudgetConfig
from coding_agent_harness.models import Decision, parse_action
from coding_agent_harness.policy import (
    AuthorizationGrant,
    PendingAction,
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    PolicyGateway,
    PolicyGatewayError,
    PolicyResolution,
)
from coding_agent_harness.storage import StateStore, StorageError


def _prepare_workspace(workspace: Path) -> None:
    (workspace / "src").mkdir(exist_ok=True)
    (workspace / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    (workspace / "src" / "requirements.txt").write_text("content\n", encoding="utf-8")
    (workspace / "pkg").mkdir(exist_ok=True)
    for name in ("pyproject.toml", "requirements.txt", "uv.lock", "Makefile"):
        (workspace / name).write_text("content\n", encoding="utf-8")
    workflow = workspace / ".github" / "workflows"
    workflow.mkdir(parents=True, exist_ok=True)
    (workflow / "ci.yml").write_text("name: ci\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("payload", "expected", "reason_code"),
    [
            ({"tool": "list_files", "path": "src"}, Decision.ALLOW, "low_risk_workspace_action"),
            ({"tool": "list_files", "glob": "*.py"}, Decision.ALLOW, "low_risk_workspace_action"),
            ({"tool": "list_files", "glob": "src/**/*.py"}, Decision.ALLOW, "low_risk_workspace_action"),
            ({"tool": "list_files", "glob": ".git/**"}, Decision.DENY, "unsafe_glob_pattern"),
            ({"tool": "list_files", "glob": ".env*"}, Decision.DENY, "unsafe_glob_pattern"),
            ({"tool": "list_files", "glob": "../**"}, Decision.DENY, "unsafe_glob_pattern"),
            ({"tool": "list_files", "glob": "C:/**"}, Decision.DENY, "unsafe_glob_pattern"),
        ({"tool": "read_file", "path": "src/a.py"}, Decision.ALLOW, "low_risk_workspace_action"),
        (
            {
                "tool": "replace_in_file",
                "path": "src/a.py",
                "old_text": "value = 1",
                "new_text": "value = 2",
            },
            Decision.ALLOW,
            "low_risk_workspace_action",
        ),
        ({"tool": "create_file", "path": "src/new.py", "content": "x"}, Decision.REQUIRE_APPROVAL, "file_lifecycle_change"),
        ({"tool": "delete_file", "path": "src/a.py"}, Decision.REQUIRE_APPROVAL, "file_lifecycle_change"),
        ({"tool": "delete_file", "path": "src"}, Decision.DENY, "delete_target_not_file"),
        (
            {"tool": "replace_in_file", "path": "pyproject.toml", "old_text": "content", "new_text": "new"},
            Decision.REQUIRE_APPROVAL,
            "protected_file_change",
        ),
        (
            {"tool": "replace_in_file", "path": "requirements.txt", "old_text": "content", "new_text": "new"},
            Decision.REQUIRE_APPROVAL,
            "protected_file_change",
        ),
        (
            {"tool": "replace_in_file", "path": "src/requirements.txt", "old_text": "content", "new_text": "new"},
            Decision.REQUIRE_APPROVAL,
            "protected_file_change",
        ),
        (
            {"tool": "replace_in_file", "path": "uv.lock", "old_text": "content", "new_text": "new"},
            Decision.REQUIRE_APPROVAL,
            "protected_file_change",
        ),
        (
            {"tool": "replace_in_file", "path": "Makefile", "old_text": "content", "new_text": "new"},
            Decision.REQUIRE_APPROVAL,
            "protected_file_change",
        ),
        (
            {"tool": "replace_in_file", "path": ".github/workflows/ci.yml", "old_text": "ci", "new_text": "safe"},
            Decision.REQUIRE_APPROVAL,
            "protected_file_change",
        ),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pytest", "-q"]}, Decision.ALLOW, "command_allowlist"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "ruff", "check", "src"]}, Decision.ALLOW, "command_allowlist"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "mypy", "src"]}, Decision.ALLOW, "command_allowlist"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "compileall", "src"]}, Decision.REQUIRE_APPROVAL, "command_writes_bytecode"),
        ({"tool": "run_command", "program": "git", "args": ["status", "--short"]}, Decision.ALLOW, "command_allowlist"),
        ({"tool": "run_command", "program": "git", "args": ["diff", "--", "src/a.py"]}, Decision.ALLOW, "command_allowlist"),
        ({"tool": "run_command", "program": "git", "args": ["diff", "--", "../outside.py"]}, Decision.DENY, "command_path_denied"),
        ({"tool": "run_command", "program": "git", "args": ["status", "--git-dir=.git"]}, Decision.DENY, "command_path_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pytest", ".env"]}, Decision.DENY, "command_path_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pytest", "--rootdir=../outside"]}, Decision.DENY, "command_path_denied"),
        ({"tool": "run_command", "program": "git", "args": ["add", "src/a.py"]}, Decision.REQUIRE_APPROVAL, "git_write_requires_approval"),
        ({"tool": "run_command", "program": "git", "args": ["add"]}, Decision.DENY, "git_add_target_required"),
        ({"tool": "run_command", "program": "git", "args": ["add", "missing.py"]}, Decision.DENY, "git_add_target_invalid"),
        ({"tool": "run_command", "program": "git", "args": ["add", "../outside.py"]}, Decision.DENY, "git_add_target_invalid"),
        ({"tool": "run_command", "program": "git", "args": ["add", "--pathspec-from-file=.env", "src"]}, Decision.DENY, "git_add_target_invalid"),
        ({"tool": "run_command", "program": "git", "args": ["commit", "-m", "test"]}, Decision.REQUIRE_APPROVAL, "git_write_requires_approval"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pip", "install", "--no-index", "pkg"]}, Decision.REQUIRE_APPROVAL, "local_dependency_install"),
        ({"tool": "read_file", "path": ".env"}, Decision.DENY, "security_boundary_violation"),
        ({"tool": "read_file", "path": "../outside"}, Decision.DENY, "security_boundary_violation"),
        ({"tool": "run_command", "program": "powershell", "args": ["-Command", "dir"]}, Decision.DENY, "shell_wrapper_denied"),
        ({"tool": "run_command", "program": "unknown", "args": []}, Decision.DENY, "command_not_allowed"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pytest", "|", "more"]}, Decision.DENY, "shell_syntax_denied"),
        ({"tool": "run_command", "program": "git", "args": ["push"]}, Decision.DENY, "remote_git_denied"),
        ({"tool": "run_command", "program": "git", "args": ["fetch"]}, Decision.DENY, "remote_git_denied"),
        ({"tool": "run_command", "program": "git", "args": ["pull"]}, Decision.DENY, "remote_git_denied"),
        ({"tool": "run_command", "program": "git", "args": ["clone", "https://example.invalid/x"]}, Decision.DENY, "remote_git_denied"),
        ({"tool": "run_command", "program": "curl", "args": ["https://example.invalid"]}, Decision.DENY, "network_tool_denied"),
        ({"tool": "run_command", "program": "wget", "args": ["https://example.invalid"]}, Decision.DENY, "network_tool_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pip", "install", "pkg"]}, Decision.DENY, "network_install_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pip", "install", "--no-index", "https://example.invalid/pkg.whl"]}, Decision.DENY, "network_install_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pip", "install", "--no-index", "git+https://example.invalid/pkg"]}, Decision.DENY, "network_install_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pip", "install", "--no-index", "--index-url", "https://example.invalid", "pkg"]}, Decision.DENY, "network_install_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pip", "install", "--no-index", "missing"]}, Decision.DENY, "local_install_target_invalid"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pip", "install", "--no-index", "../outside"]}, Decision.DENY, "local_install_target_invalid"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pip", "install", "--no-index", "--target=../outside", "pkg"]}, Decision.DENY, "local_install_target_invalid"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pip", "install", "--no-index", "-fhttps://example.invalid/simple", "pkg"]}, Decision.DENY, "network_install_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pip", "install", "--no-index", "-c../outside.txt", "pkg"]}, Decision.DENY, "local_install_target_invalid"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pip", "install", "--no-index", "-r../outside.txt"]}, Decision.DENY, "local_install_target_invalid"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pip", "install", "--no-index", "--unknown-option", "pkg"]}, Decision.DENY, "pip_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "ruff", "check", "--fix", "src"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "ruff", "check", "--unsafe-fixes=true", "src"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "mypy", "--install-types", "src"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "git", "args": ["diff", "--output", "diff.txt"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "git", "args": ["diff", "--ext-diff"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "git", "args": ["diff", "--textconv"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pytest", "--pastebin=all"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pytest", "--basetemp=src"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pytest", "--basetemp", "src"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pytest", "--cache-clear"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pytest", "--junitxml=report.xml"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pytest", "--junitxml", "report.xml"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "ruff", "check", "--output-file=report.txt", "src"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "ruff", "check", "-o", "report.txt", "src"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "mypy", "--junit-xml=report.xml", "src"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pytest", "--debug=src/a.py"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pytest", "--log-file", "src/a.py"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "ruff", "check", "--add-noqa", "src"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "ruff", "check", "--add-ignore", "src"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "mypy", "--html-report", "reports", "src"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "pytest", "@opts.txt"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "ruff", "check", "@opts.txt"]}, Decision.DENY, "command_side_effect_option_denied"),
        ({"tool": "run_command", "program": "python", "args": ["-m", "mypy", "@opts.txt"]}, Decision.DENY, "command_side_effect_option_denied"),
    ],
)
def test_builtin_risk_matrix(
    workspace: Path, payload: dict[str, object], expected: Decision, reason_code: str
) -> None:
    _prepare_workspace(workspace)
    decision = PolicyEngine().evaluate(
        parse_action(payload), PolicyContext.for_workspace(workspace)
    )
    assert decision.decision is expected
    assert decision.reason_code == reason_code
    assert str(workspace) not in decision.reason_code


@pytest.mark.parametrize("argument", ["-m pytest", ";", ">", "$(whoami)", "`whoami`", "a\nb"])
def test_command_arguments_are_not_shell_strings(workspace: Path, argument: str) -> None:
    decision = PolicyEngine().evaluate(
        parse_action({"tool": "run_command", "program": "python", "args": [argument]}),
        PolicyContext.for_workspace(workspace),
    )
    assert decision.decision is Decision.DENY


def test_project_command_prefixes_can_only_narrow_builtin_set(workspace: Path) -> None:
    context = PolicyContext.for_workspace(
        workspace, command_prefixes=frozenset({("python", "-m", "pytest")})
    )
    allowed = PolicyEngine().evaluate(
        parse_action({"tool": "run_command", "program": "python", "args": ["-m", "pytest", "-q"]}),
        context,
    )
    restricted = PolicyEngine().evaluate(
        parse_action({"tool": "run_command", "program": "git", "args": ["status"]}),
        context,
    )
    assert allowed.decision is Decision.ALLOW
    assert restricted.decision is Decision.DENY
    assert restricted.reason_code == "project_command_restricted"
    git_add = PolicyEngine().evaluate(
        parse_action({"tool": "run_command", "program": "git", "args": ["add", "src/a.py"]}),
        context,
    )
    local_pip = PolicyEngine().evaluate(
        parse_action(
            {
                "tool": "run_command",
                "program": "python",
                "args": ["-m", "pip", "install", "--no-index", "pkg"],
            }
        ),
        context,
    )
    assert git_add.decision is Decision.DENY
    assert local_pip.decision is Decision.DENY
    assert git_add.reason_code == local_pip.reason_code == "project_command_restricted"
    with pytest.raises(ValueError, match="command_prefix_not_builtin"):
        PolicyContext.for_workspace(
            workspace, command_prefixes=frozenset({("python", "-c")})
        )
    with pytest.raises(ValueError, match="command_prefix_not_builtin"):
        PolicyContext(
            workspace=workspace,
            budgets=BudgetConfig(),
            command_prefixes=frozenset({("python", "-c")}),
        )


def test_command_timeout_cannot_exceed_session_budget(workspace: Path) -> None:
    decision = PolicyEngine().evaluate(
        parse_action(
            {
                "tool": "run_command",
                "program": "python",
                "args": ["-m", "pytest", "-q"],
                "timeout_seconds": 31,
            }
        ),
        PolicyContext.for_workspace(
            workspace, budgets=BudgetConfig(command_timeout_seconds=30)
        ),
    )

    assert decision.decision is Decision.DENY
    assert decision.reason_code == "command_timeout_exceeds_budget"


def test_governance_models_are_strict_frozen_and_validate_resolution_shape() -> None:
    action = parse_action({"tool": "run_command", "program": "git", "args": ["status"]})
    pending = PendingAction(action_id="a", session_id="s", action=action, fingerprint="fp")
    grant = AuthorizationGrant(
        action_id="a",
        session_id="s",
        action=action,
        fingerprint="fp",
        policy_decision_id="d",
    )
    with pytest.raises(ValidationError):
        PolicyDecision.model_validate(
            {"decision": "ALLOW", "reason_code": "ok", "rule_source": "builtin", "fingerprint": "fp"}
        )
    with pytest.raises(ValidationError):
        pending.action_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        PolicyResolution(
            action_id="a",
            action=action,
            fingerprint="fp",
            decision=Decision.ALLOW,
            reason_code="ok",
            pending_action=pending,
        )
    with pytest.raises(ValidationError):
        PolicyResolution(
            action_id="a",
            action=action,
            fingerprint="fp",
            decision=Decision.REQUIRE_APPROVAL,
            reason_code="approval",
            grant=grant,
        )
    with pytest.raises(ValidationError):
        PolicyResolution(
            action_id="a",
            action=action,
            fingerprint="fp",
            decision=Decision.ALLOW,
            reason_code="ok",
            grant=grant.model_copy(update={"approval_id": "unexpected-approval"}),
        )
    with pytest.raises(ValidationError):
        PolicyResolution(
            action_id="a",
            action=action,
            fingerprint="fp",
            decision=Decision.DENY,
            reason_code="deny",
            grant=grant,
        )
    decoded = TypeAdapter(list[PendingAction]).validate_json(
        '[{"action_id":"a","session_id":"s","action":{"tool":"run_command",'
        '"program":"git","args":["status"]},"fingerprint":"fp"}]'
    )
    assert decoded[0].action == action


class RecordingWriter:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[dict[str, object]] = []
        self.fail = fail

    def append(self, event: dict[str, object]) -> None:
        self.events.append(event)
        if self.fail:
            raise OSError("private writer detail")


class FakeApprovalService:
    def __init__(self, approval_id: str = "a" * 64, fail: bool = False) -> None:
        self.approval_id = approval_id
        self.fail = fail
        self.requests: list[PendingAction] = []

    def request_in_transaction(
        self,
        pending: PendingAction,
        workspace: Path,
        *,
        expires_in: timedelta,
    ) -> str:
        del workspace, expires_in
        self.requests.append(pending)
        if self.fail:
            raise RuntimeError("private approval detail")
        return self.approval_id


def _session(store: StateStore, workspace: Path) -> str:
    return store.create_session(store.upsert_project(workspace, "Demo"), "task")


def _count(store: StateStore, table: str) -> int:
    return int(store._execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_gateway_persists_and_flushes_before_returning_allow_grant(
    store: StateStore, workspace: Path
) -> None:
    _prepare_workspace(workspace)
    session_id = _session(store, workspace)
    writer = RecordingWriter()
    resolution = PolicyGateway(PolicyEngine(), store, writer).authorize(
        session_id,
        1,
        parse_action({"tool": "read_file", "path": "src/a.py"}),
        workspace,
    )
    assert resolution.decision is Decision.ALLOW
    assert resolution.grant is not None
    assert resolution.grant.action_id == resolution.action_id
    assert store.get_policy_decision(resolution.grant.policy_decision_id).decision is Decision.ALLOW
    assert [event["event"] for event in writer.events] == ["policy_decision"]
    assert store.list_pending_audit() == ()


def test_gateway_creates_real_pending_resolution_inside_business_transaction(
    store: StateStore, workspace: Path
) -> None:
    _prepare_workspace(workspace)
    session_id = _session(store, workspace)
    writer = RecordingWriter()
    approvals = FakeApprovalService()
    resolution = PolicyGateway(PolicyEngine(), store, writer, approvals).authorize(
        session_id,
        1,
        parse_action({"tool": "create_file", "path": "src/new.py", "content": "x"}),
        workspace,
    )
    assert resolution.decision is Decision.REQUIRE_APPROVAL
    assert resolution.grant is None
    assert resolution.pending_action == approvals.requests[0]
    assert resolution.approval_id == approvals.approval_id


def test_gateway_integrates_with_persistent_approval_service(
    store: StateStore, workspace: Path
) -> None:
    from coding_agent_harness.approvals import ApprovalService
    from coding_agent_harness.models import ApprovalStatus

    _prepare_workspace(workspace)
    session_id = _session(store, workspace)
    writer = RecordingWriter()
    approvals = ApprovalService(store, writer, token_source=lambda: "de" * 32)
    resolution = PolicyGateway(PolicyEngine(), store, writer, approvals).authorize(
        session_id,
        1,
        parse_action({"tool": "create_file", "path": "src/new.py", "content": "x"}),
        workspace,
    )
    assert resolution.approval_id == "de" * 32
    assert store.get_approval(resolution.approval_id).status is ApprovalStatus.PENDING
    assert store.list_pending_audit() == ()


def test_gateway_safely_persists_denied_path_without_creating_approval(
    store: StateStore, workspace: Path
) -> None:
    session_id = _session(store, workspace)
    writer = RecordingWriter()
    approvals = FakeApprovalService()
    resolution = PolicyGateway(PolicyEngine(), store, writer, approvals).authorize(
        session_id,
        1,
        parse_action({"tool": "read_file", "path": ".env"}),
        workspace,
    )
    assert resolution.decision is Decision.DENY
    assert resolution.grant is None and resolution.pending_action is None
    assert approvals.requests == []
    stored = store.get_action(resolution.action_id)
    assert ".env" not in stored.action.model_dump_json()
    assert stored.action.path == "<REJECTED_PATH>"  # type: ignore[union-attr]


def test_gateway_rolls_back_all_records_before_any_writer_side_effect(
    store: StateStore, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_workspace(workspace)
    session_id = _session(store, workspace)
    writer = RecordingWriter()

    def fail(*_args: object, **_kwargs: object) -> str:
        raise StorageError("private sql detail")

    monkeypatch.setattr(store, "record_policy_decision", fail)
    with pytest.raises(PolicyGatewayError) as caught:
        PolicyGateway(PolicyEngine(), store, writer).authorize(
            session_id,
            1,
            parse_action({"tool": "read_file", "path": "src/a.py"}),
            workspace,
        )
    assert str(caught.value) == "policy_gateway_failed"
    assert _count(store, "actions") == 0
    assert _count(store, "policy_decisions") == 0
    assert _count(store, "audit_outbox") == 0
    assert writer.events == []


def test_gateway_rolls_back_when_approval_request_fails(
    store: StateStore, workspace: Path
) -> None:
    _prepare_workspace(workspace)
    session_id = _session(store, workspace)
    writer = RecordingWriter()
    with pytest.raises(PolicyGatewayError, match="policy_gateway_failed"):
        PolicyGateway(PolicyEngine(), store, writer, FakeApprovalService(fail=True)).authorize(
            session_id,
            1,
            parse_action({"tool": "create_file", "path": "src/new.py", "content": "x"}),
            workspace,
        )
    assert [_count(store, table) for table in ("actions", "policy_decisions", "audit_outbox")] == [0, 0, 0]
    assert writer.events == []


def test_gateway_flushes_old_outbox_before_creating_new_records(
    store: StateStore, workspace: Path
) -> None:
    _prepare_workspace(workspace)
    session_id = _session(store, workspace)
    store.enqueue_audit({"event": "older"})
    writer = RecordingWriter(fail=True)
    with pytest.raises(PolicyGatewayError, match="policy_gateway_failed"):
        PolicyGateway(PolicyEngine(), store, writer).authorize(
            session_id,
            1,
            parse_action({"tool": "read_file", "path": "src/a.py"}),
            workspace,
        )
    assert _count(store, "actions") == 0
    assert [event["event"] for event in writer.events] == ["older"]


def test_gateway_does_not_return_grant_when_new_audit_flush_fails(
    store: StateStore, workspace: Path
) -> None:
    _prepare_workspace(workspace)
    session_id = _session(store, workspace)
    writer = RecordingWriter(fail=True)
    result: list[Any] = []
    with pytest.raises(PolicyGatewayError, match="policy_gateway_failed"):
        result.append(
            PolicyGateway(PolicyEngine(), store, writer).authorize(
                session_id,
                1,
                parse_action({"tool": "read_file", "path": "src/a.py"}),
                workspace,
            )
        )
    assert result == []
    assert _count(store, "actions") == 1
    assert _count(store, "policy_decisions") == 1
    assert len(store.list_pending_audit()) == 1
