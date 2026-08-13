from pathlib import Path

import pytest

from coding_agent_harness.file_tools import FileToolError


def test_replace_requires_exact_match_count(tools, session_id: str, workspace: Path) -> None:
    target = workspace / "existing.py"
    target.write_text("value = 1\nvalue = 1\n", encoding="utf-8")
    with pytest.raises(FileToolError, match="match_count_mismatch"):
        tools.replace(session_id, "existing.py", "value = 1", "value = 2", expected_matches=1)
    assert target.read_text(encoding="utf-8") == "value = 1\nvalue = 1\n"


def test_replace_records_backup_before_write(tools, session_id: str, store, workspace: Path) -> None:
    target = workspace / "existing.py"
    target.write_bytes(b"value = 1\r\n")
    tools.replace(session_id, "existing.py", "value = 1", "value = 2", 1)
    record = store.list_changes(session_id)[0]
    assert record.backup_ref is not None
    assert Path(record.backup_ref).read_bytes() == b"value = 1\r\n"
    assert target.read_bytes() == b"value = 2\r\n"


def test_read_is_utf8_and_line_bounded(tools, workspace: Path) -> None:
    (workspace / "unicode.txt").write_text("零\n一\n二\n三\n", encoding="utf-8")
    assert tools.read("unicode.txt", 2, 3) == "2: 一\n3: 二"


def test_read_rejects_large_files(tools, workspace: Path) -> None:
    (workspace / "large.txt").write_bytes(b"x" * 1_000_001)
    with pytest.raises(FileToolError, match="file_too_large"):
        tools.read("large.txt", 1, 1)


def test_list_files_is_sorted_limited_and_marks_truncation(tools, workspace: Path) -> None:
    (workspace / "z.py").write_text("z", encoding="utf-8")
    (workspace / "a.py").write_text("a", encoding="utf-8")
    listing = tools.list_files(".", "*.py", 2)
    assert listing.paths == ("a.py", "delete.py")
    assert listing.truncated is True


def test_create_and_delete_are_journaled(tools, session_id: str, store, workspace: Path) -> None:
    tools.create(session_id, "new.py", "new\n")
    assert (workspace / "new.py").read_text(encoding="utf-8") == "new\n"
    tools.delete(session_id, "delete.py")
    assert not (workspace / "delete.py").exists()
    assert [item.operation for item in store.list_changes(session_id)] == ["create", "delete"]


def test_backup_failure_is_fail_closed(tools, session_id: str, workspace: Path, monkeypatch) -> None:
    target = workspace / "existing.py"
    monkeypatch.setattr(tools.journal, "record_before_change", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("backup")))
    with pytest.raises(FileToolError, match="change_failed"):
        tools.replace(session_id, "existing.py", "before", "after", 1)
    assert target.read_text(encoding="utf-8") == "before\n"


def test_atomic_fsync_failure_is_fail_closed(tools, session_id: str, workspace: Path, monkeypatch) -> None:
    target = workspace / "existing.py"
    monkeypatch.setattr("coding_agent_harness.file_tools.os.fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync")))
    with pytest.raises(FileToolError, match="change_failed"):
        tools.replace(session_id, "existing.py", "before", "after", 1)
    assert target.read_text(encoding="utf-8") == "before\n"


def test_create_journals_before_creating_parent_directory(tools, session_id: str, workspace: Path, monkeypatch) -> None:
    def fail_record(*args, **kwargs):
        raise RuntimeError("journal unavailable")

    monkeypatch.setattr(tools.journal, "record_before_change", fail_record)
    with pytest.raises(FileToolError, match="change_failed"):
        tools.create(session_id, "nested/new.py", "x\n")
    assert not (workspace / "nested").exists()


def test_replace_restores_original_when_finish_persistence_fails(tools, session_id: str, workspace: Path, monkeypatch) -> None:
    monkeypatch.setattr(tools.journal, "finish_change", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db")))
    with pytest.raises(FileToolError, match="change_failed"):
        tools.replace(session_id, "existing.py", "before", "after", 1)
    assert (workspace / "existing.py").read_text(encoding="utf-8") == "before\n"
