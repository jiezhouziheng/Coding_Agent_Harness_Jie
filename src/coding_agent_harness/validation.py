from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from coding_agent_harness.config import ValidatorConfig
from coding_agent_harness.models import Observation, RunCommandAction, ValidationResult


class ValidationStage(StrEnum):
    BASELINE = "baseline"
    FAST = "fast"
    FINAL = "final"


class ValidationPipeline:
    def __init__(self, runner: Any, validators: tuple[ValidatorConfig, ...]) -> None:
        self.runner = runner
        self.validators = validators

    @classmethod
    def default(cls, runner: Any) -> ValidationPipeline:
        return cls(runner, (ValidatorConfig(validator_id="pytest", args=("-m", "pytest")),))

    def run(self, stage: ValidationStage, workspace: Path) -> list[ValidationResult]:
        results: list[ValidationResult] = []
        for spec in self.validators:
            if stage.value not in spec.stages:
                continue
            command = RunCommandAction(program=spec.program, args=spec.args, cwd=".")
            try:
                raw = self.runner.run(command, workspace=workspace)
            except (OSError, RuntimeError, ValueError) as error:
                results.append(ValidationResult(validator_id=spec.validator_id, stage=stage.value, status="error", exit_code=None, duration_ms=0, summary=f"{spec.validator_id} error", evidence=str(error)))
                continue
            status = cast(
                Any, "timeout" if raw.timed_out else "passed" if raw.exit_code == 0 else "failed"
            )
            results.append(ValidationResult(validator_id=spec.validator_id, stage=stage.value, status=status, exit_code=raw.exit_code, duration_ms=raw.duration_ms, summary=f"{spec.validator_id} {status}", evidence=(raw.stdout + raw.stderr)[:50_000]))
        return results

    def success_gate_open(self, results: list[ValidationResult]) -> bool:
        return bool(results) and all(item.status == "passed" for item in results)


def observation_from_validation(action_id: str, results: list[ValidationResult]) -> Observation:
    failed = next((item for item in results if item.status != "passed"), None)
    if failed is None and results:
        return Observation(action_id=action_id, category="success", summary="validation passed")
    if failed is None:
        return Observation(action_id=action_id, category="tool_error", summary="no validators configured")
    category = cast(
        Any,
        {"pytest": "test_failure", "ruff": "lint_failure", "mypy": "type_failure"}.get(
            failed.validator_id, "timeout" if failed.status == "timeout" else "tool_error"
        ),
    )
    return Observation(action_id=action_id, category=category, summary=failed.summary, evidence=failed.evidence)
