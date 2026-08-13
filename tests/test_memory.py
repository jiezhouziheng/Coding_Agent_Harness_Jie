import pytest

from coding_agent_harness.memory import MemoryError, MemoryService


def test_unapproved_subjective_memory_is_not_retrievable(store, session_id: str, workspace) -> None:
    project_id = store.upsert_project(workspace, "memory")
    service = MemoryService(store)
    entry = service.propose(project_id, session_id, "confirmed_decision", "Use UTC", None, ("time",))
    assert entry.status == "CANDIDATE"
    assert service.search(project_id, keywords=("UTC",), limit=5) == ()


def test_verified_successful_fix_activates_and_search_is_bounded(store, session_id: str, workspace, monkeypatch) -> None:
    project_id = store.upsert_project(workspace, "memory")
    service = MemoryService(store)
    monkeypatch.setattr(store, "action_has_successful_validation", lambda *_: True)
    from coding_agent_harness.models import ListFilesAction
    from coding_agent_harness.security import action_fingerprint
    evidence_id = store.record_action(session_id, 1, ListFilesAction(), action_fingerprint(ListFilesAction()))
    for index in range(7):
        service.propose_verified_fix(project_id, session_id, f"fix cache {index}", evidence_id, ("cache",))
    assert len(service.search(project_id, keywords=("cache",), limit=5)) == 5


def test_invalid_type_and_missing_evidence_fail_closed(store, session_id: str) -> None:
    service = MemoryService(store)
    with pytest.raises(MemoryError, match="memory_type_not_allowed"):
        service.propose("p1", session_id, "arbitrary", "bad", None, ())
    with pytest.raises(MemoryError, match="missing_validation_evidence"):
        service.propose_verified_fix("p1", session_id, "fix", "missing", ())


def test_memory_project_must_match_session_project(store, session_id: str) -> None:
    service = MemoryService(store)
    with pytest.raises(MemoryError, match="memory_propose_failed"):
        service.propose("unrelated-project", session_id, "project_convention", "bad", None, ())


def test_search_filters_before_applying_limit(store, session_id: str, workspace) -> None:
    project_id = store.upsert_project(workspace, "memory")
    service = MemoryService(store)
    matching = service.propose(project_id, session_id, "project_convention", "cache fix", None, ("cache",))
    service.approve(matching.id)
    for index in range(6):
        entry = service.propose(project_id, session_id, "project_convention", f"other {index}", None, ())
        service.approve(entry.id)
    assert len(service.search(project_id, keywords=("cache",), limit=5)) == 1


def test_approve_reject_delete_lifecycle(store, session_id: str, workspace) -> None:
    project_id = store.upsert_project(workspace, "memory")
    service = MemoryService(store)
    entry = service.propose(project_id, session_id, "project_convention", "Use UTC", None, ())
    assert service.approve(entry.id).status == "ACTIVE"
    entry2 = service.propose(project_id, session_id, "project_convention", "Use UTC", None, ())
    assert service.reject(entry2.id).status == "REJECTED"
    assert service.delete(entry.id).status == "DELETED"


def test_action_memory_uses_session_project_and_context_query_is_bounded(store, session_id: str, workspace) -> None:
    project_id = store.upsert_project(workspace, "memory")
    service = MemoryService(store)
    from coding_agent_harness.models import ProposeMemoryAction

    entry = service.propose_from_action(
        session_id,
        ProposeMemoryAction(memory_type="project_convention", content="Use UTC"),
    )
    assert entry.project_id == project_id
    inputs = store.query_context_inputs(session_id)
    assert inputs["task"] == "file tools"
    assert inputs["memories"] == ()
