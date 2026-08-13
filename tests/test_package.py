import pytest
from typer.testing import CliRunner

from coding_agent_harness import __version__
from coding_agent_harness.cli import app


def test_package_version() -> None:
    assert __version__ == "0.1.0"


def test_cli_help_lists_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in (
        "run",
        "sessions",
        "approvals",
        "changes",
        "credentials",
        "memory",
        "report",
        "demo",
    ):
        assert command in result.stdout


@pytest.mark.parametrize(
    ("group", "description"),
    (
        ("sessions", "Manage sessions."),
        ("approvals", "Manage approvals."),
        ("changes", "Inspect changes."),
        ("credentials", "Manage credentials."),
        ("memory", "Manage memory."),
        ("report", "Generate reports."),
        ("demo", "Run demonstrations."),
    ),
)
def test_empty_command_group_shows_help(group: str, description: str) -> None:
    result = CliRunner().invoke(app, [group])

    assert "Usage:" in result.stdout
    assert description in result.stdout
