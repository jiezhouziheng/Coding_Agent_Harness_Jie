from __future__ import annotations

from typing import Any

from coding_agent_harness.models import StrictModel
from coding_agent_harness.security import redact_text


class ModelContext(StrictModel):
    task: str
    completion_criteria: str
    policy_summary: str
    tools: tuple[dict[str, object], ...] = ()
    validator_summary: str = ""
    current_failure: str = ""
    source_snippets: tuple[str, ...] = ()
    recent_observations: tuple[str, ...] = ()
    memories: tuple[str, ...] = ()


class ContextBuilder:
    def __init__(self, max_bytes: int = 50_000) -> None:
        self.max_bytes = max_bytes

    def build(self, *, task: str, completion_criteria: str, policy_summary: str, tools: tuple[dict[str, object], ...] = (), validator_summary: str = "", current_failure: str = "", source_snippets: tuple[str, ...] = (), observations: tuple[str, ...] = (), memories: tuple[str, ...] = ()) -> ModelContext:
        fixed = ModelContext(task=task, completion_criteria=completion_criteria, policy_summary=policy_summary, tools=tools, validator_summary=validator_summary, current_failure=current_failure)
        if len(fixed.model_dump_json().encode()) > self.max_bytes:
            raise ValueError("required_context_exceeds_budget")
        remaining = self.max_bytes - len(fixed.model_dump_json().encode())

        def take(items: tuple[str, ...], budget: int) -> tuple[tuple[str, ...], int]:
            accepted: list[str] = []
            for item in items:
                safe = redact_text(item)[:4_000]
                size = len(safe.encode())
                if size > budget:
                    break
                accepted.append(safe)
                budget -= size
            return tuple(accepted), budget

        snippets, remaining = take(source_snippets, remaining)
        recent, remaining = take(observations, remaining)
        selected_memories, remaining = take(memories[:5], remaining)
        return fixed.model_copy(update={"source_snippets": snippets, "recent_observations": recent, "memories": selected_memories})

    def from_store(self, store: Any, session_id: str) -> ModelContext:
        return self.build(**store.query_context_inputs(session_id, memory_limit=5))
