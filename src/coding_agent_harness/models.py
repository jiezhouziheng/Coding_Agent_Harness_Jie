from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ListFilesAction(StrictModel):
    tool: Literal["list_files"] = "list_files"
    path: str = "."
    glob: str = "**/*"
    limit: int = Field(default=100, ge=1, le=500)


class ReadFileAction(StrictModel):
    tool: Literal["read_file"] = "read_file"
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1)

    @model_validator(mode="after")
    def validate_line_range(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ReplaceInFileAction(StrictModel):
    tool: Literal["replace_in_file"] = "replace_in_file"
    path: str
    old_text: str = Field(min_length=1)
    new_text: str
    expected_matches: int = Field(default=1, ge=1, le=20)


class CreateFileAction(StrictModel):
    tool: Literal["create_file"] = "create_file"
    path: str
    content: str


class DeleteFileAction(StrictModel):
    tool: Literal["delete_file"] = "delete_file"
    path: str


class RunCommandAction(StrictModel):
    tool: Literal["run_command"] = "run_command"
    program: str
    args: tuple[str, ...] = ()
    cwd: str = "."
    timeout_seconds: int = Field(default=120, ge=1, le=300)

    @field_validator("args", mode="before")
    @classmethod
    def freeze_json_args(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value


class ProposeMemoryAction(StrictModel):
    tool: Literal["propose_memory"] = "propose_memory"
    memory_type: Literal[
        "project_convention",
        "validation_command",
        "confirmed_decision",
        "successful_fix",
    ]
    content: str = Field(min_length=1, max_length=2000)
    evidence_action_id: str | None = None
    tags: tuple[str, ...] = ()

    @field_validator("tags", mode="before")
    @classmethod
    def freeze_json_tags(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value


class FinishAction(StrictModel):
    tool: Literal["finish"] = "finish"
    summary: str = Field(min_length=1, max_length=2000)


Action = Annotated[
    ListFilesAction
    | ReadFileAction
    | ReplaceInFileAction
    | CreateFileAction
    | DeleteFileAction
    | RunCommandAction
    | ProposeMemoryAction
    | FinishAction,
    Field(discriminator="tool"),
]

ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)


def parse_action(payload: object) -> Action:
    return ACTION_ADAPTER.validate_python(payload)


class Decision(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class ApprovalStatus(StrEnum):
    PROPOSED = "PROPOSED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CONSUMED = "CONSUMED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class SessionStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PAUSED_APPROVAL = "PAUSED_APPROVAL"
    PAUSED_LIMIT_REACHED = "PAUSED_LIMIT_REACHED"
    PAUSED_PROTOCOL_ERROR = "PAUSED_PROTOCOL_ERROR"
    PAUSED_WORKSPACE_DRIFT = "PAUSED_WORKSPACE_DRIFT"
    PAUSED_INTERNAL_ERROR = "PAUSED_INTERNAL_ERROR"
    NEEDS_USER_DECISION = "NEEDS_USER_DECISION"
    CHANGES_KEPT = "CHANGES_KEPT"
    ROLLED_BACK = "ROLLED_BACK"


class Observation(StrictModel):
    category: Literal[
        "test_failure",
        "lint_failure",
        "type_failure",
        "timeout",
        "tool_error",
        "policy_blocked",
        "approval_denied",
        "success",
    ]
    summary: str = Field(max_length=4000)
    evidence: str = Field(default="", max_length=50000)
    action_id: str | None = None


class ValidationResult(StrictModel):
    validator_id: str
    stage: Literal["baseline", "fast", "final"]
    status: Literal["passed", "failed", "error", "timeout"]
    exit_code: int | None
    duration_ms: int = Field(ge=0)
    summary: str = Field(max_length=4000)
    evidence: str = Field(default="", max_length=50000)


ALLOWED_TRANSITIONS: frozenset[tuple[SessionStatus, SessionStatus]] = frozenset(
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


def validate_transition(current: SessionStatus, target: SessionStatus) -> bool:
    if not isinstance(current, SessionStatus) or not isinstance(target, SessionStatus):
        return False
    return (current, target) in ALLOWED_TRANSITIONS
