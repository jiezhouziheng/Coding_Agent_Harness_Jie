"""Workspace-bound path handling and secret-safe diagnostic helpers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PureWindowsPath
from typing import cast

from coding_agent_harness.models import Action, RunCommandAction, parse_action

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
