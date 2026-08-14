from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from coding_agent_harness.cli import app, exit_code_for_status
from coding_agent_harness.models import SessionStatus


def test_exit_codes_are_stable() -> None:
    assert exit_code_for_status(SessionStatus.SUCCEEDED) == 0
    assert exit_code_for_status(SessionStatus.PAUSED_APPROVAL) == 20
    assert exit_code_for_status(SessionStatus.NEEDS_USER_DECISION) == 30
    assert exit_code_for_status(SessionStatus.CREATED) == 40


def test_help_exposes_control_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "sessions" in result.stdout
    assert "credentials" in result.stdout
    assert "approvals" in result.stdout


def test_credentials_status_never_echoes_secret(cli_app, credential_backend) -> None:
    credential_backend.set("coding-agent-harness", "default", "sk-secret")

    result = CliRunner().invoke(cli_app, ["credentials", "status", "--profile", "default"])

    assert result.exit_code == 0
    assert "configured" in result.stdout.lower()
    assert "sk-secret" not in result.stdout


def test_run_paused_prints_resume_commands(cli_app, workspace: Path) -> None:
    result = CliRunner().invoke(
        cli_app,
        ["run", "--workspace", str(workspace), "--task", "fix tests"],
    )

    assert result.exit_code in {20, 30, 40}
    assert "session" in result.stdout.lower()

