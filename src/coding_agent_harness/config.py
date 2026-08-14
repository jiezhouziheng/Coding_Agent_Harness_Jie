"""Strict, layered configuration models for governed harness execution."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from coding_agent_harness.models import StrictModel


class ConfigError(ValueError):
    """Raised when a project configuration file cannot be read or parsed."""


class BudgetConfig(StrictModel):
    max_steps: int = Field(default=20, ge=1, le=40)
    max_llm_calls: int = Field(default=12, ge=1, le=24)
    max_consecutive_failures: int = Field(default=4, ge=1, le=8)
    max_repeated_action: int = Field(default=2, ge=1, le=3)
    command_timeout_seconds: int = Field(default=120, ge=1, le=300)
    session_timeout_minutes: int = Field(default=30, ge=1, le=60)
    max_observation_bytes: int = Field(default=50000, ge=1000, le=100000)


class ValidatorConfig(StrictModel):
    validator_id: str = Field(min_length=1)
    program: str = Field(default="python", min_length=1)
    args: tuple[str, ...] = ()
    stages: tuple[Literal["baseline", "fast", "final"], ...] = (
        "baseline",
        "fast",
        "final",
    )
    required: bool = True

    @field_validator("args", "stages", mode="before")
    @classmethod
    def freeze_lists(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value


class UserConfig(StrictModel):
    provider_url: str = Field(default="https://api.openai.com/v1", min_length=1)
    model: str = Field(default="gpt-5-mini", min_length=1)
    credential_profile: str = Field(default="default", min_length=1)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)


class ProjectConfig(StrictModel):
    source_roots: tuple[str, ...] = ("src", "tests")
    validators: tuple[ValidatorConfig, ...] = (
        ValidatorConfig(validator_id="pytest", args=("-m", "pytest")),
    )
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)

    @field_validator("source_roots", "validators", mode="before")
    @classmethod
    def freeze_lists(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def validate_source_roots(self) -> Self:
        if not self.source_roots or any(not root for root in self.source_roots):
            raise ValueError("source_roots must contain non-empty paths")
        return self


class ResolvedConfig(StrictModel):
    provider_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    credential_profile: str = Field(min_length=1)
    source_roots: tuple[str, ...]
    validators: tuple[ValidatorConfig, ...]
    budgets: BudgetConfig


def tighten_budgets(trusted: BudgetConfig, lower_trust: BudgetConfig) -> BudgetConfig:
    return BudgetConfig(
        **{
            name: min(getattr(trusted, name), getattr(lower_trust, name))
            for name in BudgetConfig.model_fields
        }
    )


def load_project_config(path: Path) -> ProjectConfig:
    try:
        with path.open("rb") as file:
            payload = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"could not load project config: {path}") from error
    return ProjectConfig.model_validate(payload)


def load_user_config(path: Path) -> UserConfig:
    """Load the trusted user configuration with a stable fail-closed error."""
    try:
        with path.open("rb") as file:
            payload = tomllib.load(file)
        return UserConfig.model_validate(payload)
    except (OSError, tomllib.TOMLDecodeError, ValidationError):
        raise ConfigError("user_config_load_failed") from None


def resolve_config(
    user: UserConfig, project: ProjectConfig | None = None, cli: BudgetConfig | None = None
) -> ResolvedConfig:
    selected_project = project if project is not None else ProjectConfig()
    budgets = user.budgets
    for lower_trust in (selected_project.budgets, cli):
        if lower_trust is not None:
            budgets = _tighten_explicit_budgets(budgets, lower_trust)
    return ResolvedConfig(
        provider_url=user.provider_url,
        model=user.model,
        credential_profile=user.credential_profile,
        source_roots=selected_project.source_roots,
        validators=selected_project.validators,
        budgets=budgets,
    )


def _tighten_explicit_budgets(trusted: BudgetConfig, lower_trust: BudgetConfig) -> BudgetConfig:
    values = trusted.model_dump()
    for name in lower_trust.model_fields_set:
        values[name] = min(values[name], getattr(lower_trust, name))
    return BudgetConfig.model_validate(values)
