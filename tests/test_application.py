from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent_harness.application import create_control_application
from coding_agent_harness.config import BudgetConfig, ConfigError
from coding_agent_harness.credentials import MemoryCredentialBackend
from coding_agent_harness.llm import OpenAICompatibleClient, ScriptedMockLLM


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
