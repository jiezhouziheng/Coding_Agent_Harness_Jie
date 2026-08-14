from pathlib import Path

from coding_agent_harness.llm import ScriptedMockLLM
from coding_agent_harness.models import SessionStatus


def seed_failing_repository(root: Path) -> None:
    (root / "calc.py").write_text("def total():\n    return 1\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text(
        "from calc import total\n\ndef test_total():\n    assert total() == 2\n",
        encoding="utf-8",
    )


def test_scripted_mock_repairs_failing_python_repository(app_factory, tmp_path: Path) -> None:
    seed_failing_repository(tmp_path)
    llm = ScriptedMockLLM(
        [
            {"tool": "read_file", "path": "calc.py", "start_line": 1, "end_line": 20},
            {"tool": "replace_in_file", "path": "calc.py", "old_text": "return 1", "new_text": "return 3"},
            {"tool": "replace_in_file", "path": "calc.py", "old_text": "return 3", "new_text": "return 2"},
            {"tool": "finish", "summary": "fixed total"},
        ]
    )
    app = app_factory(workspace=tmp_path, llm=llm)
    result = app.run(task="fix failing tests")
    assert result.status is SessionStatus.SUCCEEDED
    assert "test_failure" in llm.contexts[2].model_dump_json()
    assert "return 2" in (tmp_path / "calc.py").read_text(encoding="utf-8")
