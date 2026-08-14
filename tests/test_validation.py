from pathlib import Path

import pytest

from coding_agent_harness.config import ValidatorConfig
from coding_agent_harness.validation import (
    ValidationPipeline,
    ValidationStage,
    observation_from_validation,
)


@pytest.mark.parametrize("stage", list(ValidationStage))
def test_pipeline_runs_validators_for_each_stage(stage: ValidationStage, tmp_path: Path, fake_runner) -> None:
    fake_runner.queue(exit_code=0, stdout="ok")
    pipeline = ValidationPipeline.default(fake_runner)
    results = pipeline.run(stage, tmp_path)
    assert results[0].stage == stage.value
    assert results[0].status == "passed"


def test_failed_final_validator_closes_success_gate(tmp_path: Path, fake_runner) -> None:
    fake_runner.queue(exit_code=1, stderr="1 failed")
    pipeline = ValidationPipeline.default(fake_runner)
    results = pipeline.run(ValidationStage.FINAL, tmp_path)
    assert pipeline.success_gate_open(results) is False
    assert results[0].status == "failed"
    assert results[0].summary == "pytest failed"


def test_validation_classifies_failures_and_timeout(tmp_path: Path, fake_runner) -> None:
    fake_runner.queue(exit_code=1, stderr="ruff failed")
    fake_runner.queue(exit_code=1, stderr="mypy failed")
    fake_runner.queue(exit_code=None, timed_out=True, stderr="hung")
    pipeline = ValidationPipeline(
        fake_runner,
        (
            ValidatorConfig(validator_id="ruff", args=("-m", "ruff", "check")),
            ValidatorConfig(validator_id="mypy", args=("-m", "mypy")),
            ValidatorConfig(validator_id="custom", args=("-m", "pytest")),
        ),
    )
    results = pipeline.run(ValidationStage.FAST, tmp_path)
    assert [item.status for item in results] == ["failed", "failed", "timeout"]
    assert observation_from_validation("a1", results).category == "lint_failure"


def test_validation_tool_error_and_success_gate_requires_all_results(tmp_path: Path, fake_runner) -> None:
    fake_runner.queue(exit_code=0)
    pipeline = ValidationPipeline.default(fake_runner)
    results = pipeline.run(ValidationStage.BASELINE, tmp_path)
    assert pipeline.success_gate_open(results) is True
    assert observation_from_validation("a1", results).category == "success"
    assert observation_from_validation("a1", []).category == "tool_error"


def test_optional_validator_failure_does_not_close_required_success_gate(
    tmp_path: Path, fake_runner
) -> None:
    fake_runner.queue(exit_code=0)
    fake_runner.queue(exit_code=1, stderr="optional lint failed")
    pipeline = ValidationPipeline(
        fake_runner,
        (
            ValidatorConfig(validator_id="pytest", args=("-m", "pytest")),
            ValidatorConfig(
                validator_id="ruff",
                args=("-m", "ruff", "check"),
                required=False,
            ),
        ),
    )

    results = pipeline.run(ValidationStage.FINAL, tmp_path)

    assert [result.status for result in results] == ["passed", "failed"]
    assert pipeline.success_gate_open(results) is True
