import pytest

from coding_agent_harness.context import ContextBuilder


def test_context_keeps_task_policy_and_failure_under_pressure() -> None:
    context = ContextBuilder(max_bytes=1_200).build(
        task="fix failing test", completion_criteria="all required validators pass",
        policy_summary="never read .env", current_failure="expected 4 got 3",
        source_snippets=("x" * 2_000,), observations=("old observation" * 200,), memories=("old memory" * 200,),
    )
    serialized = context.model_dump_json()
    assert "fix failing test" in serialized
    assert "never read .env" in serialized
    assert "expected 4 got 3" in serialized
    assert len(serialized.encode()) <= 1_200


def test_required_context_over_budget_fails_closed() -> None:
    with pytest.raises(ValueError, match="required_context_exceeds_budget"):
        ContextBuilder(max_bytes=10).build(task="task", completion_criteria="criteria", policy_summary="policy")
