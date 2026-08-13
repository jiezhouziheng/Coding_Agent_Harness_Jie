from __future__ import annotations

from typing import Any

from coding_agent_harness.storage import MemoryRecord, StateStore, StorageError

ALLOWED_MEMORY_TYPES = frozenset({"project_convention", "validation_command", "confirmed_decision", "successful_fix"})


class MemoryError(RuntimeError):
    pass


class MemoryService:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def propose(self, project_id: str, session_id: str, memory_type: str, content: str, evidence_action_id: str | None, tags: tuple[str, ...]) -> MemoryRecord:
        if memory_type not in ALLOWED_MEMORY_TYPES:
            raise MemoryError("memory_type_not_allowed")
        try:
            return self.store.create_memory(project_id, session_id, memory_type, content, evidence_action_id, tags, "CANDIDATE")
        except StorageError as error:
            raise MemoryError("memory_propose_failed") from error

    def propose_verified_fix(self, project_id: str, session_id: str, content: str, evidence_action_id: str, tags: tuple[str, ...]) -> MemoryRecord:
        if not self.store.action_has_successful_validation(session_id, evidence_action_id):
            raise MemoryError("missing_validation_evidence")
        return self.store.create_memory(project_id, session_id, "successful_fix", content, evidence_action_id, tags, "ACTIVE")

    def approve(self, entry_id: str) -> MemoryRecord:
        return self.store.transition_memory(entry_id, {"CANDIDATE", "APPROVED"}, "ACTIVE")

    def reject(self, entry_id: str) -> MemoryRecord:
        return self.store.transition_memory(entry_id, {"CANDIDATE", "APPROVED"}, "REJECTED")

    def delete(self, entry_id: str) -> MemoryRecord:
        return self.store.transition_memory(entry_id, {"CANDIDATE", "APPROVED", "ACTIVE", "REJECTED"}, "DELETED")

    def search(self, project_id: str, *, keywords: tuple[str, ...], limit: int = 5) -> tuple[MemoryRecord, ...]:
        return self.store.search_active_memory(project_id, keywords, min(limit, 5))

    def propose_from_action(self, session_id: str, action: Any) -> MemoryRecord:
        try:
            project_id = self.store.get_session(session_id).project_id
        except StorageError as error:
            raise MemoryError("memory_session_not_found") from error
        return self.propose(project_id, session_id, action.memory_type, action.content, action.evidence_action_id, action.tags)
