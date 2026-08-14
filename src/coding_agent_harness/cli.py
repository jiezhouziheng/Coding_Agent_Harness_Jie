from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from coding_agent_harness.config import BudgetConfig, ConfigError
from coding_agent_harness.models import SessionStatus

app = typer.Typer(no_args_is_help=True)

sessions_app = typer.Typer(no_args_is_help=True)
approvals_app = typer.Typer(no_args_is_help=True)
changes_app = typer.Typer(no_args_is_help=True)
credentials_app = typer.Typer(no_args_is_help=True)
memory_app = typer.Typer(no_args_is_help=True)
report_app = typer.Typer(no_args_is_help=True)
demo_app = typer.Typer(no_args_is_help=True)


def exit_code_for_status(status: SessionStatus) -> int:
    if status is SessionStatus.SUCCEEDED:
        return 0
    if status in {
        SessionStatus.PAUSED_APPROVAL,
        SessionStatus.PAUSED_LIMIT_REACHED,
        SessionStatus.PAUSED_PROTOCOL_ERROR,
        SessionStatus.PAUSED_WORKSPACE_DRIFT,
        SessionStatus.PAUSED_INTERNAL_ERROR,
    }:
        return 20
    if status is SessionStatus.NEEDS_USER_DECISION:
        return 30
    return 40


def _services(ctx: typer.Context) -> Any:
    if ctx.obj is None:
        from coding_agent_harness.application import create_control_application
        from coding_agent_harness.session_service import default_app_data_dir

        ctx.obj = create_control_application(default_app_data_dir())
    return ctx.obj


def _show(value: object) -> None:
    if hasattr(value, "model_dump_json"):
        typer.echo(value.model_dump_json(indent=2))
    else:
        import json

        typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@app.command()
def run(
    ctx: typer.Context,
    workspace: Annotated[Path, typer.Option("--workspace")],
    task: Annotated[str, typer.Option("--task")],
    mock_script: Annotated[Path | None, typer.Option("--mock-script")] = None,
    max_steps: Annotated[int | None, typer.Option("--max-steps")] = None,
    max_llm_calls: Annotated[int | None, typer.Option("--max-llm-calls")] = None,
    max_consecutive_failures: Annotated[
        int | None, typer.Option("--max-consecutive-failures")
    ] = None,
    max_repeated_action: Annotated[
        int | None, typer.Option("--max-repeated-action")
    ] = None,
    command_timeout_seconds: Annotated[
        int | None, typer.Option("--command-timeout-seconds")
    ] = None,
    session_timeout_minutes: Annotated[
        int | None, typer.Option("--session-timeout-minutes")
    ] = None,
    max_observation_bytes: Annotated[
        int | None, typer.Option("--max-observation-bytes")
    ] = None,
) -> None:
    """Run the coding agent harness."""
    limits = {
        name: value
        for name, value in {
            "max_steps": max_steps,
            "max_llm_calls": max_llm_calls,
            "max_consecutive_failures": max_consecutive_failures,
            "max_repeated_action": max_repeated_action,
            "command_timeout_seconds": command_timeout_seconds,
            "session_timeout_minutes": session_timeout_minutes,
            "max_observation_bytes": max_observation_bytes,
        }.items()
        if value is not None
    }
    try:
        cli_budget = BudgetConfig(**limits) if limits else None
        service = _services(ctx)
        if hasattr(service, "run"):
            result = service.run(
                workspace=workspace,
                task=task,
                mock_script=mock_script,
                cli_budget=cli_budget,
            )
            _show(result)
            status = getattr(result, "status", SessionStatus.PAUSED_INTERNAL_ERROR)
            raise typer.Exit(code=exit_code_for_status(status))
    except typer.Exit:
        raise
    except ValidationError:
        typer.echo("invalid_budget_config", err=True)
        raise typer.Exit(code=40) from None
    except (ConfigError, ValueError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=40) from None
    typer.echo(f"session: pending ({workspace}) task={task}")
    raise typer.Exit(code=20)


@credentials_app.command("status")
def credentials_status(ctx: typer.Context, profile: str = "default") -> None:
    value = _services(ctx).credentials.status(profile)
    _show(value)
    typer.echo("configured" if value.exists else "not configured")


@credentials_app.command("set")
def credentials_set(ctx: typer.Context, profile: str = "default") -> None:
    _services(ctx).credentials.set(profile, typer.prompt("API Key", hide_input=True))
    typer.echo("configured")


@credentials_app.command("update")
def credentials_update(ctx: typer.Context, profile: str = "default") -> None:
    _services(ctx).credentials.update(profile, typer.prompt("New API Key", hide_input=True))
    typer.echo("updated")


@credentials_app.command("clear")
def credentials_clear(ctx: typer.Context, profile: str = "default") -> None:
    _services(ctx).credentials.clear(profile)
    typer.echo("cleared")


@sessions_app.command("list")
def sessions_list(ctx: typer.Context) -> None:
    _show(_services(ctx).sessions.list_safe())


@sessions_app.command("show")
def sessions_show(ctx: typer.Context, session_id: str) -> None:
    _show(_services(ctx).sessions.show_safe(session_id))


@sessions_app.command("resume")
def sessions_resume(ctx: typer.Context, session_id: str) -> None:
    service = _services(ctx)
    result = service.sessions.resume_and_run(session_id)
    _show(result)
    raise typer.Exit(code=exit_code_for_status(result.status))


@report_app.command("export")
def report_export(ctx: typer.Context, session_id: str, output: Path) -> None:
    typer.echo(str(_services(ctx).reports.export(session_id, output)))


@changes_app.command("show")
def changes_show(ctx: typer.Context, session_id: str) -> None:
    _show(tuple(_services(ctx).store.list_changes(session_id)))


@approvals_app.command("list")
def approvals_list(ctx: typer.Context, session_id: str | None = None) -> None:
    _show(_services(ctx).store.list_pending_approvals(session_id))


@approvals_app.command("approve")
def approvals_approve(ctx: typer.Context, approval_id: str, yes: bool = False) -> None:
    if not yes and not typer.confirm("Approve governed action?", default=False):
        raise typer.Exit(code=10)
    _show(_services(ctx).approvals.approve(approval_id))


@approvals_app.command("deny")
def approvals_deny(ctx: typer.Context, approval_id: str) -> None:
    _show(_services(ctx).approvals.deny(approval_id))


@changes_app.command("keep")
def changes_keep(ctx: typer.Context, session_id: str) -> None:
    session = _services(ctx).store.get_session(session_id)
    if session.status is not SessionStatus.NEEDS_USER_DECISION:
        raise typer.BadParameter("session_not_awaiting_decision")
    _services(ctx).store.transition_session(session_id, SessionStatus.CHANGES_KEPT)
    _show(_services(ctx).store.get_session(session_id))


@changes_app.command("rollback")
def changes_rollback(ctx: typer.Context, session_id: str, yes: bool = False) -> None:
    if not yes and not typer.confirm("Rollback recorded file changes?", default=False):
        raise typer.Exit(code=10)
    service = _services(ctx)
    session = service.store.get_session(session_id)
    project = service.store.get_project(session.project_id)
    service.changes.rollback(session_id, Path(project.canonical_path))
    if session.status is SessionStatus.NEEDS_USER_DECISION:
        service.store.transition_session(session_id, SessionStatus.ROLLED_BACK)
    _show(service.store.get_session(session_id))


@memory_app.command("list")
def memory_list(ctx: typer.Context, project_id: str) -> None:
    _show(_services(ctx).memory.list_safe(project_id))


@memory_app.command("approve")
def memory_approve(ctx: typer.Context, entry_id: str) -> None:
    _show(_services(ctx).memory.approve(entry_id))


@memory_app.command("reject")
def memory_reject(ctx: typer.Context, entry_id: str) -> None:
    _show(_services(ctx).memory.reject(entry_id))


@memory_app.command("delete")
def memory_delete(ctx: typer.Context, entry_id: str) -> None:
    _show(_services(ctx).memory.delete(entry_id))


@sessions_app.callback()
def sessions() -> None:
    """Manage sessions."""


@approvals_app.callback()
def approvals() -> None:
    """Manage approvals."""


@changes_app.callback()
def changes() -> None:
    """Inspect changes."""


@credentials_app.callback()
def credentials() -> None:
    """Manage credentials."""


@memory_app.callback()
def memory() -> None:
    """Manage memory."""


@report_app.callback()
def report() -> None:
    """Generate reports."""


@demo_app.callback()
def demo() -> None:
    """Run demonstrations."""


@demo_app.command("governance")
def demo_governance(ctx: typer.Context) -> None:
    import tempfile

    from coding_agent_harness.application import create_control_application
    from coding_agent_harness.credentials import MemoryCredentialBackend

    with tempfile.TemporaryDirectory(prefix="cah-demo-app-") as name:
        service = create_control_application(Path(name), credential_backend=MemoryCredentialBackend())
        try:
            result = service.demo.run_governance()
            _show(result)
            code = 0 if all(scene.passed for scene in result.scenes) else 1
        finally:
            service.store.close()
    raise typer.Exit(code=code)


app.add_typer(sessions_app, name="sessions")
app.add_typer(approvals_app, name="approvals")
app.add_typer(changes_app, name="changes")
app.add_typer(credentials_app, name="credentials")
app.add_typer(memory_app, name="memory")
app.add_typer(report_app, name="report")
app.add_typer(demo_app, name="demo")


if __name__ == "__main__":
    app()
