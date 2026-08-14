from pathlib import Path

import pytest

from coding_agent_harness import file_tools as file_tools_module
from coding_agent_harness.application import create_control_application
from coding_agent_harness.credentials import MemoryCredentialBackend
from coding_agent_harness.llm import ScriptedMockLLM
from coding_agent_harness.models import ApprovalStatus, SessionStatus


def seed_failing_repository(root: Path) -> None:
    (root / "calc.py").write_text("def total():\n    return 1\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text(
        "from calc import total\n\ndef test_total():\n    assert total() == 2\n",
        encoding="utf-8",
    )


def test_scripted_mock_repairs_failing_python_repository(app_factory, tmp_path: Path) -> None:
    seed_failing_repository(tmp_path)
    llm = ScriptedMockLLM(
        [
            {"tool": "read_file", "path": "calc.py", "start_line": 1, "end_line": 20},
            {"tool": "replace_in_file", "path": "calc.py", "old_text": "return 1", "new_text": "return 3"},
            {"tool": "replace_in_file", "path": "calc.py", "old_text": "return 3", "new_text": "return 2"},
            {"tool": "finish", "summary": "fixed total"},
        ]
    )
    app = app_factory(workspace=tmp_path, llm=llm)
    result = app.run(task="fix failing tests")
    assert result.status is SessionStatus.SUCCEEDED
    assert "test_failure" in llm.contexts[2].model_dump_json()
    assert "return 2" in (tmp_path / "calc.py").read_text(encoding="utf-8")


def test_same_size_edits_do_not_reuse_stale_bytecode(
    app_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen_ns = 1_700_000_000_000_000_000
    original_write_text = Path.write_text
    original_replace = file_tools_module.os.replace

    def write_text_with_frozen_mtime(self: Path, data: str, **kwargs: object) -> int:
        written = original_write_text(self, data, **kwargs)
        if self.name == "calc.py":
            file_tools_module.os.utime(self, ns=(frozen_ns, frozen_ns))
        return written

    def replace_with_frozen_mtime(source: str, destination: str) -> None:
        original_replace(source, destination)
        target = Path(destination)
        if target.name == "calc.py":
            file_tools_module.os.utime(target, ns=(frozen_ns, frozen_ns))

    monkeypatch.setattr(Path, "write_text", write_text_with_frozen_mtime)
    monkeypatch.setattr(file_tools_module.os, "replace", replace_with_frozen_mtime)
    seed_failing_repository(tmp_path)
    llm = ScriptedMockLLM(
        [
            {"tool": "read_file", "path": "calc.py", "start_line": 1, "end_line": 20},
            {"tool": "replace_in_file", "path": "calc.py", "old_text": "return 1", "new_text": "return 3"},
            {"tool": "replace_in_file", "path": "calc.py", "old_text": "return 3", "new_text": "return 2"},
            {"tool": "finish", "summary": "fixed total"},
        ]
    )

    result = app_factory(workspace=tmp_path, llm=llm).run(task="fix failing tests")

    assert result.status is SessionStatus.SUCCEEDED
    assert "test_failure" in llm.contexts[2].model_dump_json()
    assert "return 2" in (tmp_path / "calc.py").read_text(encoding="utf-8")


def test_approved_action_survives_pause_and_executes_once_on_resume(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    clients = iter(
        (
            ScriptedMockLLM(
                [{"tool": "create_file", "path": "approved.py", "content": "ok = True\n"}]
            ),
            ScriptedMockLLM([{"tool": "finish", "summary": "approved change complete"}]),
        )
    )
    app = create_control_application(
        tmp_path / "app-data",
        credential_backend=MemoryCredentialBackend(),
        llm_factory=lambda: next(clients),
    )

    try:
        paused = app.run(workspace=workspace, task="create approved file")
        approval = app.store.list_pending_approvals(paused.session_id)[0]
        app.approvals.approve(approval.id)

        result = app.sessions.resume_and_run(paused.session_id)

        assert result.status is SessionStatus.SUCCEEDED
        assert (workspace / "approved.py").read_text(encoding="utf-8") == "ok = True\n"
        assert app.store.get_approval(approval.id).status is ApprovalStatus.CONSUMED
        assert len(app.store.list_changes(paused.session_id)) == 1
    finally:
        app.store.close()


def test_denied_action_becomes_feedback_without_execution_on_resume(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    clients = iter(
        (
            ScriptedMockLLM(
                [{"tool": "create_file", "path": "denied.py", "content": "bad = True\n"}]
            ),
            ScriptedMockLLM([{"tool": "finish", "summary": "respect denial"}]),
        )
    )
    app = create_control_application(
        tmp_path / "app-data",
        credential_backend=MemoryCredentialBackend(),
        llm_factory=lambda: next(clients),
    )

    try:
        paused = app.run(workspace=workspace, task="request denied file")
        approval = app.store.list_pending_approvals(paused.session_id)[0]
        app.approvals.deny(approval.id)

        result = app.sessions.resume_and_run(paused.session_id)

        assert result.status is SessionStatus.SUCCEEDED
        assert not (workspace / "denied.py").exists()
        assert app.store.get_approval(approval.id).status is ApprovalStatus.DENIED
        assert app.store.list_changes(paused.session_id) == ()
        observation = app.store.latest_observation(paused.session_id)
        assert observation is not None
        assert observation.summary == "approval_denied"
    finally:
        app.store.close()
