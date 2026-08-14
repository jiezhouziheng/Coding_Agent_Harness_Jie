from pathlib import Path

import pytest

from coding_agent_harness.approvals import ApprovalService
from coding_agent_harness.dispatcher import Dispatcher, DispatchError
from coding_agent_harness.file_tools import FileTools
from coding_agent_harness.journal import ChangeJournal
from coding_agent_harness.models import CreateFileAction, Decision, ListFilesAction, ReadFileAction
from coding_agent_harness.policy import AuthorizationGrant, PolicyEngine, PolicyGateway
from coding_agent_harness.security import action_fingerprint


class CountingFileTools:
    def __init__(self) -> None:
        self.call_count = 0

    def list_files(self, *args):
        self.call_count += 1
        return "listed"


class CountingCommandRunner:
    def __init__(self) -> None:
        self.call_count = 0

    def run(self, *args, **kwargs):
        self.call_count += 1
        return "ran"


class FakeDecision:
    def __init__(self, decision: Decision, action_id: str = "a1") -> None:
        self.decision = decision
        self.action_id = action_id


class FakeStoredAction:
    def __init__(
        self,
        action=None,
        *,
        session_id: str = "s1",
        fingerprint: str | None = None,
    ) -> None:
        self.action = action or ListFilesAction()
        self.session_id = session_id
        self.fingerprint = fingerprint or action_fingerprint(self.action)


class FakeStore:
    def __init__(
        self,
        decision: Decision = Decision.ALLOW,
        consumed: bool = True,
        *,
        decision_action_id: str = "a1",
        stored_action: FakeStoredAction | None = None,
    ) -> None:
        self.decision = decision
        self.consumed = consumed
        self.decision_action_id = decision_action_id
        self.stored_action = stored_action or FakeStoredAction()

    def get_policy_decision(self, _decision_id: str):
        return FakeDecision(self.decision, self.decision_action_id)

    def get_action(self, _action_id: str):
        return self.stored_action

    def is_consumed_approval(self, *_args, **_kwargs):
        return self.consumed


def _grant(action, *, decision_id="d1", approval_id=None, fingerprint=None):
    return AuthorizationGrant(
        action_id="a1", session_id="s1", action=action,
        fingerprint=fingerprint or action_fingerprint(action),
        policy_decision_id=decision_id, approval_id=approval_id,
    )


@pytest.fixture
def dispatcher(tmp_path: Path):
    files = CountingFileTools()
    commands = CountingCommandRunner()
    return Dispatcher(FakeStore(), files, commands, None, tmp_path), files, commands


def test_dispatcher_rejects_raw_action_without_touching_tools(dispatcher) -> None:
    service, files, commands = dispatcher
    with pytest.raises(DispatchError, match="authorization_grant_required"):
        service.execute(ReadFileAction(path="app.py"))
    assert files.call_count == commands.call_count == 0


@pytest.mark.parametrize("bad_grant", [
    _grant(ListFilesAction(), fingerprint="tampered"),
])
def test_dispatcher_rejects_tampered_grant_without_calls(dispatcher, bad_grant) -> None:
    service, files, commands = dispatcher
    with pytest.raises(DispatchError, match="grant_fingerprint_mismatch"):
        service.execute(bad_grant)
    assert files.call_count == commands.call_count == 0


def test_dispatcher_rejects_denied_grant_without_calls(tmp_path: Path) -> None:
    files, commands = CountingFileTools(), CountingCommandRunner()
    service = Dispatcher(FakeStore(Decision.DENY), files, commands, None, tmp_path)
    with pytest.raises(DispatchError, match="denied_action"):
        service.execute(_grant(ListFilesAction()))
    assert files.call_count == commands.call_count == 0


def test_dispatcher_rejects_unconsumed_approval_without_calls(tmp_path: Path) -> None:
    files, commands = CountingFileTools(), CountingCommandRunner()
    service = Dispatcher(FakeStore(Decision.REQUIRE_APPROVAL, consumed=False), files, commands, None, tmp_path)
    with pytest.raises(DispatchError, match="consumed_approval_required"):
        service.execute(_grant(ListFilesAction(), approval_id="ap1"))
    assert files.call_count == commands.call_count == 0


def test_dispatcher_rejects_policy_decision_for_another_action_without_calls(
    tmp_path: Path,
) -> None:
    files, commands = CountingFileTools(), CountingCommandRunner()
    service = Dispatcher(
        FakeStore(decision_action_id="another-action"),
        files,
        commands,
        None,
        tmp_path,
    )

    with pytest.raises(DispatchError, match="grant_decision_mismatch"):
        service.execute(_grant(ListFilesAction()))

    assert files.call_count == commands.call_count == 0


def test_dispatcher_rejects_stored_action_for_another_session_without_calls(
    tmp_path: Path,
) -> None:
    files, commands = CountingFileTools(), CountingCommandRunner()
    service = Dispatcher(
        FakeStore(stored_action=FakeStoredAction(session_id="another-session")),
        files,
        commands,
        None,
        tmp_path,
    )

    with pytest.raises(DispatchError, match="grant_action_mismatch"):
        service.execute(_grant(ListFilesAction()))

    assert files.call_count == commands.call_count == 0


def test_dispatcher_routes_file_and_command_actions(tmp_path: Path) -> None:
    files, commands = CountingFileTools(), CountingCommandRunner()
    service = Dispatcher(FakeStore(), files, commands, None, tmp_path)
    observation = service.execute(_grant(ListFilesAction()))
    assert observation.category == "success"
    assert files.call_count == 1


def test_dispatcher_rejects_consumed_approval_from_another_session(
    store, audit_writer, tmp_path: Path
) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    first_session = store.create_session(
        store.upsert_project(first_workspace, "first"), "first task"
    )
    second_session = store.create_session(
        store.upsert_project(second_workspace, "second"), "second task"
    )
    tokens = iter(("11" * 32, "22" * 32))
    approvals = ApprovalService(store, audit_writer, token_source=lambda: next(tokens))
    gateway = PolicyGateway(PolicyEngine(), store, audit_writer, approvals)
    action = CreateFileAction(path="approved.py", content="approved = True\n")
    first = gateway.authorize(first_session, 1, action, first_workspace)
    second = gateway.authorize(second_session, 1, action, second_workspace)
    assert first.pending_action is not None and first.approval_id is not None
    assert second.pending_action is not None
    approvals.approve(first.approval_id)
    first_grant = approvals.consume(
        first.approval_id, first.pending_action, first_workspace
    )
    forged_grant = first_grant.model_copy(
        update={
            "action_id": second.action_id,
            "session_id": second_session,
            "policy_decision_id": store.get_policy_decision_for_action(
                second.action_id
            ).id,
        }
    )
    dispatcher = Dispatcher(
        store,
        FileTools(second_workspace, ChangeJournal(store, tmp_path / "backups")),
        CountingCommandRunner(),
        None,
        second_workspace,
    )

    with pytest.raises(DispatchError, match="consumed_approval_required"):
        dispatcher.execute(forged_grant)

    assert not (second_workspace / "approved.py").exists()
    assert store.list_changes(second_session) == ()
