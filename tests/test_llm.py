import httpx
import pytest

from coding_agent_harness.context import ModelContext
from coding_agent_harness.llm import LLMError, OpenAICompatibleClient, ScriptedMockLLM, tool_schemas


def context() -> ModelContext:
    return ModelContext(task="fix", completion_criteria="pass", policy_summary="safe")


def test_scripted_mock_records_context_before_returning_action() -> None:
    client = ScriptedMockLLM([{"tool": "read_file", "path": "calc.py"}])
    action = client.next_action(context())
    assert action.tool == "read_file"
    assert client.contexts == [context()]


def test_tool_schemas_remove_constant_tool_field() -> None:
    for schema in tool_schemas():
        assert "tool" not in schema["function"]["parameters"]["properties"]


def test_openai_client_parses_native_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-secret"
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"type": "function", "function": {"name": "finish", "arguments": '{"summary":"done"}'}}]}}]})

    client = OpenAICompatibleClient("https://provider.invalid/v1", "test", "test-secret", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.next_action(context()).tool == "finish"


def test_openai_client_retries_timeout_and_server_errors_with_bound() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("temporary")
        if calls == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"type": "function", "function": {"name": "finish", "arguments": '{"summary":"done"}'}}]}}]})

    client = OpenAICompatibleClient(
        "https://provider.invalid/v1", "test", "secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None,
    )
    assert client.next_action(context()).tool == "finish"
    assert calls == 3


def test_openai_client_does_not_retry_permanent_client_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="secret should not leak")

    client = OpenAICompatibleClient(
        "https://provider.invalid/v1", "test", "secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None,
    )
    with pytest.raises(LLMError, match="http_401") as error:
        client.next_action(context())
    assert calls == 1
    assert "secret" not in str(error.value)
