"""Workspace-bound UTF-8 file operations with journal-before-mutation semantics."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from coding_agent_harness.journal import ChangeJournal, JournalError
from coding_agent_harness.security import SecurityViolation, WorkspaceGuard
from coding_agent_harness.storage import ChangeRecord, StorageError


class FileToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class FileList:
    paths: tuple[str, ...]
    truncated: bool


def _atomic_write(target: Path, content: bytes) -> None:
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


class FileTools:
    def __init__(self, workspace: Path, journal: ChangeJournal) -> None:
        self.guard = WorkspaceGuard(workspace)
        self.journal = journal

    def read(self, path: str, start_line: int, end_line: int) -> str:
        try:
            target = self.guard.resolve(path, must_exist=True)
            if not target.is_file() or target.stat().st_size > 1_000_000:
                raise FileToolError("file_too_large" if target.is_file() else "not_a_file")
            lines = target.read_text(encoding="utf-8").splitlines()
            return "\n".join(f"{number}: {line}" for number, line in enumerate(lines[start_line - 1:end_line], start=start_line))
        except FileToolError:
            raise
        except (OSError, UnicodeError, SecurityViolation) as error:
            raise FileToolError("read_failed") from error

    def list_files(self, path: str = ".", glob: str = "**/*", limit: int = 100) -> FileList:
        try:
            root = self.guard.resolve(path, must_exist=True)
            candidates = [item for item in root.glob(glob) if item.is_file()]
            paths = sorted((self.guard.relative(item) for item in candidates), key=str.casefold)
            return FileList(tuple(paths[:limit]), len(paths) > limit)
        except (OSError, SecurityViolation) as error:
            raise FileToolError("list_failed") from error

    def replace(
        self, session_id: str, path: str, old: str, new: str, expected_matches: int
    ) -> ChangeRecord:
        try:
            target = self.guard.resolve(path, must_exist=True)
            raw = target.read_bytes()
            text = raw.decode("utf-8")
            if text.count(old) != expected_matches:
                raise FileToolError("match_count_mismatch")
            record = self.journal.record_before_change(session_id, self.guard.relative(target), target, "modify")
            updated = text.replace(old, new, expected_matches).encode("utf-8")
            _atomic_write(target, updated)
            try:
                return self.journal.finish_change(record.id, updated)
            except Exception as error:
                try:
                    _atomic_write(target, raw)
                except Exception as restore_error:
                    raise FileToolError("change_failed") from restore_error
                raise FileToolError("change_failed") from error
        except FileToolError:
            raise
        except Exception as error:
            if isinstance(error, FileToolError):
                raise
            raise FileToolError("change_failed") from error

    def create(self, session_id: str, path: str, content: str) -> ChangeRecord:
        try:
            target = self.guard.resolve(path)
            if target.exists():
                raise FileToolError("target_exists")
            record = self.journal.record_before_change(session_id, self.guard.relative(target), target, "create")
            data = content.encode("utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, data)
            self.journal.finish_change(record.id, data)
            return record
        except FileToolError:
            raise
        except Exception as error:
            raise FileToolError("change_failed") from error

    def delete(self, session_id: str, path: str) -> ChangeRecord:
        try:
            target = self.guard.resolve(path, must_exist=True)
            if not target.is_file():
                raise FileToolError("not_a_file")
            record = self.journal.record_before_change(session_id, self.guard.relative(target), target, "delete")
            target.unlink()
            self.journal.finish_change(record.id, after_digest="missing")
            return record
        except FileToolError:
            raise
        except (OSError, SecurityViolation, JournalError, StorageError) as error:
            raise FileToolError("change_failed") from error
