from pathlib import Path

import pytest

from coding_agent_harness.dispatcher import Dispatcher, DispatchError
from coding_agent_harness.models import Decision, ListFilesAction, ReadFileAction
from coding_agent_harness.policy import AuthorizationGrant
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
    def __init__(self, decision: Decision) -> None:
        self.decision = decision


class FakeStore:
    def __init__(self, decision: Decision = Decision.ALLOW, consumed: bool = True) -> None:
        self.decision = decision
        self.consumed = consumed

    def get_policy_decision(self, _decision_id: str):
        return FakeDecision(self.decision)

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


def test_dispatcher_routes_file_and_command_actions(tmp_path: Path) -> None:
    files, commands = CountingFileTools(), CountingCommandRunner()
    service = Dispatcher(FakeStore(), files, commands, None, tmp_path)
    observation = service.execute(_grant(ListFilesAction()))
    assert observation.category == "success"
    assert files.call_count == 1
