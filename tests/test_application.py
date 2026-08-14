from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent_harness.application import HarnessApplication, create_control_application
from coding_agent_harness.config import BudgetConfig, ConfigError
from coding_agent_harness.credentials import MemoryCredentialBackend
from coding_agent_harness.llm import OpenAICompatibleClient, ScriptedMockLLM


class TrackingLock:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class TrackingSessions:
    def __init__(self) -> None:
        self.workspaces: list[Path] = []
        self.lock = TrackingLock()

    def acquire_workspace(self, workspace: Path) -> TrackingLock:
        self.workspaces.append(workspace)
        return self.lock


class ExplodingEngineFactory:
    def create(self, **_kwargs: object):
        raise RuntimeError("engine_factory_failed")


def test_engine_factory_wires_layered_config_and_keyring_client(
    app_data: Path, workspace: Path
) -> None:
    (workspace / "src").mkdir()
    (app_data / "config.toml").write_text(
        """provider_url = "https://provider.example/v1"
model = "model-a"
credential_profile = "course"
[budgets]
max_steps = 30
max_llm_calls = 20
command_timeout_seconds = 240
""",
        encoding="utf-8",
    )
    (workspace / "harness.toml").write_text(
        """source_roots = ["src"]
[[validators]]
validator_id = "pytest"
program = "python"
args = ["-m", "pytest", "-q"]
stages = ["baseline", "final"]
[budgets]
max_steps = 8
""",
        encoding="utf-8",
    )
    backend = MemoryCredentialBackend()
    backend.set("coding-agent-harness", "course", "test-secret")
    service = create_control_application(app_data, credential_backend=backend)

    try:
        session_id, engine = service.engine_factory.create(
            workspace=workspace,
            task="fix tests",
            cli_budget=BudgetConfig(max_llm_calls=3, command_timeout_seconds=30),
        )

        assert isinstance(engine.llm, OpenAICompatibleClient)
        assert engine.llm.base_url == "https://provider.example/v1"
        assert engine.llm.model == "model-a"
        assert engine.llm.api_key == "test-secret"
        assert engine.validators.validators[0].args == ("-m", "pytest", "-q")
        assert engine.validators.command_timeout_seconds == 30
        assert engine.context_builder.source_roots == ("src",)
        assert engine.policy.context.budgets.command_timeout_seconds == 30
        budget = service.store.get_session(session_id).budget
        assert budget["max_steps"] == 8
        assert budget["max_llm_calls"] == 3
        assert budget["command_timeout_seconds"] == 30
    finally:
        service.store.close()


def test_mock_script_uses_project_config_without_reading_key(
    app_data: Path, workspace: Path, tmp_path: Path
) -> None:
    (workspace / "harness.toml").write_text(
        """[[validators]]
validator_id = "pytest"
args = ["-m", "pytest", "-q"]
[budgets]
max_steps = 6
""",
        encoding="utf-8",
    )
    script = tmp_path / "script.json"
    script.write_text("[]", encoding="utf-8")
    service = create_control_application(
        app_data, credential_backend=MemoryCredentialBackend()
    )

    try:
        session_id, engine = service.engine_factory.create(
            workspace=workspace, task="offline", mock_script=script
        )

        assert isinstance(engine.llm, ScriptedMockLLM)
        assert engine.validators.validators[0].args == ("-m", "pytest", "-q")
        assert service.store.get_session(session_id).budget["max_steps"] == 6
    finally:
        service.store.close()


def test_unsafe_project_validator_is_rejected_before_session_creation(
    app_data: Path, workspace: Path
) -> None:
    (workspace / "harness.toml").write_text(
        """[[validators]]
validator_id = "network"
program = "curl"
args = ["https://example.invalid"]
""",
        encoding="utf-8",
    )
    service = create_control_application(
        app_data, credential_backend=MemoryCredentialBackend()
    )

    try:
        with pytest.raises(ConfigError, match="unsafe_project_validator"):
            service.engine_factory.create(workspace=workspace, task="unsafe")
        assert service.store.list_sessions() == ()
    finally:
        service.store.close()


def test_project_source_root_escape_is_rejected_before_session_creation(
    app_data: Path, workspace: Path
) -> None:
    (workspace / "harness.toml").write_text(
        'source_roots = ["../outside"]\n', encoding="utf-8"
    )
    service = create_control_application(
        app_data, credential_backend=MemoryCredentialBackend()
    )

    try:
        with pytest.raises(ConfigError, match="unsafe_source_root"):
            service.engine_factory.create(workspace=workspace, task="unsafe")
        assert service.store.list_sessions() == ()
    finally:
        service.store.close()


def test_application_run_releases_workspace_lock_when_factory_fails(
    workspace: Path,
) -> None:
    sessions = TrackingSessions()
    service = HarnessApplication(
        store=None,
        sessions=sessions,  # type: ignore[arg-type]
        approvals=None,
        changes=None,
        credentials=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        reports=None,
        engine_factory=ExplodingEngineFactory(),
    )

    with pytest.raises(RuntimeError, match="engine_factory_failed"):
        service.run(workspace=workspace, task="fail after locking")

    assert sessions.workspaces == [workspace]
    assert sessions.lock.released is True
