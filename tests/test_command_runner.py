from pathlib import Path

import pytest

from coding_agent_harness.command_runner import CommandRunner
from coding_agent_harness.models import RunCommandAction


def test_runner_scrubs_credentials_and_uses_argument_vector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    action = RunCommandAction(
        program="python",
        args=("-c", "import os; print(os.getenv('OPENAI_API_KEY', 'missing'))"),
    )
    result = CommandRunner(max_output_bytes=1_000).run(action, workspace=tmp_path)
    assert result.exit_code == 0
    assert result.stdout.strip() == "missing"
    assert "sk-secret" not in result.stdout


def test_runner_truncates_combined_bytes_and_redacts_output(tmp_path: Path) -> None:
    action = RunCommandAction(program="python", args=("-c", "print('x' * 5000)"))
    result = CommandRunner(max_output_bytes=100).run(action, workspace=tmp_path)
    assert result.truncated is True
    assert len((result.stdout + result.stderr).encode()) <= 130
    assert "<TRUNCATED>" in result.stdout


def test_runner_rejects_cwd_outside_workspace(tmp_path: Path) -> None:
    action = RunCommandAction(program="python", cwd="..")
    with pytest.raises(ValueError, match="workspace"):
        CommandRunner().run(action, workspace=tmp_path)


def test_runner_times_out_and_kills_process_tree(tmp_path: Path) -> None:
    action = RunCommandAction(program="python", args=("-c", "import time; time.sleep(5)"), timeout_seconds=1)
    result = CommandRunner().run(action, workspace=tmp_path)
    assert result.timed_out is True
    assert result.exit_code is None


@pytest.mark.parametrize(
    "action",
    [
        RunCommandAction(program="unknown-program"),
        RunCommandAction(program="sh", args=("-c", "echo denied")),
        RunCommandAction(program="python", args=("-m", "pip", "install", "pkg")),
    ],
)
def test_runner_rejects_unknown_shell_or_network_commands(tmp_path: Path, action: RunCommandAction) -> None:
    with pytest.raises(ValueError):
        CommandRunner().run(action, workspace=tmp_path)
