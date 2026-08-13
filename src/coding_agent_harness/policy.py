"""Deterministic policy evaluation and the non-bypassable authorization gateway."""

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import timedelta
from fnmatch import fnmatchcase
from pathlib import Path, PureWindowsPath
from typing import Protocol, Self

from pydantic import Field, model_validator

from coding_agent_harness.config import BudgetConfig
from coding_agent_harness.models import (
    Action,
    CreateFileAction,
    Decision,
    DeleteFileAction,
    ListFilesAction,
    RunCommandAction,
    StrictModel,
    parse_action,
)
from coding_agent_harness.security import (
    SecurityViolation,
    WorkspaceGuard,
    action_fingerprint,
    normalize_action,
)


class PolicyGatewayError(RuntimeError):
    """Raised when authorization evidence cannot be durably established."""


class PolicyDecision(StrictModel):
    decision: Decision
    reason_code: str = Field(min_length=1)
    rule_source: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)


class PendingAction(StrictModel):
    action_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    action: Action
    fingerprint: str = Field(min_length=1)


class AuthorizationGrant(StrictModel):
    action_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    action: Action
    fingerprint: str = Field(min_length=1)
    policy_decision_id: str = Field(min_length=1)
    approval_id: str | None = None


class PolicyResolution(StrictModel):
    action_id: str = Field(min_length=1)
    action: Action
    fingerprint: str = Field(min_length=1)
    decision: Decision
    reason_code: str = Field(min_length=1)
    grant: AuthorizationGrant | None = None
    pending_action: PendingAction | None = None
    approval_id: str | None = None
    approval_ttl_seconds: int = Field(default=900, ge=1)

    @model_validator(mode="after")
    def validate_decision_shape(self) -> Self:
        if self.decision is Decision.ALLOW:
            if (
                self.grant is None
                or self.grant.approval_id is not None
                or self.pending_action is not None
                or self.approval_id is not None
            ):
                raise ValueError("invalid_allow_resolution")
        elif self.decision is Decision.REQUIRE_APPROVAL:
            if self.grant is not None or self.pending_action is None or self.approval_id is None:
                raise ValueError("invalid_approval_resolution")
        elif self.grant is not None or self.pending_action is not None or self.approval_id is not None:
            raise ValueError("invalid_deny_resolution")
        if self.grant is not None and (
            self.grant.action_id != self.action_id
            or self.grant.action != self.action
            or self.grant.fingerprint != self.fingerprint
        ):
            raise ValueError("grant_resolution_mismatch")
        if self.pending_action is not None and (
            self.pending_action.action_id != self.action_id
            or self.pending_action.action != self.action
            or self.pending_action.fingerprint != self.fingerprint
        ):
            raise ValueError("pending_resolution_mismatch")
        return self


_BUILTIN_COMMAND_PREFIXES = frozenset(
    {
        ("python", "-m", "pytest"),
        ("python", "-m", "ruff", "check"),
        ("python", "-m", "mypy"),
        ("python", "-m", "compileall"),
        ("python", "-m", "pip", "install"),
        ("git", "status"),
        ("git", "diff"),
        ("git", "add"),
        ("git", "commit"),
    }
)


@dataclass(frozen=True)
class PolicyContext:
    workspace: Path
    budgets: BudgetConfig
    command_prefixes: frozenset[tuple[str, ...]]

    def __post_init__(self) -> None:
        if not self.command_prefixes.issubset(_BUILTIN_COMMAND_PREFIXES):
            raise ValueError("command_prefix_not_builtin")

    @classmethod
    def for_workspace(
        cls,
        workspace: Path,
        *,
        budgets: BudgetConfig | None = None,
        command_prefixes: frozenset[tuple[str, ...]] | None = None,
    ) -> PolicyContext:
        selected = _BUILTIN_COMMAND_PREFIXES if command_prefixes is None else command_prefixes
        if not selected.issubset(_BUILTIN_COMMAND_PREFIXES):
            raise ValueError("command_prefix_not_builtin")
        return cls(
            workspace=WorkspaceGuard(workspace).root,
            budgets=budgets or BudgetConfig(),
            command_prefixes=selected,
        )


_PROTECTED_NAMES = frozenset(
    {
        "build.gradle",
        "build.gradle.kts",
        "cargo.lock",
        "cargo.toml",
        "composer.json",
        "composer.lock",
        "gemfile",
        "gemfile.lock",
        "go.mod",
        "go.sum",
        "harness.toml",
        "makefile",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pom.xml",
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "uv.lock",
        "yarn.lock",
        ".gitlab-ci.yml",
    }
)
_SHELL_WRAPPERS = frozenset(
    {"bash", "cmd", "cmd.exe", "pwsh", "pwsh.exe", "powershell", "powershell.exe", "sh", "zsh"}
)
_NETWORK_TOOLS = frozenset({"curl", "curl.exe", "wget", "wget.exe"})
_REMOTE_GIT = frozenset({"clone", "fetch", "pull", "push"})
_NETWORK_PATTERN = re.compile(r"(?i)(?:^[a-z][a-z0-9+.-]*://|^git\+|^git@|^[^/\\]+@[^:]+:)")
_SHELL_PATTERN = re.compile(r"[|&;<>\r\n\x00]|\$\(|`")
_PIP_NETWORK_OPTIONS = frozenset(
    {
        "-f",
        "-i",
        "--cert",
        "--client-cert",
        "--extra-index-url",
        "--find-links",
        "--index-url",
        "--proxy",
        "--trusted-host",
    }
)
_PIP_LOCAL_INPUT_OPTIONS = frozenset(
    {"-c", "-r", "-t", "--constraint", "--requirement", "--target"}
)
_SENSITIVE_GLOB_PROBES = (
    ".env",
    ".env.local",
    ".git",
    ".ssh",
    "id_ed25519",
    "id_rsa",
    "secret.key",
    "secret.pem",
)


class PolicyEngine:
    def evaluate(self, action: Action, context: PolicyContext) -> PolicyDecision:
        fingerprint = action_fingerprint(action)
        resolved_path: Path | None = None
        try:
            guard = WorkspaceGuard(context.workspace)
            path = getattr(action, "path", None)
            if isinstance(path, str):
                resolved_path = guard.resolve(
                    path, must_exist=not isinstance(action, CreateFileAction)
                )
        except SecurityViolation:
            return _decision(Decision.DENY, "security_boundary_violation", fingerprint)

        if isinstance(action, ListFilesAction) and _unsafe_glob(action.glob):
            return _decision(Decision.DENY, "unsafe_glob_pattern", fingerprint)
        if isinstance(action, DeleteFileAction) and (
            resolved_path is None or not resolved_path.is_file()
        ):
            return _decision(Decision.DENY, "delete_target_not_file", fingerprint)
        if isinstance(action, (CreateFileAction, DeleteFileAction)):
            return _decision(Decision.REQUIRE_APPROVAL, "file_lifecycle_change", fingerprint)
        if action.tool == "replace_in_file" and _is_protected_path(action.path):
            return _decision(Decision.REQUIRE_APPROVAL, "protected_file_change", fingerprint)
        if isinstance(action, RunCommandAction):
            return self._evaluate_command(action, context, fingerprint)
        return _decision(Decision.ALLOW, "low_risk_workspace_action", fingerprint)

    def _evaluate_command(
        self, action: RunCommandAction, context: PolicyContext, fingerprint: str
    ) -> PolicyDecision:
        program = action.program.casefold()
        args = action.args
        if program in _SHELL_WRAPPERS:
            return _decision(Decision.DENY, "shell_wrapper_denied", fingerprint)
        if any(_SHELL_PATTERN.search(argument) for argument in args):
            return _decision(Decision.DENY, "shell_syntax_denied", fingerprint)
        if program in _NETWORK_TOOLS:
            return _decision(Decision.DENY, "network_tool_denied", fingerprint)
        if program == "git" and args and args[0].casefold() in _REMOTE_GIT:
            return _decision(Decision.DENY, "remote_git_denied", fingerprint)
        if any(_NETWORK_PATTERN.search(argument) for argument in args):
            reason = "network_install_denied" if _is_pip_install(program, args) else "network_argument_denied"
            return _decision(Decision.DENY, reason, fingerprint)
        if _is_pip_install(program, args):
            if ("python", "-m", "pip", "install") not in context.command_prefixes:
                return _decision(Decision.DENY, "project_command_restricted", fingerprint)
            return self._evaluate_pip(action, context, fingerprint)
        if program == "git" and args and args[0].casefold() == "add":
            if ("git", "add") not in context.command_prefixes:
                return _decision(Decision.DENY, "project_command_restricted", fingerprint)
            return self._evaluate_git_add(action, context, fingerprint)
        if any(_unsafe_command_path(argument) for argument in args):
            return _decision(Decision.DENY, "command_path_denied", fingerprint)

        command = (program, *(argument.casefold() for argument in args))
        matched = _matching_prefix(command)
        if matched is None:
            return _decision(Decision.DENY, "command_not_allowed", fingerprint)
        if matched not in context.command_prefixes:
            return _decision(Decision.DENY, "project_command_restricted", fingerprint)
        if matched == ("python", "-m", "compileall"):
            return _decision(Decision.REQUIRE_APPROVAL, "command_writes_bytecode", fingerprint)
        if matched[:2] == ("git", "commit"):
            return _decision(Decision.REQUIRE_APPROVAL, "git_write_requires_approval", fingerprint)
        if not _allowed_command_arguments_are_safe(matched, args):
            return _decision(Decision.DENY, "command_side_effect_option_denied", fingerprint)
        return _decision(Decision.ALLOW, "command_allowlist", fingerprint)

    @staticmethod
    def _evaluate_pip(
        action: RunCommandAction, context: PolicyContext, fingerprint: str
    ) -> PolicyDecision:
        install_args = action.args[3:]
        if "--no-index" not in install_args:
            return _decision(Decision.DENY, "network_install_denied", fingerprint)
        targets: list[str] = []
        for argument in install_args:
            lowered = argument.casefold()
            if lowered == "--no-index":
                continue
            if _matches_option(lowered, _PIP_NETWORK_OPTIONS, compact_short=True):
                return _decision(Decision.DENY, "network_install_denied", fingerprint)
            if _matches_option(lowered, _PIP_LOCAL_INPUT_OPTIONS, compact_short=True):
                return _decision(Decision.DENY, "local_install_target_invalid", fingerprint)
            if argument.startswith("-"):
                return _decision(Decision.DENY, "pip_option_denied", fingerprint)
            if _unsafe_command_path(argument):
                return _decision(Decision.DENY, "local_install_target_invalid", fingerprint)
            targets.append(argument)
        if not targets:
            return _decision(Decision.DENY, "local_install_target_invalid", fingerprint)
        try:
            guard = WorkspaceGuard(context.workspace)
            cwd = guard.resolve(action.cwd, must_exist=True)
            for target in targets:
                candidate = Path(target)
                if candidate.is_absolute():
                    raise SecurityViolation("absolute local install target")
                relative = (Path(guard.relative(cwd)) / candidate).as_posix()
                guard.resolve(relative, must_exist=True)
        except SecurityViolation:
            return _decision(Decision.DENY, "local_install_target_invalid", fingerprint)
        return _decision(Decision.REQUIRE_APPROVAL, "local_dependency_install", fingerprint)

    @staticmethod
    def _evaluate_git_add(
        action: RunCommandAction, context: PolicyContext, fingerprint: str
    ) -> PolicyDecision:
        if any(_unsafe_command_path(argument) for argument in action.args[1:]):
            return _decision(Decision.DENY, "git_add_target_invalid", fingerprint)
        targets = tuple(argument for argument in action.args[1:] if not argument.startswith("-"))
        if not targets:
            return _decision(Decision.DENY, "git_add_target_required", fingerprint)
        try:
            guard = WorkspaceGuard(context.workspace)
            cwd = guard.resolve(action.cwd, must_exist=True)
            cwd_relative = Path(guard.relative(cwd))
            for target in targets:
                guard.resolve((cwd_relative / target).as_posix(), must_exist=True)
        except SecurityViolation:
            return _decision(Decision.DENY, "git_add_target_invalid", fingerprint)
        return _decision(Decision.REQUIRE_APPROVAL, "git_write_requires_approval", fingerprint)


class _Store(Protocol):
    def flush_audit(self, writer: object) -> int: ...
    def transaction(self) -> AbstractContextManager[None]: ...
    def record_action(self, session_id: str, step: int, action: Action, fingerprint: str) -> str: ...
    def record_policy_decision(
        self, action_id: str, *, decision: Decision, reason_code: str, rule_source: str
    ) -> str: ...
    def enqueue_audit(self, event: dict[str, object]) -> int: ...


class _ApprovalRequests(Protocol):
    def request_in_transaction(
        self,
        pending: PendingAction,
        workspace: Path,
        *,
        expires_in: timedelta,
    ) -> str: ...


class PolicyGateway:
    def __init__(
        self,
        engine: PolicyEngine,
        store: _Store,
        audit_writer: object,
        approval_service: _ApprovalRequests | None = None,
    ) -> None:
        self.engine = engine
        self.store = store
        self.audit_writer = audit_writer
        self.approval_service = approval_service

    def authorize(
        self, session_id: str, step: int, action: Action, workspace: Path
    ) -> PolicyResolution:
        try:
            self.store.flush_audit(self.audit_writer)
            normalized, decision = self._normalize_and_evaluate(action, workspace)
            fingerprint = decision.fingerprint
            approval_id: str | None = None
            pending: PendingAction | None = None
            with self.store.transaction():
                action_id = self.store.record_action(session_id, step, normalized, fingerprint)
                decision_id = self.store.record_policy_decision(
                    action_id,
                    decision=decision.decision,
                    reason_code=decision.reason_code,
                    rule_source=decision.rule_source,
                )
                self.store.enqueue_audit(
                    {
                        "event": "policy_decision",
                        "session_id": session_id,
                        "action_id": action_id,
                        "decision": decision.decision.value,
                        "reason_code": decision.reason_code,
                    }
                )
                if decision.decision is Decision.REQUIRE_APPROVAL:
                    if self.approval_service is None:
                        raise PolicyGatewayError("policy_gateway_failed")
                    pending = PendingAction(
                        action_id=action_id,
                        session_id=session_id,
                        action=normalized,
                        fingerprint=fingerprint,
                    )
                    approval_id = self.approval_service.request_in_transaction(
                        pending, workspace, expires_in=timedelta(seconds=900)
                    )
            self.store.flush_audit(self.audit_writer)
            grant = None
            if decision.decision is Decision.ALLOW:
                grant = AuthorizationGrant(
                    action_id=action_id,
                    session_id=session_id,
                    action=normalized,
                    fingerprint=fingerprint,
                    policy_decision_id=decision_id,
                )
            return PolicyResolution(
                action_id=action_id,
                action=normalized,
                fingerprint=fingerprint,
                decision=decision.decision,
                reason_code=decision.reason_code,
                grant=grant,
                pending_action=pending,
                approval_id=approval_id,
            )
        except PolicyGatewayError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise PolicyGatewayError("policy_gateway_failed") from None

    def _normalize_and_evaluate(
        self, action: Action, workspace: Path
    ) -> tuple[Action, PolicyDecision]:
        try:
            normalized = normalize_action(action, WorkspaceGuard(workspace))
            decision = self.engine.evaluate(normalized, PolicyContext.for_workspace(workspace))
        except SecurityViolation:
            fingerprint = action_fingerprint(action)
            return _rejected_action(action), _decision(
                Decision.DENY, "security_boundary_violation", fingerprint
            )
        if decision.decision is Decision.DENY:
            return _rejected_action(normalized), decision
        return normalized, decision


def _matching_prefix(command: tuple[str, ...]) -> tuple[str, ...] | None:
    matches = tuple(
        prefix for prefix in _BUILTIN_COMMAND_PREFIXES if command[: len(prefix)] == prefix
    )
    return max(matches, key=len) if matches else None


def _is_pip_install(program: str, args: tuple[str, ...]) -> bool:
    return program == "python" and tuple(argument.casefold() for argument in args[:3]) == (
        "-m",
        "pip",
        "install",
    )


def _matches_option(
    argument: str, options: frozenset[str], *, compact_short: bool = False
) -> bool:
    for option in options:
        if argument == option or argument.startswith(f"{option}="):
            return True
        if (
            compact_short
            and option.startswith("-")
            and not option.startswith("--")
            and argument.startswith(option)
            and len(argument) > len(option)
        ):
            return True
    return False


_PYTEST_SAFE_FLAGS = frozenset(
    {
        "--collect-only",
        "--disable-warnings",
        "--exitfirst",
        "--fixtures",
        "--fixtures-per-test",
        "--full-trace",
        "--last-failed",
        "--failed-first",
        "--new-first",
        "--no-header",
        "--no-summary",
        "--quiet",
        "--setup-only",
        "--setup-plan",
        "--setup-show",
        "--showlocals",
        "--strict-config",
        "--strict-markers",
        "--verbose",
    }
)
_PYTEST_SAFE_VALUES = frozenset(
    {
        "-k",
        "-m",
        "--capture",
        "--color",
        "--confcutdir",
        "--deselect",
        "--durations",
        "--durations-min",
        "--ignore",
        "--ignore-glob",
        "--maxfail",
        "--rootdir",
        "--tb",
    }
)
_RUFF_SAFE_FLAGS = frozenset(
    {
        "--exit-zero",
        "--force-exclude",
        "--no-cache",
        "--no-force-exclude",
        "--no-preview",
        "--no-respect-gitignore",
        "--preview",
        "--quiet",
        "--respect-gitignore",
        "--show-fixes",
        "--silent",
        "--statistics",
        "--verbose",
    }
)
_RUFF_SAFE_VALUES = frozenset(
    {
        "--exclude",
        "--extend-exclude",
        "--extend-ignore",
        "--extend-per-file-ignores",
        "--extend-select",
        "--ignore",
        "--line-length",
        "--output-format",
        "--per-file-ignores",
        "--select",
        "--target-version",
    }
)
_MYPY_SAFE_FLAGS = frozenset(
    {
        "--check-untyped-defs",
        "--disallow-any-generics",
        "--disallow-incomplete-defs",
        "--disallow-untyped-defs",
        "--explicit-package-bases",
        "--ignore-missing-imports",
        "--namespace-packages",
        "--no-error-summary",
        "--no-incremental",
        "--pretty",
        "--show-error-codes",
        "--strict",
        "--warn-redundant-casts",
        "--warn-unreachable",
        "--warn-unused-ignores",
    }
)
_MYPY_SAFE_VALUES = frozenset(
    {"-m", "-p", "--exclude", "--follow-imports", "--module", "--package", "--platform", "--python-version"}
)
_GIT_STATUS_SAFE_FLAGS = frozenset(
    {
        "--ahead-behind",
        "--branch",
        "--ignored",
        "--long",
        "--no-ahead-behind",
        "--no-renames",
        "--porcelain",
        "--short",
        "--show-stash",
    }
)
_GIT_STATUS_SAFE_VALUES = frozenset(
    {"--column", "--find-renames", "--ignored", "--porcelain", "--untracked-files"}
)
_GIT_DIFF_SAFE_FLAGS = frozenset(
    {
        "--binary",
        "--cached",
        "--check",
        "--exit-code",
        "--name-only",
        "--name-status",
        "--no-color",
        "--no-ext-diff",
        "--no-patch",
        "--no-textconv",
        "--patch",
        "--quiet",
        "--staged",
        "--stat",
    }
)
_GIT_DIFF_SAFE_VALUES = frozenset(
    {"--color", "--diff-filter", "--stat", "--submodule", "--unified", "--word-diff", "--word-diff-regex"}
)


def _allowed_command_arguments_are_safe(
    prefix: tuple[str, ...], args: tuple[str, ...]
) -> bool:
    tail = tuple(argument.casefold() for argument in args[len(prefix) - 1 :])
    if prefix == ("python", "-m", "ruff", "check"):
        return _options_are_safe(
            tail,
            flags=_RUFF_SAFE_FLAGS,
            values=_RUFF_SAFE_VALUES,
            short_flag_pattern=r"-[qvs]+",
        )
    if prefix == ("python", "-m", "mypy"):
        return _options_are_safe(
            tail,
            flags=_MYPY_SAFE_FLAGS,
            values=_MYPY_SAFE_VALUES,
            short_flag_pattern=r"-v+",
        )
    if prefix == ("git", "diff"):
        return _options_are_safe(
            tail,
            flags=_GIT_DIFF_SAFE_FLAGS,
            values=_GIT_DIFF_SAFE_VALUES,
            short_flag_pattern=r"-[ps]+",
        )
    if prefix == ("python", "-m", "pytest"):
        return _options_are_safe(
            tail,
            flags=_PYTEST_SAFE_FLAGS,
            values=_PYTEST_SAFE_VALUES,
            short_flag_pattern=r"-[qvxs]+",
        )
    if prefix == ("git", "status"):
        return _options_are_safe(
            tail,
            flags=_GIT_STATUS_SAFE_FLAGS,
            values=_GIT_STATUS_SAFE_VALUES,
        )
    return False


def _options_are_safe(
    arguments: tuple[str, ...],
    *,
    flags: frozenset[str],
    values: frozenset[str],
    short_flag_pattern: str | None = None,
) -> bool:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument.startswith("@"):
            return False
        if argument == "--":
            return True
        if not argument.startswith("-") or argument == "-":
            index += 1
            continue
        if argument in flags or (
            short_flag_pattern is not None
            and re.fullmatch(short_flag_pattern, argument) is not None
        ):
            index += 1
            continue
        matched_value = next(
            (
                option
                for option in values
                if argument == option or argument.startswith(f"{option}=")
            ),
            None,
        )
        if matched_value is None:
            return False
        if argument == matched_value:
            index += 1
            if index >= len(arguments):
                return False
        index += 1
    return True


def _is_protected_path(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return (
        Path(normalized).name in _PROTECTED_NAMES
        or normalized.startswith(".github/workflows/")
    )


def _unsafe_command_path(argument: str) -> bool:
    if not argument:
        return False
    candidate = argument
    if argument.startswith("-"):
        if "=" not in argument:
            return False
        candidate = argument.split("=", 1)[1]
    windows = PureWindowsPath(candidate)
    native = Path(candidate)
    if windows.drive or windows.root or native.is_absolute():
        return True
    parts = tuple(part for part in re.split(r"[\\/]", candidate) if part not in ("", "."))
    return any(
        part == ".." or WorkspaceGuard._is_sensitive_component(part) for part in parts
    )


def _unsafe_glob(pattern: str) -> bool:
    if not pattern or "\x00" in pattern:
        return True
    windows = PureWindowsPath(pattern)
    if windows.drive or windows.root or Path(pattern).is_absolute():
        return True
    parts = tuple(part for part in re.split(r"[\\/]", pattern) if part not in ("", "."))
    for part in parts:
        lowered = part.casefold()
        if part == ".." or ":" in part:
            return True
        if lowered in {"*", "**"}:
            continue
        if any(fnmatchcase(probe, lowered) for probe in _SENSITIVE_GLOB_PROBES):
            return True
    return False


def _decision(decision: Decision, reason: str, fingerprint: str) -> PolicyDecision:
    return PolicyDecision(
        decision=decision,
        reason_code=reason,
        rule_source="builtin",
        fingerprint=fingerprint,
    )


def _rejected_action(action: Action) -> Action:
    payload = action.model_dump(mode="json")
    if "path" in payload:
        payload["path"] = "<REJECTED_PATH>"
    if "cwd" in payload:
        payload["cwd"] = "<REJECTED_CWD>"
    if "args" in payload:
        payload["args"] = []
    if "content" in payload:
        payload["content"] = "<REJECTED_CONTENT>"
    if "old_text" in payload:
        payload["old_text"] = "<REJECTED_TEXT>"
    if "new_text" in payload:
        payload["new_text"] = "<REJECTED_TEXT>"
    return parse_action(payload)
