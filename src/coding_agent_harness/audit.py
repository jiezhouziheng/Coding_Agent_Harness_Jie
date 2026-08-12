"""Append-only, redacted JSONL audit writing."""

from __future__ import annotations

import json
import os
from pathlib import Path

from coding_agent_harness.security import redact_text


class AuditError(RuntimeError):
    """Raised when an audit event could not be durably appended."""


class AuditWriter:
    def __init__(
        self, path: Path, workspace: Path | None = None, secrets: tuple[str, ...] = ()
    ) -> None:
        self.path = path
        self.workspace = workspace
        self.secrets = secrets

    def append(self, event: dict[str, object]) -> None:
        descriptor: int | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = _redact(event, self.workspace, self.secrets)
            line = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            file = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            descriptor = None
            with file:
                file.write(line + "\n")
                file.flush()
                os.fsync(file.fileno())
        except Exception:  # noqa: BLE001 - every append failure has one public error.
            raise AuditError("audit_append_failed") from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _redact(value: object, workspace: Path | None, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        return redact_text(value, workspace=workspace, secrets=secrets)
    if isinstance(value, dict):
        result: dict[object, object] = {}
        reserved = set(value)
        key_number = 1
        for key, item in value.items():
            redacted_key = _redact(key, workspace, secrets)
            if redacted_key != key or redacted_key in result:
                while True:
                    candidate = f"<REDACTED_KEY_{key_number}>"
                    key_number += 1
                    if candidate not in result and candidate not in reserved:
                        redacted_key = candidate
                        break
            result[redacted_key] = _redact(item, workspace, secrets)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item, workspace, secrets) for item in value]
    return value
