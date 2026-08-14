from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from pydantic import ValidationError

from coding_agent_harness.config import (
    BudgetConfig,
    ConfigError,
    ProjectConfig,
    UserConfig,
    ValidatorConfig,
    load_project_config,
    load_user_config,
    resolve_config,
    tighten_budgets,
)


def test_load_user_config_parses_trusted_provider_settings() -> None:
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        config_path = Path(directory) / "config.toml"
        config_path.write_text(
            """provider_url = "https://provider.example/v1"
model = "model-a"
credential_profile = "course"
[budgets]
max_steps = 30
""",
            encoding="utf-8",
        )

        config = load_user_config(config_path)

    assert config.provider_url == "https://provider.example/v1"
    assert config.model == "model-a"
    assert config.credential_profile == "course"
    assert config.budgets.max_steps == 30


def test_load_user_config_converts_missing_and_invalid_files_to_config_error() -> None:
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        missing = Path(directory) / "missing.toml"
        invalid = Path(directory) / "invalid.toml"
        invalid.write_text("[broken", encoding="utf-8")

        with pytest.raises(ConfigError, match="user_config_load_failed"):
            load_user_config(missing)
        with pytest.raises(ConfigError, match="user_config_load_failed"):
            load_user_config(invalid)


def test_budget_defaults_and_bounds_are_strict_and_frozen() -> None:
    budgets = BudgetConfig()

    assert budgets.max_steps == 20
    assert budgets.max_observation_bytes == 50000
    with pytest.raises(ValidationError):
        BudgetConfig(max_steps="20")
    with pytest.raises(ValidationError):
        BudgetConfig(max_steps=41)
    with pytest.raises(ValidationError):
        budgets.max_steps = 10


def test_validator_lists_freeze_to_tuples_and_validate_contents() -> None:
    validator = ValidatorConfig(validator_id="tests", args=["-m", "pytest"], stages=["fast"])

    assert validator.args == ("-m", "pytest")
    assert validator.stages == ("fast",)
    with pytest.raises(ValidationError):
        ValidatorConfig(validator_id="", args=["-m", 1])
    with pytest.raises(ValidationError):
        ValidatorConfig(validator_id="tests", stages=["slow"])


def test_project_config_rejects_unknown_or_provider_fields() -> None:
    with pytest.raises(ValidationError):
        ProjectConfig(provider_url="https://evil.invalid")
    with pytest.raises(ValidationError):
        ProjectConfig(source_roots=["src", 1])
    with pytest.raises(ValidationError):
        ProjectConfig(source_roots=[])


def test_load_project_config_parses_toml_and_preserves_explicit_budget_fields() -> None:
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        config_path = Path(directory) / "project.toml"
        config_path.write_text(
            """source_roots = [\"app\", \"checks\"]
[[validators]]
validator_id = \"unit\"
args = [\"-m\", \"pytest\"]
stages = [\"baseline\", \"final\"]
[budgets]
max_steps = 9
""",
            encoding="utf-8",
        )

        project = load_project_config(config_path)

    assert project.source_roots == ("app", "checks")
    assert project.validators[0].args == ("-m", "pytest")
    assert project.validators[0].stages == ("baseline", "final")
    assert project.budgets.max_steps == 9
    assert project.budgets.model_fields_set == {"max_steps"}


def test_load_project_config_converts_file_errors_to_config_error() -> None:
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        missing = Path(directory) / "missing.toml"
        invalid = Path(directory) / "invalid.toml"
        invalid.write_text("[broken", encoding="utf-8")

        with pytest.raises(ConfigError):
            load_project_config(missing)
        with pytest.raises(ConfigError):
            load_project_config(invalid)


def test_load_project_config_rejects_unknown_toml_fields() -> None:
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        config_path = Path(directory) / "project.toml"
        config_path.write_text("network_permission = true", encoding="utf-8")

        with pytest.raises(ValidationError):
            load_project_config(config_path)


def test_tighten_budgets_uses_each_field_minimum() -> None:
    trusted = BudgetConfig(max_steps=20, max_llm_calls=12)
    lower_trust = BudgetConfig(max_steps=7, max_llm_calls=20)

    tightened = tighten_budgets(trusted, lower_trust)

    assert tightened.max_steps == 7
    assert tightened.max_llm_calls == 12


def test_resolve_config_only_applies_explicit_project_and_cli_budget_fields() -> None:
    user = UserConfig(
        provider_url="https://provider.example/v1",
        model="model-a",
        credential_profile="main",
        budgets=BudgetConfig(max_steps=30, max_llm_calls=18, command_timeout_seconds=250),
    )
    project = ProjectConfig(
        source_roots=["app"],
        budgets=BudgetConfig(max_steps=10),
    )
    cli = BudgetConfig(max_llm_calls=5)

    resolved = resolve_config(user, project, cli)

    assert resolved.provider_url == "https://provider.example/v1"
    assert resolved.model == "model-a"
    assert resolved.credential_profile == "main"
    assert resolved.source_roots == ("app",)
    assert resolved.budgets.max_steps == 10
    assert resolved.budgets.max_llm_calls == 5
    assert resolved.budgets.command_timeout_seconds == 250
    with pytest.raises(ValidationError):
        resolved.model = "changed"


def test_resolve_config_uses_project_defaults_when_absent() -> None:
    resolved = resolve_config(UserConfig())

    assert resolved.source_roots == ("src", "tests")
    assert resolved.validators[0].validator_id == "pytest"
    assert resolved.validators[0].args == ("-m", "pytest")


def test_project_default_pytest_validator_has_pytest_module_arguments() -> None:
    project = ProjectConfig()

    assert project.validators == (ValidatorConfig(validator_id="pytest", args=("-m", "pytest")),)


@pytest.mark.parametrize("factory", (UserConfig, ValidatorConfig))
def test_required_strings_are_non_empty(factory: type[UserConfig] | type[ValidatorConfig]) -> None:
    if factory is UserConfig:
        with pytest.raises(ValidationError):
            factory(model="")
    else:
        with pytest.raises(ValidationError):
            factory(validator_id="")
