from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from coding_agent_harness.cli import app, exit_code_for_status
from coding_agent_harness.config import BudgetConfig
from coding_agent_harness.engine import SessionResult
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
    script = workspace / "script.json"
    script.write_text("[]", encoding="utf-8")
    result = CliRunner().invoke(
        cli_app,
        [
            "run",
            "--workspace",
            str(workspace),
            "--task",
            "fix tests",
            "--mock-script",
            str(script),
        ],
    )

    assert result.exit_code in {20, 30, 40}
    assert "session" in result.stdout.lower()


def test_run_forwards_only_explicit_cli_budget_limits(workspace: Path) -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def run(self, **kwargs: object) -> SessionResult:
            self.kwargs = kwargs
            return SessionResult(
                session_id="session-1",
                status=SessionStatus.PAUSED_LIMIT_REACHED,
                stop_reason="test",
            )

    service = RecordingService()
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--workspace",
            str(workspace),
            "--task",
            "fix tests",
            "--max-steps",
            "5",
            "--max-llm-calls",
            "4",
            "--command-timeout-seconds",
            "30",
        ],
        obj=service,
    )

    assert result.exit_code == 20
    budget = service.kwargs["cli_budget"]
    assert isinstance(budget, BudgetConfig)
    assert budget.model_fields_set == {
        "max_steps",
        "max_llm_calls",
        "command_timeout_seconds",
    }
    assert budget.max_steps == 5
    assert budget.max_llm_calls == 4
    assert budget.command_timeout_seconds == 30


def test_run_without_configured_credential_fails_cleanly(
    cli_app, workspace: Path
) -> None:
    result = CliRunner().invoke(
        cli_app,
        ["run", "--workspace", str(workspace), "--task", "fix tests"],
    )

    assert result.exit_code == 40
    assert isinstance(result.exception, SystemExit)
    assert "credential_not_configured" in result.output
