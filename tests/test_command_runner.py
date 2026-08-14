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


def test_runner_uses_unique_non_inherited_pycache_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inherited_prefix = "inherited-pycache-prefix"
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", inherited_prefix)
    action = RunCommandAction(
        program="python",
        args=("-c", "import os; print(os.environ['PYTHONPYCACHEPREFIX'])"),
    )

    prefixes = [
        Path(CommandRunner().run(action, workspace=tmp_path).stdout.strip())
        for _ in range(2)
    ]

    assert all(str(prefix) != inherited_prefix for prefix in prefixes)
    assert prefixes[0] != prefixes[1]
    assert all(not prefix.exists() for prefix in prefixes)


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
    script = (
        "import os, pathlib, time; "
        "pathlib.Path('pycache-prefix.txt').write_text(os.environ['PYTHONPYCACHEPREFIX']); "
        "time.sleep(5)"
    )
    action = RunCommandAction(program="python", args=("-c", script), timeout_seconds=1)
    result = CommandRunner().run(action, workspace=tmp_path)
    assert result.timed_out is True
    assert result.exit_code is None
    prefix = Path((tmp_path / "pycache-prefix.txt").read_text(encoding="utf-8"))
    assert not prefix.exists()


def test_runner_cleans_pycache_prefix_when_process_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_prefix: Path | None = None

    def fail_to_start(*args: object, **kwargs: object) -> None:
        nonlocal captured_prefix
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        prefix_value = environment.get("PYTHONPYCACHEPREFIX")
        captured_prefix = Path(prefix_value) if isinstance(prefix_value, str) else None
        raise OSError("process start failed")

    monkeypatch.setattr("coding_agent_harness.command_runner.subprocess.Popen", fail_to_start)

    with pytest.raises(OSError, match="process start failed"):
        CommandRunner().run(RunCommandAction(program="python"), workspace=tmp_path)

    assert captured_prefix is not None
    assert not captured_prefix.exists()


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
