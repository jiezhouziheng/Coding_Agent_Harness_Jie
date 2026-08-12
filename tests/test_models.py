from typing import Any, cast

import pytest
from pydantic import ValidationError

from coding_agent_harness.models import (
    ALLOWED_TRANSITIONS,
    Decision,
    Observation,
    ReplaceInFileAction,
    SessionStatus,
    ValidationResult,
    parse_action,
    validate_transition,
)


def test_replace_in_file_action_has_exact_value_semantics() -> None:
    payload = {
        "tool": "replace_in_file",
        "path": "src/example.py",
        "old_text": "before",
        "new_text": "after",
        "expected_matches": 2,
    }

    assert parse_action(payload) == ReplaceInFileAction(**payload)


@pytest.mark.parametrize(
    ("payload", "model_name", "expected"),
    (
        (
            {"tool": "list_files", "path": "src", "glob": "*.py", "limit": 25},
            "ListFilesAction",
            {"tool": "list_files", "path": "src", "glob": "*.py", "limit": 25},
        ),
        (
            {
                "tool": "read_file",
                "path": "README.md",
                "start_line": 2,
                "end_line": 8,
            },
            "ReadFileAction",
            {
                "tool": "read_file",
                "path": "README.md",
                "start_line": 2,
                "end_line": 8,
            },
        ),
        (
            {
                "tool": "replace_in_file",
                "path": "src/example.py",
                "old_text": "before",
                "new_text": "after",
                "expected_matches": 1,
            },
            "ReplaceInFileAction",
            {
                "tool": "replace_in_file",
                "path": "src/example.py",
                "old_text": "before",
                "new_text": "after",
                "expected_matches": 1,
            },
        ),
        (
            {"tool": "create_file", "path": "notes.txt", "content": "hello"},
            "CreateFileAction",
            {"tool": "create_file", "path": "notes.txt", "content": "hello"},
        ),
        (
            {"tool": "delete_file", "path": "obsolete.txt"},
            "DeleteFileAction",
            {"tool": "delete_file", "path": "obsolete.txt"},
        ),
        (
            {
                "tool": "run_command",
                "program": "python",
                "args": ["-m", "pytest"],
                "cwd": ".",
                "timeout_seconds": 30,
            },
            "RunCommandAction",
            {
                "tool": "run_command",
                "program": "python",
                "args": ["-m", "pytest"],
                "cwd": ".",
                "timeout_seconds": 30,
            },
        ),
        (
            {
                "tool": "propose_memory",
                "memory_type": "project_convention",
                "content": "Run focused tests before the full suite.",
                "evidence_action_id": "action-7",
                "tags": ["testing", "workflow"],
            },
            "ProposeMemoryAction",
            {
                "tool": "propose_memory",
                "memory_type": "project_convention",
                "content": "Run focused tests before the full suite.",
                "evidence_action_id": "action-7",
                "tags": ["testing", "workflow"],
            },
        ),
        (
            {"tool": "finish", "summary": "Implementation complete."},
            "FinishAction",
            {"tool": "finish", "summary": "Implementation complete."},
        ),
    ),
)
def test_parse_action_accepts_each_supported_tool(
    payload: dict[str, object], model_name: str, expected: dict[str, object]
) -> None:
    action = parse_action(payload)

    assert type(action).__name__ == model_name
    assert action.model_dump(mode="json") == expected


def test_json_arrays_are_frozen_at_the_action_boundary() -> None:
    command = parse_action(
        {
            "tool": "run_command",
            "program": "python",
            "args": ["-m", "pytest"],
            "cwd": ".",
            "timeout_seconds": 30,
        }
    )
    memory = parse_action(
        {
            "tool": "propose_memory",
            "memory_type": "project_convention",
            "content": "The package uses Pydantic models.",
            "tags": ["models", "pydantic"],
        }
    )

    assert command.args == ("-m", "pytest")
    assert memory.tags == ("models", "pydantic")


def test_actions_are_frozen() -> None:
    action = ReplaceInFileAction(
        path="src/example.py",
        old_text="before",
        new_text="after",
        expected_matches=1,
    )

    with pytest.raises(ValidationError):
        action.path = "src/other.py"


@pytest.mark.parametrize(
    "payload",
    (
        {"tool": "unknown", "path": "."},
        {"tool": "delete_file", "path": "obsolete.txt", "force": True},
        {
            "tool": "read_file",
            "path": "README.md",
            "start_line": 10,
            "end_line": 2,
        },
        {"tool": "list_files", "limit": "10"},
        {"tool": "run_command", "program": "python", "args": [1]},
        {
            "tool": "propose_memory",
            "memory_type": "project_convention",
            "content": "content",
            "tags": "time",
        },
        {
            "tool": "propose_memory",
            "memory_type": "project_convention",
            "content": "content",
            "tags": [1],
        },
        {
            "tool": "replace_in_file",
            "path": "src/example.py",
            "old_text": "before",
            "new_text": "after",
            "expected_matches": 0,
        },
    ),
)
def test_parse_action_rejects_invalid_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        parse_action(payload)


def test_validate_transition_allows_only_explicit_edges() -> None:
    assert ALLOWED_TRANSITIONS == frozenset(
        {
            (SessionStatus.CREATED, SessionStatus.RUNNING),
            (SessionStatus.RUNNING, SessionStatus.SUCCEEDED),
            (SessionStatus.RUNNING, SessionStatus.PAUSED_APPROVAL),
            (SessionStatus.RUNNING, SessionStatus.PAUSED_LIMIT_REACHED),
            (SessionStatus.RUNNING, SessionStatus.PAUSED_PROTOCOL_ERROR),
            (SessionStatus.RUNNING, SessionStatus.PAUSED_WORKSPACE_DRIFT),
            (SessionStatus.RUNNING, SessionStatus.PAUSED_INTERNAL_ERROR),
            (SessionStatus.RUNNING, SessionStatus.NEEDS_USER_DECISION),
            (SessionStatus.PAUSED_APPROVAL, SessionStatus.RUNNING),
            (SessionStatus.PAUSED_LIMIT_REACHED, SessionStatus.RUNNING),
            (SessionStatus.NEEDS_USER_DECISION, SessionStatus.CHANGES_KEPT),
            (SessionStatus.NEEDS_USER_DECISION, SessionStatus.ROLLED_BACK),
        }
    )
    assert validate_transition(SessionStatus.RUNNING, SessionStatus.SUCCEEDED) is True
    assert validate_transition(SessionStatus.CREATED, SessionStatus.SUCCEEDED) is False


def test_validate_transition_rejects_wrong_types() -> None:
    current = cast(Any, "RUNNING")
    target = cast(Any, "SUCCEEDED")

    assert validate_transition(current, target) is False


@pytest.mark.parametrize(
    "payload",
    (
        {"category": "success", "summary": "x" * 4001},
        {"category": "unknown", "summary": "Invalid category."},
        {
            "category": "success",
            "summary": "Evidence exceeds the limit.",
            "evidence": "x" * 50001,
        },
    ),
)
def test_observation_rejects_invalid_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Observation(**payload)


def test_decision_has_exact_policy_values() -> None:
    assert [item.value for item in Decision] == [
        "ALLOW",
        "REQUIRE_APPROVAL",
        "DENY",
    ]


@pytest.mark.parametrize(
    "payload",
    (
        {
            "validator_id": "unit-tests",
            "stage": "fast",
            "status": "unknown",
            "exit_code": None,
            "duration_ms": 500,
            "summary": "Invalid status.",
        },
        {
            "validator_id": "unit-tests",
            "stage": "fast",
            "status": "passed",
            "exit_code": "1",
            "duration_ms": 500,
            "summary": "Invalid exit code.",
        },
        {
            "validator_id": "unit-tests",
            "stage": "fast",
            "status": "passed",
            "exit_code": None,
            "duration_ms": -1,
            "summary": "Invalid duration.",
        },
    ),
)
def test_validation_result_rejects_invalid_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ValidationResult(**payload)


def test_validation_result_accepts_valid_payload() -> None:
    result = ValidationResult(
        validator_id="unit-tests",
        stage="final",
        status="passed",
        exit_code=0,
        duration_ms=1250,
        summary="All tests passed.",
        evidence="22 passed",
    )

    assert result.status == "passed"
    assert result.exit_code == 0


def test_validation_result_requires_explicit_exit_code() -> None:
    with pytest.raises(ValidationError):
        ValidationResult(
            validator_id="unit-tests",
            stage="final",
            status="passed",
            duration_ms=1250,
            summary="All tests passed.",
        )


def test_shared_model_constraints_are_fail_closed() -> None:
    observation = Observation(
        category="tool_error",
        summary="The tool returned an error.",
        action_id="action-9",
    )

    with pytest.raises(ValidationError):
        observation.summary = "changed"

    with pytest.raises(ValidationError):
        ValidationResult(
            validator_id="unit-tests",
            stage="baseline",
            status="error",
            exit_code=None,
            duration_ms=0,
            summary="Collection failed.",
            unexpected=True,
        )
