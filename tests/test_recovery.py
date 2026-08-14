from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent_harness.models import SessionStatus
from coding_agent_harness.session_service import SessionService, WorkspaceBusy


class FakeJournal:
    def __init__(self) -> None:
        self.drifted = False

    def find_drift(self, session_id: str) -> bool:
        return self.drifted


class FakeApprovals:
    def __init__(self) -> None:
        self.invalidated: list[tuple[str, str]] = []

    def invalidate_for_session(self, session_id: str, *, reason: str) -> None:
        self.invalidated.append((session_id, reason))


class FakeLock:
    def __init__(self, path: Path, registry: set[str]) -> None:
        self.path = path
        self.registry = registry
        self.held = False

    def acquire(self) -> FakeLock:
        key = str(self.path)
        if key in self.registry:
            raise WorkspaceBusy("workspace_busy")
        self.registry.add(key)
        self.held = True
        return self

    def release(self) -> None:
        if self.held:
            self.registry.discard(str(self.path))
            self.held = False


class CountingEngineFactory:
    def __init__(self, store) -> None:
        self.store = store
        self.create_calls = 0

    def create(self, *, session_id: str):
        self.create_calls += 1
        return session_id, self

    def run(self, session_id: str):
        return self.store.get_session(session_id)


@pytest.fixture
def recovery_session(store, workspace: Path) -> str:
    project_id = store.upsert_project(workspace, "recovery")
    return store.create_session(project_id, "recover session")


def test_resume_reloads_pending_approval_after_restart(store, recovery_session: str) -> None:
    service = SessionService(store, FakeJournal(), FakeApprovals(), lambda _: None)
    store.transition_session(recovery_session, SessionStatus.RUNNING)
    store.transition_session(recovery_session, SessionStatus.PAUSED_APPROVAL)

    result = service.resume(recovery_session)

    assert result.status is SessionStatus.PAUSED_APPROVAL


def test_resume_invalidates_approval_on_workspace_drift(store, recovery_session: str) -> None:
    journal = FakeJournal()
    approvals = FakeApprovals()
    service = SessionService(store, journal, approvals, lambda _: None)
    store.transition_session(recovery_session, SessionStatus.RUNNING)
    journal.drifted = True

    result = service.resume(recovery_session)

    assert result.status is SessionStatus.PAUSED_WORKSPACE_DRIFT
    assert approvals.invalidated == [(recovery_session, "workspace_drift")]


def test_second_writer_for_same_workspace_is_rejected(workspace: Path, tmp_path: Path) -> None:
    registry: set[str] = set()
    factory = lambda path: FakeLock(path, registry)
    first_service = SessionService(None, None, None, factory)
    second_service = SessionService(None, None, None, factory)

    first = first_service.acquire_workspace(workspace, lock_root=tmp_path)
    with pytest.raises(WorkspaceBusy):
        second_service.acquire_workspace(workspace, lock_root=tmp_path)
    first.release()


@pytest.mark.parametrize(
    "status",
    [
        SessionStatus.PAUSED_LIMIT_REACHED,
        SessionStatus.PAUSED_PROTOCOL_ERROR,
        SessionStatus.PAUSED_WORKSPACE_DRIFT,
        SessionStatus.PAUSED_INTERNAL_ERROR,
        SessionStatus.SUCCEEDED,
        SessionStatus.NEEDS_USER_DECISION,
        SessionStatus.CHANGES_KEPT,
        SessionStatus.ROLLED_BACK,
    ],
)
def test_resume_and_run_returns_non_runnable_status_without_creating_engine(
    store, workspace: Path, recovery_session: str, tmp_path: Path, status: SessionStatus
) -> None:
    store.transition_session(recovery_session, SessionStatus.RUNNING)
    if status in {SessionStatus.CHANGES_KEPT, SessionStatus.ROLLED_BACK}:
        store.transition_session(recovery_session, SessionStatus.NEEDS_USER_DECISION)
    store.transition_session(recovery_session, status)
    engine_factory = CountingEngineFactory(store)
    service = SessionService(
        store,
        FakeJournal(),
        FakeApprovals(),
        None,
        lock_root=tmp_path / "locks",
    )
    service.engine_factory = engine_factory

    result = service.resume_and_run(recovery_session)

    assert result.status is status
    assert engine_factory.create_calls == 0
