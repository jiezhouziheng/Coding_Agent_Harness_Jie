from pathlib import Path

import pytest

from coding_agent_harness.storage import StateStore


@pytest.fixture
def credential_backend():
    from coding_agent_harness.credentials import MemoryCredentialBackend

    return MemoryCredentialBackend()


@pytest.fixture
def cli_app(app_data: Path, credential_backend, monkeypatch: pytest.MonkeyPatch):
    from coding_agent_harness import cli
    from coding_agent_harness.application import create_control_application

    service = create_control_application(app_data, credential_backend=credential_backend)
    monkeypatch.setattr(cli, "_services", lambda _ctx: service)
    return cli.app


@pytest.fixture
def app_factory():
    from coding_agent_harness.application import create_control_application
    from coding_agent_harness.credentials import MemoryCredentialBackend

    def factory(*, workspace: Path, llm):
        app_data = workspace.parent / ".cah-app-data"
        service = create_control_application(
            app_data,
            credential_backend=MemoryCredentialBackend(),
            llm_factory=lambda: llm,
        )
        service.default_workspace = workspace
        return service

    return factory


class MemoryAuditWriter:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def append(self, event: dict[str, object]) -> None:
        self.events.append(event)


@pytest.fixture
def app_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("cah-app-data")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    return workspace_path


@pytest.fixture
def store(app_data: Path) -> StateStore:
    value = StateStore(app_data / "state.db")
    value.initialize()
    yield value
    value.close()


@pytest.fixture
def session_id(store: StateStore, workspace: Path) -> str:
    project_id = store.upsert_project(workspace, "Task 6")
    return store.create_session(project_id, "file tools")


@pytest.fixture
def journal(store: StateStore, app_data: Path):
    from coding_agent_harness.journal import ChangeJournal

    return ChangeJournal(store, app_data / "backups")


@pytest.fixture
def tools(workspace: Path, journal):
    from coding_agent_harness.file_tools import FileTools

    (workspace / "existing.py").write_text("before\n", encoding="utf-8")
    (workspace / "delete.py").write_text("delete me\n", encoding="utf-8")
    return FileTools(workspace, journal)


@pytest.fixture
def audit_writer() -> MemoryAuditWriter:
    return MemoryAuditWriter()


class FakeCommandRunner:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.queue_items: list[object] = []

    def queue(self, *, exit_code: int | None = 0, stdout: str = "", stderr: str = "", timed_out: bool = False, duration_ms: int = 1) -> None:
        from types import SimpleNamespace

        self.queue_items.append(SimpleNamespace(exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out, duration_ms=duration_ms, truncated=False))

    def run(self, action, *, workspace):
        self.calls.append((action, workspace))
        if not self.queue_items:
            raise AssertionError("fake runner queue is empty")
        return self.queue_items.pop(0)


@pytest.fixture
def fake_runner() -> FakeCommandRunner:
    return FakeCommandRunner()
