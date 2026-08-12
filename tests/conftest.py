from pathlib import Path

import pytest

from coding_agent_harness.storage import StateStore


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
