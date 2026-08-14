"""Control-plane services for durable session recovery and workspace ownership."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

from coding_agent_harness.models import SessionStatus


def default_app_data_dir() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "CodingAgentHarness"
    return Path.home() / ".local" / "share" / "CodingAgentHarness"


class WorkspaceBusy(RuntimeError):
    """Another harness process owns the workspace lock."""


class WorkspaceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None
        self._held = False

    def acquire(self) -> WorkspaceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._handle = self.path.open("x", encoding="utf-8")
            self._handle.write(json.dumps({"pid": os.getpid()}))
            self._handle.flush()
            self._held = True
            return self
        except FileExistsError:
            raise WorkspaceBusy("workspace_busy") from None
        except OSError:
            raise WorkspaceBusy("workspace_busy") from None

    def release(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None
        if self._held:
            try:
                self.path.unlink(missing_ok=True)
            finally:
                self._held = False

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class SessionService:
    def __init__(self, store: Any, journal: Any, approvals: Any, lock_factory: Callable[[Path], Any] | None, lock_root: Path | None = None) -> None:
        self.store = store
        self.journal = journal
        self.approvals = approvals
        self.lock_factory = lock_factory or WorkspaceLock
        self.lock_root = lock_root
        self.engine_factory: Any = None

    def resume(self, session_id: str) -> Any:
        session = self.store.get_session(session_id)
        try:
            project = self.store.get_project(session.project_id)
            drifted = bool(self.journal.find_drift_in(session_id, Path(project.canonical_path)))
        except AttributeError:
            drifted = bool(self.journal.find_drift(session_id))
        if drifted:
            self.approvals.invalidate_for_session(session_id, reason="workspace_drift")
            if session.status is not SessionStatus.PAUSED_WORKSPACE_DRIFT:
                self.store.transition_session(session_id, SessionStatus.PAUSED_WORKSPACE_DRIFT)
            return self.store.get_session(session_id)
        return session

    def acquire_workspace(self, workspace: Path, *, lock_root: Path | None = None) -> Any:
        canonical = workspace.resolve(strict=False)
        root = lock_root or self.lock_root or canonical / ".cah-locks"
        token = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:32]
        lock = self.lock_factory(root / f"{token}.lock")
        return lock.acquire()

    def list_safe(self) -> tuple[dict[str, object], ...]:
        rows = self.store.list_sessions()
        return tuple(_safe_session(row) for row in rows)

    def show_safe(self, session_id: str) -> dict[str, object]:
        return _safe_session(self.store.get_session(session_id))

    def resume_and_run(self, session_id: str) -> Any:
        session = self.resume(session_id)
        if session.status is SessionStatus.PAUSED_WORKSPACE_DRIFT:
            return session
        if self.engine_factory is None:
            return session
        _, engine = self.engine_factory.create(session_id=session_id)
        return engine.run(session_id)


def _safe_session(session: Any) -> dict[str, object]:
    return {
        "session_id": session.id,
        "project_id": session.project_id,
        "task": session.task,
        "status": session.status.value,
        "budget": session.budget,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }
