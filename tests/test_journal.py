from pathlib import Path

import pytest

from coding_agent_harness.file_tools import FileToolError


def test_rollback_restores_only_session_changes(workspace: Path, journal, tools, session_id: str) -> None:
    untouched = workspace / "untouched.py"
    untouched.write_text("keep\n", encoding="utf-8")
    tools.replace(session_id, "existing.py", "before", "after", 1)
    tools.create(session_id, "new.py", "new\n")
    tools.delete(session_id, "delete.py")
    journal.rollback(session_id, workspace)
    assert (workspace / "existing.py").read_text(encoding="utf-8") == "before\n"
    assert not (workspace / "new.py").exists()
    assert (workspace / "delete.py").read_text(encoding="utf-8") == "delete me\n"
    assert untouched.read_text(encoding="utf-8") == "keep\n"


def test_rollback_refuses_external_drift(workspace: Path, journal, tools, session_id: str) -> None:
    tools.replace(session_id, "existing.py", "before", "after", 1)
    (workspace / "existing.py").write_text("external\n", encoding="utf-8")
    with pytest.raises(FileToolError, match="workspace_drift"):
        journal.rollback(session_id, workspace)


def test_rollback_create_only_deletes_matching_fingerprint(workspace: Path, journal, tools, session_id: str) -> None:
    tools.create(session_id, "new.py", "new\n")
    (workspace / "new.py").write_text("external\n", encoding="utf-8")
    with pytest.raises(FileToolError, match="workspace_drift"):
        journal.rollback(session_id, workspace)
    assert (workspace / "new.py").read_text(encoding="utf-8") == "external\n"


def test_rollback_uses_external_backup_for_delete(workspace: Path, journal, tools, session_id: str, app_data: Path) -> None:
    tools.delete(session_id, "delete.py")
    backup_root = app_data / "backups"
    assert not (workspace / "delete.py").exists()
    assert any(backup_root.rglob("*.bin"))
    journal.rollback(session_id, workspace)
    assert (workspace / "delete.py").read_text(encoding="utf-8") == "delete me\n"
