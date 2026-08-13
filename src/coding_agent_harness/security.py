"""Workspace-bound path handling and secret-safe diagnostic helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PureWindowsPath
from typing import cast

from coding_agent_harness.models import (
    Action,
    CreateFileAction,
    RunCommandAction,
    parse_action,
)

SENSITIVE_PATTERNS = frozenset({".env", ".git", ".ssh", ".pem", ".key", "id_rsa", "id_ed25519"})
_SENSITIVE_ENV_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


class SecurityViolation(ValueError):
    """Raised when untrusted input would escape workspace security boundaries."""


class WorkspaceGuard:
    def __init__(self, root: Path) -> None:
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as error:
            raise SecurityViolation("workspace root must exist") from error
        if not resolved_root.is_dir():
            raise SecurityViolation("workspace root must be a directory")
        self.root = resolved_root

    def resolve(self, raw: str, must_exist: bool = False) -> Path:
        relative = self._validate_raw_path(raw)
        candidate = self.root / relative
        self._validate_symlink_components(relative)
        if must_exist and not candidate.exists():
            raise SecurityViolation("workspace path must exist")

        if candidate.exists():
            resolved = self._resolve_strict(candidate)
            self._ensure_contained(resolved)
            return resolved

        ancestor = candidate
        while not ancestor.exists():
            parent = ancestor.parent
            if parent == ancestor:
                raise SecurityViolation("workspace path has no resolvable ancestor")
            ancestor = parent
        self._ensure_contained(self._resolve_strict(ancestor))
        self._ensure_contained(candidate)
        return candidate

    def _validate_symlink_components(self, relative: Path) -> None:
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                resolved = self._resolve_strict(current)
                self._ensure_contained(resolved)
            elif not current.exists():
                break

    @staticmethod
    def _resolve_strict(path: Path) -> Path:
        try:
            return path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise SecurityViolation("workspace path cannot be resolved") from error

    def relative(self, path: Path) -> str:
        try:
            resolved = path.resolve(strict=False)
        except OSError as error:
            raise SecurityViolation("unable to resolve workspace path") from error
        self._ensure_contained(resolved)
        return resolved.relative_to(self.root).as_posix() or "."

    def _validate_raw_path(self, raw: str) -> Path:
        if not raw or "\x00" in raw:
            raise SecurityViolation("workspace path is empty or contains NUL")
        windows_path = PureWindowsPath(raw)
        native_path = Path(raw)
        if windows_path.drive or windows_path.root or native_path.is_absolute():
            raise SecurityViolation("workspace path must be relative")
        parts = tuple(part for part in re.split(r"[\\/]", raw) if part not in ("", "."))
        if any(part == ".." for part in parts):
            raise SecurityViolation("workspace path cannot contain parent traversal")
        if any(":" in part for part in parts):
            raise SecurityViolation("workspace path cannot contain colon components")
        if any(self._is_sensitive_component(part) for part in parts):
            raise SecurityViolation("workspace path targets sensitive material")
        return Path(*parts) if parts else Path(".")

    @staticmethod
    def _is_sensitive_component(component: str) -> bool:
        normalized = component.casefold()
        return (
            normalized == ".env"
            or normalized.startswith(".env.")
            or normalized in {".git", ".ssh", "id_rsa", "id_ed25519"}
            or normalized.endswith((".pem", ".key"))
        )

    def _ensure_contained(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise SecurityViolation("workspace path escapes root") from error


def normalize_action(action: Action, guard: WorkspaceGuard) -> Action:
    payload = action.model_dump(mode="json")
    if "path" in payload:
        path = guard.resolve(cast(str, payload["path"]), must_exist=action.tool != "create_file")
        payload["path"] = guard.relative(path)
    if isinstance(action, RunCommandAction):
        payload["program"] = action.program.casefold()
        cwd = guard.resolve(action.cwd, must_exist=True)
        if not cwd.is_dir():
            raise SecurityViolation("command cwd must be a directory")
        payload["cwd"] = guard.relative(cwd)
    return parse_action(payload)


def action_fingerprint(action: Action) -> str:
    serialized = json.dumps(
        action.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(serialized.encode("ascii")).hexdigest()


def workspace_fingerprint(action: Action, workspace: Path) -> str:
    """Bind an action to the visible workspace inputs it may mutate or consume."""
    guard = WorkspaceGuard(workspace)
    entries: list[dict[str, str]] = []
    path = getattr(action, "path", None)
    if isinstance(path, str):
        target = guard.resolve(path, must_exist=not isinstance(action, CreateFileAction))
        entries.append(_fingerprint_entry(target, guard, allow_missing=True))
        if target.exists() and target.is_dir():
            entries.extend(_visible_tree_snapshot(target, guard))
    elif isinstance(action, RunCommandAction):
        command = (action.program.casefold(), *(arg.casefold() for arg in action.args))
        if command[:2] == ("git", "commit"):
            entries.extend(_visible_workspace_snapshot(guard))
        elif command[:2] == ("git", "add"):
            entries.extend(_command_path_entries(action.args[1:], action.cwd, guard))
        elif command[:4] == ("python", "-m", "pip", "install"):
            targets = tuple(arg for arg in action.args[3:] if not arg.startswith("-"))
            entries.extend(_command_path_entries(targets, action.cwd, guard))
        elif command[:3] == ("python", "-m", "compileall"):
            entries.extend(_visible_workspace_snapshot(guard))
    payload = {"tool": action.tool, "entries": entries}
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("ascii")).hexdigest()


def _command_path_entries(
    raw_paths: tuple[str, ...], cwd: str, guard: WorkspaceGuard
) -> list[dict[str, str]]:
    cwd_path = guard.resolve(cwd, must_exist=True)
    cwd_relative = Path(guard.relative(cwd_path))
    entries: list[dict[str, str]] = []
    for raw in raw_paths:
        relative = (cwd_relative / raw).as_posix()
        target = guard.resolve(relative, must_exist=True)
        if target.is_dir():
            entries.extend(_visible_tree_snapshot(target, guard))
        else:
            entries.append(_fingerprint_entry(target, guard))
    return entries


def _visible_workspace_snapshot(guard: WorkspaceGuard) -> list[dict[str, str]]:
    return _visible_tree_snapshot(guard.root, guard)


def _visible_tree_snapshot(root: Path, guard: WorkspaceGuard) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    try:
        children = sorted(os.scandir(root), key=lambda item: item.name.casefold())
    except OSError as error:
        raise SecurityViolation("workspace snapshot unavailable") from error
    for child in children:
        if WorkspaceGuard._is_sensitive_component(child.name):
            continue
        child_path = Path(child.path)
        try:
            if child.is_symlink():
                entries.append(
                    {"path": guard.relative(child_path), "kind": "symlink", "digest": "not-followed"}
                )
            elif child.is_dir(follow_symlinks=False):
                entries.append(_fingerprint_entry(child_path, guard))
                entries.extend(_visible_tree_snapshot(child_path, guard))
            elif child.is_file(follow_symlinks=False):
                entries.append(_fingerprint_entry(child_path, guard))
        except OSError as error:
            raise SecurityViolation("workspace snapshot unavailable") from error
    return entries


def _fingerprint_entry(
    target: Path, guard: WorkspaceGuard, *, allow_missing: bool = False
) -> dict[str, str]:
    relative = guard.relative(target)
    if not target.exists():
        if allow_missing:
            return {"path": relative, "kind": "missing", "digest": "missing"}
        raise SecurityViolation("workspace fingerprint target missing")
    if target.is_symlink():
        raise SecurityViolation("workspace fingerprint target is symbolic link")
    if target.is_dir():
        return {"path": relative, "kind": "directory", "digest": "directory"}
    if not target.is_file():
        raise SecurityViolation("workspace fingerprint target type denied")
    try:
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError as error:
        raise SecurityViolation("workspace fingerprint target unreadable") from error
    return {"path": relative, "kind": "file", "digest": digest}


def redact_text(text: str, workspace: Path | None = None, secrets: tuple[str, ...] = ()) -> str:
    redacted = text
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        redacted = redacted.replace(secret, "<REDACTED>")
    redacted = re.sub(r"(?i)\bBearer\s+[^\s]+", "Bearer <REDACTED>", redacted)
    redacted = re.sub(
        r"(?i)\b(api[_-]?key)\s*([=:])\s*[^\s,;]+",
        r"\1\2<REDACTED>",
        redacted,
    )
    if workspace is not None:
        root = str(workspace.resolve(strict=False))
        root_pattern = re.escape(root).replace(r"\\", r"[\\\\/]")
        redacted = re.sub(root_pattern, "<WORKSPACE>", redacted, flags=re.IGNORECASE)
        redacted = re.sub(
            r"<WORKSPACE>[^\s]*",
            lambda match: match.group(0).replace("\\", "/"),
            redacted,
        )
    return redacted


def scrub_environment(environment: dict[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in environment.items()
        if not any(part in name.upper() for part in _SENSITIVE_ENV_PARTS)
    }
