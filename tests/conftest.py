from pathlib import Path

import pytest

from coding_agent_harness.storage import StateStore


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
