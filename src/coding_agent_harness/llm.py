from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from coding_agent_harness.context import ModelContext
from coding_agent_harness.models import (
    Action,
    CreateFileAction,
    DeleteFileAction,
    FinishAction,
    ListFilesAction,
    ProposeMemoryAction,
    ReadFileAction,
    ReplaceInFileAction,
    RunCommandAction,
    parse_action,
)
from coding_agent_harness.security import redact_text


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def next_action(self, context: ModelContext) -> Action: ...


def tool_schemas() -> list[dict[str, Any]]:
    types = (ListFilesAction, ReadFileAction, ReplaceInFileAction, CreateFileAction, DeleteFileAction, RunCommandAction, ProposeMemoryAction, FinishAction)
    result: list[dict[str, Any]] = []
    for action_type in types:
        schema = action_type.model_json_schema()
        properties = dict(schema.get("properties", {}))
        properties.pop("tool", None)
        required = [field for field in schema.get("required", []) if field != "tool"]
        name = action_type.model_fields["tool"].default
        result.append({"type": "function", "function": {"name": name, "description": action_type.__name__, "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}})
    return result


class ScriptedMockLLM:
    def __init__(self, actions: Sequence[dict[str, object]]) -> None:
        self.actions = list(actions)
        self.contexts: list[ModelContext] = []

    def next_action(self, context: ModelContext) -> Action:
        self.contexts.append(context)
        if not self.actions:
            raise LLMError("script_exhausted")
        return parse_action(self.actions.pop(0))


class OpenAICompatibleClient:
    def __init__(self, base_url: str, model: str, api_key: str, http_client: httpx.Client | None = None, sleep: Any = time.sleep) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.http = http_client or httpx.Client(timeout=30)
        self.sleep = sleep

    def next_action(self, context: ModelContext) -> Action:
        payload = {"model": self.model, "messages": [{"role": "user", "content": context.model_dump_json()}], "tools": tool_schemas(), "tool_choice": "required"}
        for attempt in range(3):
            try:
                response = self.http.post(f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}"}, json=payload)
                if response.status_code in {401, 403} or 400 <= response.status_code < 500:
                    raise LLMError(f"http_{response.status_code}")
                if response.status_code >= 500:
                    if attempt == 2:
                        raise LLMError("provider_unavailable")
                    self.sleep(0)
                    continue
                call = response.json()["choices"][0]["message"]["tool_calls"][0]["function"]
                return parse_action({"tool": call["name"], **json.loads(call["arguments"])})
            except LLMError:
                raise
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
                if attempt < 2 and isinstance(error, (httpx.TimeoutException, httpx.ConnectError)):
                    self.sleep(0)
                    continue
                raise LLMError(redact_text(str(error))) from None
        raise LLMError("provider_unavailable")
