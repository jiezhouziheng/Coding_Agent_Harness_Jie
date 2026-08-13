"""Durable change records and precise, drift-aware rollback."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from coding_agent_harness.storage import ChangeRecord, StateStore, StorageError


def digest_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class JournalError(RuntimeError):
    pass


class ChangeJournal:
    def __init__(self, store: StateStore, backup_root: Path) -> None:
        self.store = store
        self.backup_root = backup_root.resolve()

    def record_before_change(
        self, session_id: str, relative_path: str, target: Path, operation: str
    ) -> ChangeRecord:
        try:
            before = target.read_bytes() if target.exists() else None
            backup_ref: str | None = None
            if before is not None:
                existing = self.store.list_changes(session_id)
                backup = self.backup_root / session_id / f"{len(existing):06d}.bin"
                backup.parent.mkdir(parents=True, exist_ok=True)
                _durable_write(backup, before)
                backup_ref = str(backup)
            return self.store.create_change(
                session_id=session_id, relative_path=relative_path, operation=operation,
                before_digest=digest_bytes(before) if before is not None else None,
                backup_ref=backup_ref,
            )
        except (OSError, StorageError) as error:
            raise JournalError("journal_record_failed") from error

    def finish_change(
        self,
        change_id: str,
        after: bytes | None = None,
        *,
        after_digest: str | None = None,
    ) -> ChangeRecord:
        try:
            return self.store.finish_change(
                change_id, after_digest=after_digest or digest_bytes(after or b"")
            )
        except StorageError as error:
            raise JournalError("journal_finish_failed") from error

    def rollback(self, session_id: str, workspace: Path) -> None:
        from coding_agent_harness.file_tools import FileToolError, _atomic_write
        from coding_agent_harness.security import WorkspaceGuard

        guard = WorkspaceGuard(workspace)
        changes = sorted(self.store.list_changes(session_id), key=lambda item: item.sequence, reverse=True)
        try:
            targets: list[tuple[ChangeRecord, Path]] = []
            for record in changes:
                target = guard.resolve(record.relative_path)
                current = target.read_bytes() if target.exists() and target.is_file() else None
                matches_after = (record.after_digest == "missing" and current is None) or (
                    current is not None and record.after_digest is not None and digest_bytes(current) == record.after_digest
                )
                if not matches_after:
                    raise FileToolError("workspace_drift")
                targets.append((record, target))
            for record, target in targets:
                if record.operation == "create":
                    target.unlink()
                else:
                    if not record.backup_ref:
                        raise FileToolError("backup_missing")
                    backup = Path(record.backup_ref)
                    content = backup.read_bytes()
                    if record.before_digest and digest_bytes(content) != record.before_digest:
                        raise FileToolError("backup_corrupt")
                    _atomic_write(target, content)
        except FileToolError:
            raise
        except (OSError, StorageError) as error:
            raise FileToolError("rollback_failed") from error


def _durable_write(target: Path, content: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
