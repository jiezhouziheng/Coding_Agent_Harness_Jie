import re
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml
from typer.testing import CliRunner

from coding_agent_harness import __version__
from coding_agent_harness.cli import app

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _load_yaml(relative: str) -> dict[str, object]:
    loaded = yaml.load(_read(relative), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _run_git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _make_targets() -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    targets: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    current: str | None = None
    for line in _read("Makefile").splitlines():
        if line.startswith("\t"):
            assert current is not None
            dependencies, recipes = targets[current]
            targets[current] = (dependencies, (*recipes, line.strip()))
            continue
        match = re.fullmatch(r"([a-z][a-z0-9_-]*):(?:\s+(.*))?", line)
        if match:
            current = match.group(1)
            dependencies = tuple((match.group(2) or "").split())
            targets[current] = (dependencies, ())
    return targets


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


def test_required_delivery_files_and_readme_sections_exist() -> None:
    required = (
        ".github/workflows/ci.yml",
        ".github/workflows/pages.yml",
        ".gitlab-ci.yml",
        "Makefile",
        "scripts/verify.ps1",
        "scripts/verify.sh",
    )
    for relative in required:
        assert (ROOT / relative).is_file(), relative

    readme = _read("README.md")
    for heading in (
        "## 项目简介",
        "## 安装",
        "## 运行",
        "## 凭据管理",
        "## 凭据安全",
        "## 目录结构",
        "## 静态 WebUI",
        "## 安全边界",
        "## 分发",
        "## 已知限制",
    ):
        assert heading in readme
    for required_text in (
        "pip install coding-agent-harness-jie",
        "pipx install coding-agent-harness-jie",
        "发布到受信任索引后",
        "pip install dist/",
        "cah demo governance",
        "cah run --workspace",
        "cah sessions show <session-id>",
        "cah approvals approve <approval-id>",
        "cah changes rollback <session-id>",
        "cah memory list <project-id>",
        "cah report export <session-id> <output>",
        "credentials set",
        "credentials status",
        "credentials update",
        "credentials clear",
        "Keyring",
        "`.env`",
        "%LOCALAPPDATA%\\CodingAgentHarness",
        "~/.local/share/CodingAgentHarness",
        "静态 WebUI",
        "state.db",
        "audit/events.jsonl",
        "backups",
        "当前 OS 用户权限",
        "没有额外的 OS sandbox",
        "wheel",
        "sdist",
        "git archive --format=zip",
        "CI 配置目标",
        "script_exhausted",
        "--mock-script",
    ):
        assert required_text in readme


def test_github_ci_has_offline_quality_and_distribution_contract() -> None:
    ci = _load_yaml(".github/workflows/ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    triggers = ci["on"]
    assert isinstance(triggers, dict)
    assert {"push", "pull_request"} <= set(triggers)

    jobs = ci["jobs"]
    assert isinstance(jobs, dict)
    unit_test = jobs["unit-test"]
    assert isinstance(unit_test, dict)
    steps = unit_test["steps"]
    assert isinstance(steps, list)
    setup_python = next(step for step in steps if step.get("uses") == "actions/setup-python@v5")
    assert setup_python["with"] == {"python-version": "3.13", "cache": "pip"}
    expected_commands = [
        'python -m pip install ".[dev]"',
        "python -m pytest",
        "python -m ruff check .",
        "python -m mypy --strict src",
        "python -m build",
    ]
    assert [step["run"] for step in steps if "run" in step] == expected_commands
    upload = next(step for step in steps if step.get("uses") == "actions/upload-artifact@v4")
    assert upload["with"] == {"name": "python-distributions", "path": "dist/*"}


@pytest.mark.parametrize("platform", ("win32", "linux"))
def test_strict_mypy_passes_for_supported_ci_platforms(platform: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "--platform", platform, "src"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"strict mypy failed for {platform}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_gitlab_ci_matches_quality_and_distribution_contract() -> None:
    gitlab = _load_yaml(".gitlab-ci.yml")
    assert gitlab["image"] == "python:3.13"
    unit_test = gitlab["unit-test"]
    assert isinstance(unit_test, dict)
    assert unit_test["script"] == [
        'python -m pip install ".[dev]"',
        "python -m pytest",
        "python -m ruff check .",
        "python -m mypy --strict src",
        "python -m build",
    ]
    assert unit_test["artifacts"] == {"when": "on_success", "paths": ["dist/*"]}


def test_pages_workflow_is_static_only_and_least_privilege() -> None:
    pages = _load_yaml(".github/workflows/pages.yml")
    assert pages["permissions"] == {"contents": "read"}
    triggers = pages["on"]
    assert isinstance(triggers, dict)
    assert set(triggers) == {"push", "workflow_dispatch"}
    push = triggers["push"]
    assert isinstance(push, dict)
    assert push == {"branches": ["main"]}
    assert pages["concurrency"] == {
        "group": "pages-production",
        "cancel-in-progress": "true",
    }

    jobs = pages["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"deploy"}
    deploy = jobs["deploy"]
    assert isinstance(deploy, dict)
    assert deploy["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    assert deploy["if"] == "github.ref == 'refs/heads/main'"
    steps = deploy["steps"]
    assert isinstance(steps, list)
    assert len(steps) == 7
    checkout, setup_python, install, reporting_test, prepare_step, upload, deploy_step = steps
    assert checkout == {"name": "Check out source", "uses": "actions/checkout@v4"}
    assert setup_python == {
        "name": "Set up Python",
        "uses": "actions/setup-python@v5",
        "with": {"python-version": "3.13", "cache": "pip"},
    }
    assert install == {
        "name": "Install development dependencies",
        "run": 'python -m pip install ".[dev]"',
    }
    assert reporting_test == {
        "name": "Validate static report",
        "run": "python -m pytest tests/test_reporting.py",
    }
    assert set(prepare_step) == {"name", "run"}
    assert prepare_step["name"] == "Prepare static WebUI"
    prepare = prepare_step["run"]
    assert prepare.splitlines() == [
        "rm -rf _site",
        "mkdir -p _site",
        "cp -R src/coding_agent_harness/web/. _site/",
    ]
    assert upload == {
        "name": "Upload Pages artifact",
        "uses": "actions/upload-pages-artifact@v3",
        "with": {"path": "_site"},
    }
    assert deploy_step == {"name": "Deploy Pages", "uses": "actions/deploy-pages@v4"}
    forbidden = (
        "cah run",
        "coding_agent_harness.cli",
        "sqlite",
        "websocket",
        "uvicorn",
        "fastapi",
        "approval",
        "control api",
    )
    run_commands = [step["run"] for step in steps if "run" in step]
    assert run_commands == [install["run"], reporting_test["run"], prepare]
    lowered = "\n".join(run_commands).lower()
    assert all(token not in lowered for token in forbidden)


def test_verification_scripts_are_fail_fast_and_run_quality_commands_in_order() -> None:
    commands = (
        "python -m pytest",
        "python -m ruff check .",
        "python -m mypy --strict src",
        "python -m build",
    )
    for relative in ("scripts/verify.ps1", "scripts/verify.sh"):
        script = _read(relative)
        positions = [script.index(command) for command in commands]
        assert positions == sorted(positions), relative
        assert "pytest" in script and "ruff" in script and "mypy" in script and "build" in script
        lowered = script.lower()
        for forbidden in (".env", "keyring", "api_key", "api-key", "llm"):
            assert forbidden not in lowered, (relative, forbidden)
    powershell = _read("scripts/verify.ps1")
    assert "$ErrorActionPreference = \"Stop\"" in powershell
    for required in (
        "$repoRoot = Split-Path -Parent $PSScriptRoot",
        "Push-Location $repoRoot",
        "try {",
        "finally {",
        "Pop-Location",
    ):
        assert required in powershell
    failure_check = "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"
    assert powershell.count(failure_check) == len(commands)
    powershell_lines = [line.strip() for line in powershell.splitlines()]
    for command in commands:
        command_index = next(
            index for index, line in enumerate(powershell_lines) if line.startswith(command)
        )
        assert powershell_lines[command_index + 1] == failure_check
    shell = _read("scripts/verify.sh")
    assert "set -euo pipefail" in shell
    assert 'cd "$(dirname "${BASH_SOURCE[0]}")/.."' in shell
    assert "|| true" not in shell

    targets = _make_targets()
    assert targets["test"] == ((), ("python -m pytest",))
    assert targets["lint"] == ((), ("python -m ruff check .",))
    assert targets["typecheck"] == ((), ("python -m mypy --strict src",))
    assert targets["build"] == ((), ("python -m build --no-isolation",))
    assert targets["verify"] == (("test", "lint", "typecheck", "build"), ())


def test_built_distributions_include_static_web_resources(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--wheel",
            "--sdist",
            "--outdir",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "distribution build failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    wheels = list(tmp_path.glob("*.whl"))
    sdists = list(tmp_path.glob("*.tar.gz"))
    assert len(wheels) == 1, [path.name for path in wheels]
    assert len(sdists) == 1, [path.name for path in sdists]
    wheel = wheels[0]
    required = {
        "coding_agent_harness/web/index.html",
        "coding_agent_harness/web/app.js",
        "coding_agent_harness/web/styles.css",
        "coding_agent_harness/web/mock-report.json",
    }
    with ZipFile(wheel) as archive:
        assert required <= set(archive.namelist())

    sdist = sdists[0]
    with tarfile.open(sdist) as archive:
        names = set(archive.getnames())
        for relative in ("index.html", "app.js", "styles.css", "mock-report.json"):
            assert any(
                name.endswith(f"/src/coding_agent_harness/web/{relative}") for name in names
            )


def test_package_metadata_explicitly_includes_static_web_resources() -> None:
    metadata = tomllib.loads(_read("pyproject.toml"))
    assert "PyYAML>=6,<7" in metadata["project"]["optional-dependencies"]["dev"]
    assert "hatchling>=1.27" in metadata["project"]["optional-dependencies"]["dev"]
    wheel = metadata["tool"]["hatch"]["build"]["targets"]["wheel"]
    include = wheel.get("include", [])
    assert any("coding_agent_harness/web" in pattern for pattern in include)
    assert (ROOT / "src/coding_agent_harness/web/index.html").is_file()
    assert (ROOT / "src/coding_agent_harness/web/styles.css").is_file()
    assert (ROOT / "src/coding_agent_harness/web/app.js").is_file()
    assert (ROOT / "src/coding_agent_harness/web/mock-report.json").is_file()


def test_ignore_and_line_ending_rules_protect_private_outputs() -> None:
    private_paths = (
        ".venv/pyvenv.cfg",
        ".pytest_cache/v/cache/nodeids",
        ".mypy_cache/3.13/module.data.json",
        ".ruff_cache/content",
        ".coverage",
        "htmlcov/index.html",
        "dist/package.whl",
        "build/lib/module.py",
        "package.egg-info/PKG-INFO",
        ".cah/state.db",
        "state.db",
        "state.sqlite-wal",
        "audit/events.jsonl",
        "backups/session/file.bak",
        "reports/private/session.json",
    )
    for relative in private_paths:
        result = _run_git("check-ignore", "--no-index", "--quiet", "--", relative)
        assert result.returncode == 0, (relative, result.stderr)

    public_report = "src/coding_agent_harness/web/mock-report.json"
    result = _run_git("check-ignore", "--no-index", "--quiet", "--", public_report)
    assert result.returncode == 1, (public_report, result.stderr)

    expected_eol = {
        "src/coding_agent_harness/__init__.py": "lf",
        "README.md": "lf",
        ".github/workflows/ci.yml": "lf",
        "src/coding_agent_harness/web/index.html": "lf",
        "src/coding_agent_harness/web/styles.css": "lf",
        "src/coding_agent_harness/web/app.js": "lf",
        "scripts/verify.sh": "lf",
        ".gitignore": "lf",
        ".gitattributes": "lf",
        "scripts/verify.ps1": "crlf",
    }
    for relative, eol in expected_eol.items():
        result = _run_git("check-attr", "eol", "--", relative)
        assert result.returncode == 0, (relative, result.stderr)
        assert result.stdout.strip().endswith(f": eol: {eol}"), result.stdout
