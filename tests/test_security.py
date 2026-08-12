from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from coding_agent_harness.models import (
    CreateFileAction,
    DeleteFileAction,
    ListFilesAction,
    ReadFileAction,
    ReplaceInFileAction,
    RunCommandAction,
)
from coding_agent_harness.security import (
    SecurityViolation,
    WorkspaceGuard,
    action_fingerprint,
    normalize_action,
    redact_text,
    scrub_environment,
)


@pytest.fixture
def workspace() -> Path:
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        root = Path(directory)
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
        yield root


def test_guard_accepts_relative_paths_and_dot(workspace: Path) -> None:
    guard = WorkspaceGuard(workspace)

    assert guard.resolve(".") == workspace.resolve()
    assert guard.resolve("src/app.py", must_exist=True) == (workspace / "src" / "app.py").resolve()


@pytest.mark.parametrize(
    "raw",
    ("", "/etc/passwd", r"C:\\temp\\x", r"\\\\server\\share\\x", "../x", "src/../x", "a\x00b"),
)
def test_guard_rejects_unsafe_path_forms(workspace: Path, raw: str) -> None:
    with pytest.raises(SecurityViolation):
        WorkspaceGuard(workspace).resolve(raw)


@pytest.mark.parametrize("raw", (".env::$DATA", ".env:$DATA", "notes:stream"))
def test_guard_rejects_colons_to_prevent_ntfs_alternate_data_streams(
    workspace: Path, raw: str
) -> None:
    with pytest.raises(SecurityViolation):
        WorkspaceGuard(workspace).resolve(raw)


@pytest.mark.parametrize("raw", (".env", "nested/.env", ".env.local", ".git/config", ".ssh/key", "config/cert.pem", "key.key", "id_rsa", "ID_ED25519"))
def test_guard_rejects_sensitive_paths_at_any_depth(workspace: Path, raw: str) -> None:
    with pytest.raises(SecurityViolation):
        WorkspaceGuard(workspace).resolve(raw)


def test_guard_requires_existence_and_honors_creatable_nested_parents(workspace: Path) -> None:
    guard = WorkspaceGuard(workspace)

    with pytest.raises(SecurityViolation):
        guard.resolve("missing.txt", must_exist=True)
    assert guard.resolve("new/deep/file.txt") == workspace / "new" / "deep" / "file.txt"


def test_guard_relative_rejects_outside_paths(workspace: Path) -> None:
    with pytest.raises(SecurityViolation):
        WorkspaceGuard(workspace).relative(workspace.parent)


def test_guard_blocks_symlinks_that_escape_workspace(workspace: Path) -> None:
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        link = workspace / "linked"
        try:
            link.symlink_to(directory, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"symlinks unavailable: {error}")

        with pytest.raises(SecurityViolation):
            WorkspaceGuard(workspace).resolve("linked/secret.txt")


@pytest.mark.parametrize(("raw", "target_is_directory"), (("dangling", False), ("dangling/child.py", True)))
def test_guard_blocks_dangling_symlink_components(
    workspace: Path, raw: str, target_is_directory: bool
) -> None:
    link = workspace / "dangling"
    try:
        link.symlink_to(
            workspace.parent / "missing-outside-target",
            target_is_directory=target_is_directory,
        )
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises(SecurityViolation):
        WorkspaceGuard(workspace).resolve(raw)


def test_guard_checks_dangling_components_even_when_exists_is_false(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = WorkspaceGuard(workspace)
    original_is_symlink = Path.is_symlink
    original_resolve = Path.resolve

    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path.name == "dangling" or original_is_symlink(path),
    )

    def resolve_or_fail(path: Path, strict: bool = False) -> Path:
        if path.name == "dangling":
            raise FileNotFoundError("simulated dangling link")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_or_fail)

    with pytest.raises(SecurityViolation):
        guard.resolve("dangling/child.py")


def test_guard_wraps_existing_candidate_resolution_errors(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = WorkspaceGuard(workspace)
    candidate = workspace / "src" / "app.py"
    original_resolve = Path.resolve

    def resolve_or_fail(path: Path, strict: bool = False) -> Path:
        if path == candidate and strict:
            raise OSError("simulated candidate failure")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_or_fail)

    with pytest.raises(SecurityViolation):
        guard.resolve("src/app.py")


def test_guard_wraps_existing_ancestor_resolution_errors(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = WorkspaceGuard(workspace)
    original_resolve = Path.resolve

    def resolve_or_fail(path: Path, strict: bool = False) -> Path:
        if path == workspace and strict:
            raise RuntimeError("simulated ancestor cycle")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_or_fail)

    with pytest.raises(SecurityViolation):
        guard.resolve("new/deep/file.py")


def test_normalize_action_rebuilds_immutable_actions_with_relative_paths(workspace: Path) -> None:
    guard = WorkspaceGuard(workspace)
    listed = normalize_action(ListFilesAction(path="src"), guard)
    read = normalize_action(ReadFileAction(path="src/app.py"), guard)
    replaced = normalize_action(
        ReplaceInFileAction(path="src/app.py", old_text="ok", new_text="updated"), guard
    )
    created = normalize_action(CreateFileAction(path="new/note.txt", content="hello"), guard)
    deleted = normalize_action(DeleteFileAction(path="src/app.py"), guard)
    command = normalize_action(
        RunCommandAction(program="PYTHON", args=("-m", "pytest"), cwd="src"), guard
    )

    assert listed.path == "src"
    assert read.path == "src/app.py"
    assert replaced.path == "src/app.py"
    assert created.path == "new/note.txt"
    assert deleted.path == "src/app.py"
    assert command.program == "python"
    assert command.args == ("-m", "pytest")
    assert command.cwd == "src"
    assert type(command) is RunCommandAction


@pytest.mark.parametrize(
    "action",
    (
        ListFilesAction(path="missing"),
        ReadFileAction(path="missing.txt"),
        ReplaceInFileAction(path="missing.txt", old_text="before", new_text="after"),
        DeleteFileAction(path="missing.txt"),
    ),
)
def test_normalize_action_requires_existing_target(workspace: Path, action: object) -> None:
    with pytest.raises(SecurityViolation):
        normalize_action(action, WorkspaceGuard(workspace))


def test_fingerprint_is_stable_for_equivalent_action_payloads() -> None:
    first = RunCommandAction(program="python", args=("-m", "pytest"), timeout_seconds=10)
    second = RunCommandAction(args=("-m", "pytest"), program="python", timeout_seconds=10)

    assert action_fingerprint(first) == action_fingerprint(second)
    assert len(action_fingerprint(first)) == 64


def test_redaction_replaces_secrets_tokens_and_workspace_paths(workspace: Path) -> None:
    text = f"Bearer abc.def api_key=secret123 wrote {workspace / 'src' / 'app.py'}"

    assert redact_text(text, workspace=workspace, secrets=("secret123",)) == (
        "Bearer <REDACTED> api_key=<REDACTED> wrote <WORKSPACE>/src/app.py"
    )
    assert redact_text("ordinary text") == "ordinary text"


def test_scrub_environment_removes_sensitive_names_without_mutating_input() -> None:
    environment = {"PATH": "bin", "Api_Token": "x", "password": "y", "MODE": "dev"}

    assert scrub_environment(environment) == {"PATH": "bin", "MODE": "dev"}
    assert environment["Api_Token"] == "x"
