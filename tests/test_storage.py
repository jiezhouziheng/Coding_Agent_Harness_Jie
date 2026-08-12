from pathlib import Path
from typing import Self

import pytest

from coding_agent_harness.audit import AuditError, AuditWriter
from coding_agent_harness.config import BudgetConfig
from coding_agent_harness.models import (
    CreateFileAction,
    Decision,
    Observation,
    SessionStatus,
    ValidationResult,
)
from coding_agent_harness.storage import StateStore, StorageError


def test_initialize_discards_failed_connection_and_can_retry(
    app_data: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3
    from contextlib import contextmanager

    import coding_agent_harness.storage as storage_module

    real_connection = sqlite3.connect(":memory:", isolation_level=None)

    class CloseFailingConnection:
        @property
        def row_factory(self) -> object:
            return real_connection.row_factory

        @row_factory.setter
        def row_factory(self, value: object) -> None:
            real_connection.row_factory = value  # type: ignore[assignment]

        def execute(self, statement: str) -> sqlite3.Cursor:
            return real_connection.execute(statement)

        def close(self) -> None:
            real_connection.close()
            raise sqlite3.Error("secret absolute path")

    @contextmanager
    def failing_transaction(_store: StateStore):
        raise StorageError("storage_begin_failed")
        yield

    store = StateStore(app_data / "retry.db")
    with monkeypatch.context() as patch:
        patch.setattr(storage_module.sqlite3, "connect", lambda *_args, **_kwargs: CloseFailingConnection())
        patch.setattr(StateStore, "transaction", failing_transaction)
        with pytest.raises(StorageError) as caught:
            store.initialize()
        assert str(caught.value) == "storage_begin_failed"
        assert store._connection is None
    store.initialize()
    assert store.upsert_project(workspace, "Retry")
    store.close()


def test_initialize_owns_and_closes_connection_before_pragma_configuration(
    app_data: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    import coding_agent_harness.storage as storage_module

    class PragmaFailingConnection:
        row_factory: object = None

        def __init__(self) -> None:
            self.closed = 0

        def execute(self, _statement: str) -> None:
            raise sqlite3.Error("private path detail")

        def close(self) -> None:
            self.closed += 1

    connection = PragmaFailingConnection()
    store = StateStore(app_data / "pragma-retry.db")
    with monkeypatch.context() as patch:
        patch.setattr(storage_module.sqlite3, "connect", lambda *_args, **_kwargs: connection)
        with pytest.raises(StorageError) as caught:
            store.initialize()
        assert str(caught.value) == "storage_initialize_failed"
    assert connection.closed == 1
    assert store._connection is None
    store.initialize()
    assert store.upsert_project(workspace, "Retry")
    store.close()


def test_project_is_canonical_and_idempotent(store: StateStore, workspace: Path) -> None:
    project_id = store.upsert_project(workspace / "child" / "..", "First")
    assert project_id == store.upsert_project(workspace, "Renamed")
    project = store.get_project(project_id)
    assert project.display_name == "Renamed"
    assert project.canonical_path == str(workspace.resolve(strict=False))


@pytest.mark.parametrize("error", [OSError("private path"), RuntimeError("private path")])
def test_project_path_resolution_failure_is_stable_and_private(
    error: Exception, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import traceback

    monkeypatch.setattr(Path, "resolve", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    with pytest.raises(StorageError) as caught:
        store.upsert_project(Path("private-path"), "Demo")
    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == "invalid_project_path"
    assert "private path" not in rendered


def test_session_persists_default_budget_and_restart(app_data: Path, workspace: Path) -> None:
    store = StateStore(app_data / "state.db")
    store.initialize()
    project_id = store.upsert_project(workspace, "Demo")
    session_id = store.create_session(project_id, "task")
    store.close()
    reopened = StateStore(app_data / "state.db")
    reopened.initialize()
    session = reopened.get_session(session_id)
    assert session.status is SessionStatus.CREATED
    assert session.budget == BudgetConfig().model_dump(mode="json")
    assert "+00:00" in session.created_at
    reopened.close()


def test_active_session_is_unique_and_terminal_allows_next(store: StateStore, workspace: Path) -> None:
    project_id = store.upsert_project(workspace, "Demo")
    session_id = store.create_session(project_id, "one")
    with pytest.raises(StorageError, match="active_session_exists"):
        store.create_session(project_id, "two")
    store.transition_session(session_id, SessionStatus.RUNNING)
    store.transition_session(session_id, SessionStatus.SUCCEEDED)
    assert store.create_session(project_id, "two")


def test_illegal_transition_does_not_change_state(store: StateStore, workspace: Path) -> None:
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    with pytest.raises(StorageError, match="illegal_session_transition"):
        store.transition_session(session_id, SessionStatus.SUCCEEDED)
    assert store.get_session(session_id).status is SessionStatus.CREATED


def test_action_and_policy_round_trip_with_tuple(store: StateStore, workspace: Path) -> None:
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    action_id = store.record_action(
        session_id, 1, CreateFileAction(path="a.txt", content="x"), "fingerprint"
    )
    decision_id = store.record_policy_decision(
        action_id, decision=Decision.ALLOW, reason_code="allowed", rule_source="test"
    )
    assert store.get_action(action_id).action == CreateFileAction(path="a.txt", content="x")
    assert store.get_policy_decision(decision_id).decision is Decision.ALLOW


def test_observations_and_validations_round_trip_and_batch_is_atomic(
    store: StateStore, workspace: Path
) -> None:
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    observation = Observation(category="success", summary="ok")
    store.record_observation(session_id, observation)
    assert store.latest_observation(session_id) == observation
    valid = ValidationResult(
        validator_id="pytest", stage="fast", status="passed", exit_code=0,
        duration_ms=1, summary="ok"
    )
    assert len(store.record_validations(session_id, [valid])) == 1
    assert store.list_validations(session_id) == (valid,)
    with pytest.raises(StorageError):
        store.record_validations(session_id, [valid, object()])  # type: ignore[list-item]
    assert store.list_validations(session_id) == (valid,)


def test_outer_transaction_rolls_back_action_policy_and_outbox(
    store: StateStore, workspace: Path
) -> None:
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    with pytest.raises(StorageError), store.transaction():
        action_id = store.record_action(
            session_id, 1, CreateFileAction(path="a", content="x"), "fp"
        )
        store.record_policy_decision(
            action_id, decision=Decision.ALLOW, reason_code="ok", rule_source="test"
        )
        store.enqueue_audit({"event": "one"})
        store.record_policy_decision(
            action_id, decision=Decision.ALLOW, reason_code="again", rule_source="test"
        )
    assert store.list_pending_audit() == ()
    with pytest.raises(StorageError):
        store.get_action(action_id)


def test_closed_store_and_unknown_records_fail_stably(store: StateStore, workspace: Path) -> None:
    with pytest.raises(StorageError, match="project_not_found"):
        store.get_project("missing")
    store.close()
    with pytest.raises(StorageError, match="storage_closed"):
        store.upsert_project(workspace, "Demo")


def test_budget_tracker_mapping_round_trips_and_unknown_session_is_rejected(
    store: StateStore, workspace: Path
) -> None:
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    tracker = {"steps": 3, "fingerprints": {"fp": 1}}
    store.save_budget_tracker(session_id, tracker)
    assert store.get_session(session_id).budget == {
        **BudgetConfig().model_dump(mode="json"),
        **tracker,
    }
    with pytest.raises(StorageError, match="session_not_found"):
        store.save_budget_tracker("missing", tracker)


def test_create_session_accepts_budget_config_with_all_budget_fields(
    store: StateStore, workspace: Path
) -> None:
    project_id = store.upsert_project(workspace, "Demo")
    session_id = store.create_session(project_id, "task", BudgetConfig(max_steps=7))
    budget = store.get_session(session_id).budget
    assert budget == BudgetConfig(max_steps=7).model_dump(mode="json")
    assert budget["max_steps"] == 7


def test_create_session_partial_budget_dict_fills_strict_defaults(
    store: StateStore, workspace: Path
) -> None:
    project_id = store.upsert_project(workspace, "Demo")
    session_id = store.create_session(project_id, "task", {"max_steps": 7})
    assert store.get_session(session_id).budget == BudgetConfig(max_steps=7).model_dump(mode="json")


@pytest.mark.parametrize(
    "budget",
    [
        {"max_steps": 999},
        {"unknown": 1},
        {"max_steps": "7"},
    ],
)
def test_create_session_rejects_invalid_budget_without_partial_write(
    budget: dict[str, object], store: StateStore, workspace: Path
) -> None:
    project_id = store.upsert_project(workspace, "Demo")
    with pytest.raises(StorageError, match="invalid_budget"):
        store.create_session(project_id, "task", budget)
    assert store._execute("SELECT COUNT(*) AS count FROM sessions").fetchone()["count"] == 0


@pytest.mark.parametrize(
    "update",
    [
        {"max_steps": 999},
        {"steps": -1},
        {"llm_calls": True},
        {"fingerprints": {"fp": -1}},
        {"fingerprints": {"fp": True}},
        {"fingerprints": {1: 1}},
        {"unknown": 1},
    ],
)
def test_save_budget_tracker_rejects_invalid_snapshot_atomically(
    update: dict[object, object], store: StateStore, workspace: Path
) -> None:
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    original = store.get_session(session_id).budget
    with pytest.raises(StorageError, match="invalid_budget"):
        store.save_budget_tracker(session_id, update)
    assert store.get_session(session_id).budget == original


@pytest.mark.parametrize(
    "payload",
    [
        {"max_steps": 7},
        {**BudgetConfig().model_dump(mode="json"), "max_steps": 999},
        {**BudgetConfig().model_dump(mode="json"), "steps": -1},
        {**BudgetConfig().model_dump(mode="json"), "fingerprints": {"fp": -1}},
        {**BudgetConfig().model_dump(mode="json"), "unknown": 1},
    ],
)
def test_get_session_rejects_invalid_budget_snapshot(
    payload: dict[str, object], store: StateStore, workspace: Path
) -> None:
    import json

    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    with store.transaction():
        store._execute(
            "UPDATE sessions SET budget_json = ? WHERE id = ?",
            (json.dumps(payload), session_id),
        )
    with pytest.raises(StorageError, match="corrupt_session"):
        store.get_session(session_id)


def test_schema_has_approved_columns_foreign_keys_and_active_writer_index(store: StateStore) -> None:
    import re

    connection = store._require_connection()
    required = {
        "projects": {"id", "canonical_path", "display_name"},
        "sessions": {"id", "project_id", "task", "status", "budget_json", "created_at", "updated_at"},
        "actions": {"id", "session_id", "step", "tool", "normalized_json", "fingerprint", "created_at"},
        "policy_decisions": {"id", "action_id", "decision", "reason_code", "rule_source", "created_at"},
        "approvals": {"id", "action_id", "session_id", "fingerprint", "nonce_digest", "status", "created_at", "expires_at", "decided_at", "consumed_at"},
        "observations": {"id", "session_id", "action_id", "category", "summary", "evidence", "created_at"},
        "validations": {"id", "session_id", "validator_id", "stage", "status", "exit_code", "duration_ms", "summary", "evidence", "created_at"},
        "memory_entries": {"id", "project_id", "source_session_id", "memory_type", "content", "evidence_action_id", "tags_json", "status", "created_at", "updated_at"},
        "changes": {"id", "session_id", "relative_path", "operation", "before_digest", "after_digest", "backup_ref", "sequence", "created_at"},
        "audit_outbox": {"sequence", "event_json", "created_at", "flushed_at"},
    }
    for table, columns in required.items():
        actual = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        assert columns <= actual
    assert {row["from"] for row in connection.execute("PRAGMA foreign_key_list(approvals)")} >= {"action_id", "session_id"}
    assert {row["from"] for row in connection.execute("PRAGMA foreign_key_list(memory_entries)")} >= {"project_id", "source_session_id", "evidence_action_id"}
    index = next(
        row for row in connection.execute("PRAGMA index_list(sessions)")
        if row["name"] == "one_active_writer"
    )
    assert index["unique"] == 1
    assert index["partial"] == 1
    index_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'one_active_writer'"
    ).fetchone()["sql"]
    assert set(re.findall(r"'([A-Z_]+)'", index_sql)) == {
        "CREATED", "RUNNING", "PAUSED_APPROVAL", "PAUSED_LIMIT_REACHED",
        "PAUSED_PROTOCOL_ERROR", "PAUSED_WORKSPACE_DRIFT", "PAUSED_INTERNAL_ERROR",
        "NEEDS_USER_DECISION",
    }
    assert not {"SUCCEEDED", "CHANGES_KEPT", "ROLLED_BACK"} & set(
        re.findall(r"'([A-Z_]+)'", index_sql)
    )


def test_record_action_rejects_non_action_and_audit_json_failure_is_atomic(
    store: StateStore, workspace: Path
) -> None:
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    with pytest.raises(StorageError, match="invalid_action"):
        store.record_action(session_id, 1, BudgetConfig(), "fp")  # type: ignore[arg-type]
    with pytest.raises(StorageError, match="storage_serialization_failed"):
        store.enqueue_audit({"bad": object()})
    assert store.list_pending_audit() == ()


def test_observation_and_validation_order_follow_insert_order_with_fixed_clock(
    app_data: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    import coding_agent_harness.storage as storage_module

    store = StateStore(app_data / "ordered.db", clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    store.initialize()
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    identifiers = iter(["z" * 32, "a" * 32, "y" * 32, "b" * 32])
    monkeypatch.setattr(storage_module, "_new_id", lambda: next(identifiers))
    first = Observation(category="success", summary="first")
    second = Observation(category="success", summary="second")
    store.record_observation(session_id, first)
    store.record_observation(session_id, second)
    first_validation = ValidationResult(
        validator_id="first", stage="fast", status="passed", exit_code=0,
        duration_ms=1, summary="first"
    )
    second_validation = ValidationResult(
        validator_id="second", stage="fast", status="passed", exit_code=0,
        duration_ms=1, summary="second"
    )
    store.record_validations(session_id, [first_validation, second_validation])
    assert store.latest_observation(session_id) == second
    assert store.list_validations(session_id) == (first_validation, second_validation)
    store.close()


def test_transaction_begin_failure_is_stable_and_store_recovers(app_data: Path, workspace: Path) -> None:
    first = StateStore(app_data / "locked.db")
    second = StateStore(app_data / "locked.db")
    first.initialize()
    second.initialize()
    project_id = first.upsert_project(workspace, "Demo")
    second._require_connection().execute("PRAGMA busy_timeout = 0")
    with first.transaction(), pytest.raises(StorageError, match="storage_begin_failed"), second.transaction():
        pass
    with second.transaction():
        session_id = second.create_session(project_id, "task")
    assert second.get_session(session_id).id == session_id
    first.close()
    second.close()


def test_transaction_rolls_back_keyboard_interrupt_and_remains_writable(
    store: StateStore, workspace: Path
) -> None:
    connection = store._require_connection()
    with pytest.raises(KeyboardInterrupt), store.transaction():
        raise KeyboardInterrupt
    assert store._transaction_depth == 0
    assert connection.in_transaction is False
    assert store.upsert_project(workspace, "Recovered")


@pytest.mark.parametrize("failure", ["rollback", "commit_then_rollback"])
def test_transaction_discards_connection_when_recovery_state_is_unknown(
    failure: str, app_data: Path
) -> None:
    import sqlite3

    class FailingConnection:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, _statement: str) -> None:
            return None

        def commit(self) -> None:
            if failure == "commit_then_rollback":
                raise sqlite3.Error("commit detail")

        def rollback(self) -> None:
            raise sqlite3.Error("rollback detail")

        def close(self) -> None:
            self.closed = True

    store = StateStore(app_data / "unknown.db")
    connection = FailingConnection()
    store._connection = connection  # type: ignore[assignment]
    expected = "storage_rollback_failed"
    with pytest.raises(StorageError, match=expected), store.transaction():
        if failure == "rollback":
            raise RuntimeError("body detail")
    assert store._connection is None
    assert store._transaction_depth == 0
    assert connection.closed is True


def test_duplicate_policy_decision_has_stable_error(store: StateStore, workspace: Path) -> None:
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    action_id = store.record_action(session_id, 1, CreateFileAction(path="a", content="x"), "fp")
    store.record_policy_decision(
        action_id, decision=Decision.ALLOW, reason_code="ok", rule_source="test"
    )
    with pytest.raises(StorageError, match="policy_decision_exists"):
        store.record_policy_decision(
            action_id, decision=Decision.ALLOW, reason_code="again", rule_source="test"
        )


def test_restart_restores_action_observation_and_validation(app_data: Path, workspace: Path) -> None:
    store = StateStore(app_data / "restart.db")
    store.initialize()
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    action = CreateFileAction(path="a", content="x")
    action_id = store.record_action(session_id, 1, action, "fp")
    observation = Observation(category="success", summary="ok", action_id=action_id)
    validation = ValidationResult(
        validator_id="pytest", stage="final", status="passed", exit_code=0,
        duration_ms=1, summary="ok"
    )
    store.record_observation(session_id, observation)
    store.record_validation(session_id, validation)
    store.close()
    reopened = StateStore(app_data / "restart.db")
    reopened.initialize()
    assert reopened.get_action(action_id).action == action
    assert reopened.latest_observation(session_id) == observation
    assert reopened.list_validations(session_id) == (validation,)
    reopened.close()


def test_observation_cannot_reference_action_from_another_session(
    store: StateStore, workspace: Path
) -> None:
    first_project = store.upsert_project(workspace, "First")
    first_session = store.create_session(first_project, "first")
    action_id = store.record_action(
        first_session, 1, CreateFileAction(path="a", content="x"), "fp"
    )
    store.transition_session(first_session, SessionStatus.RUNNING)
    store.transition_session(first_session, SessionStatus.SUCCEEDED)
    second_session = store.create_session(first_project, "second")
    with pytest.raises(StorageError, match="observation_action_mismatch"):
        store.record_observation(
            second_session, Observation(category="success", summary="bad", action_id=action_id)
        )


def test_corrupt_persisted_json_fails_closed(store: StateStore, workspace: Path) -> None:
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    action_id = store.record_action(
        session_id, 1, CreateFileAction(path="a", content="x"), "fp"
    )
    with store.transaction():
        store._execute("UPDATE actions SET normalized_json = ? WHERE id = ?", ("{", action_id))
    with pytest.raises(StorageError, match="corrupt_action"):
        store.get_action(action_id)


def test_action_tool_column_must_match_normalized_action(
    store: StateStore, workspace: Path
) -> None:
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    action_id = store.record_action(
        session_id, 1, CreateFileAction(path="a", content="x"), "fp"
    )
    with store.transaction():
        store._execute("UPDATE actions SET tool = ? WHERE id = ?", ("read_file", action_id))
    with pytest.raises(StorageError, match="corrupt_action"):
        store.get_action(action_id)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_audit_values_are_rejected_without_storage_write(
    value: float, store: StateStore, tmp_path: Path
) -> None:
    with pytest.raises(StorageError, match="storage_serialization_failed"):
        store.enqueue_audit({"value": value})
    assert store.list_pending_audit() == ()
    audit_path = tmp_path / "audit.jsonl"
    with pytest.raises(AuditError, match="audit_append_failed"):
        AuditWriter(audit_path).append({"value": value})
    assert not audit_path.exists() or audit_path.read_bytes() == b""


def test_corrupt_session_observation_validation_and_outbox_fail_closed(
    store: StateStore, workspace: Path
) -> None:
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    store.record_observation(session_id, Observation(category="success", summary="ok"))
    store.record_validation(
        session_id,
        ValidationResult(
            validator_id="pytest", stage="fast", status="passed", exit_code=0,
            duration_ms=1, summary="ok"
        ),
    )
    store.enqueue_audit({"event": "ok"})
    with store.transaction():
        store._execute("UPDATE sessions SET budget_json = ? WHERE id = ?", ("{", session_id))
    with pytest.raises(StorageError, match="corrupt_session"):
        store.get_session(session_id)
    with store.transaction():
        store._execute("UPDATE observations SET category = ? WHERE session_id = ?", ("invalid", session_id))
    with pytest.raises(StorageError, match="corrupt_observation"):
        store.latest_observation(session_id)
    with store.transaction():
        store._execute("UPDATE validations SET stage = ? WHERE session_id = ?", ("invalid", session_id))
    with pytest.raises(StorageError, match="corrupt_validation"):
        store.list_validations(session_id)
    with store.transaction():
        store._execute("UPDATE audit_outbox SET event_json = ?", ("{",))
    with pytest.raises(StorageError, match="corrupt_audit_outbox"):
        store.list_pending_audit()


def test_audit_writer_redacts_nested_values_and_preserves_valid_json(
    tmp_path: Path, workspace: Path
) -> None:
    audit_path = tmp_path / "logs" / "audit.jsonl"
    writer = AuditWriter(audit_path, workspace=workspace, secrets=('a"\\secret',))
    writer.append({"nested": ["a\"\\secret", {"path": str(workspace / "x.txt")} ]})
    writer.append({"event": "second"})
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    payload = __import__("json").loads(lines[0])
    assert payload["nested"][0] == "<REDACTED>"
    assert "a\"\\secret" not in lines[0]
    assert str(workspace) not in lines[0]
    assert payload["nested"][1]["path"].startswith("<WORKSPACE>")


def test_audit_writer_reports_stable_error_when_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = AuditWriter(tmp_path / "audit.jsonl")
    monkeypatch.setattr("coding_agent_harness.audit.os.fsync", lambda _fd: (_ for _ in ()).throw(OSError()))
    with pytest.raises(AuditError, match="audit_append_failed"):
        writer.append({"secret": "not-in-error"})


@pytest.mark.parametrize("stage", ["serialize", "mkdir", "open", "write", "flush"])
def test_audit_writer_maps_all_append_failures_without_leaking_details(
    stage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import coding_agent_harness.audit as audit_module

    secret = "quoted\"\\secret"
    audit_path = tmp_path / "private" / secret / "audit.jsonl"

    class FailingFile:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def write(self, _value: str) -> None:
            if stage == "write":
                raise OSError(secret)

        def flush(self) -> None:
            if stage == "flush":
                raise OSError(secret)

        def fileno(self) -> int:
            return 1

    event: dict[str, object] = {"secret": secret}
    if stage == "serialize":
        event["bad"] = object()
    elif stage == "mkdir":
        monkeypatch.setattr(Path, "mkdir", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(secret)))
    elif stage == "open":
        monkeypatch.setattr(audit_module.os, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(secret)))
    else:
        monkeypatch.setattr(audit_module.os, "open", lambda *_args, **_kwargs: 1)
        monkeypatch.setattr(audit_module.os, "fdopen", lambda *_args, **_kwargs: FailingFile())
        monkeypatch.setattr(audit_module.os, "fsync", lambda _fd: None)
    with pytest.raises(AuditError) as caught:
        AuditWriter(audit_path, secrets=(secret,)).append(event)
    assert str(caught.value) == "audit_append_failed"
    assert secret not in str(caught.value)
    assert str(audit_path) not in str(caught.value)


def test_audit_writer_closes_raw_fd_and_suppresses_sensitive_traceback_on_fdopen_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import traceback

    import coding_agent_harness.audit as audit_module

    secret = "trace-secret"
    audit_path = tmp_path / secret / "audit.jsonl"
    closed: list[int] = []
    monkeypatch.setattr(audit_module.os, "open", lambda *_args, **_kwargs: 47)
    monkeypatch.setattr(
        audit_module.os,
        "fdopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(f"underlying {secret} {audit_path}")),
    )
    monkeypatch.setattr(audit_module.os, "close", lambda descriptor: closed.append(descriptor))
    with pytest.raises(AuditError) as caught:
        AuditWriter(audit_path, secrets=(secret,)).append({"secret": secret})
    rendered = "".join(traceback.format_exception(caught.value))
    assert closed == [47]
    assert str(caught.value) == "audit_append_failed"
    assert secret not in rendered
    assert str(audit_path) not in rendered
    assert "underlying" not in rendered


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit()])
def test_audit_writer_closes_raw_fd_and_propagates_base_exception(
    interruption: BaseException, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import coding_agent_harness.audit as audit_module

    closed: list[int] = []
    monkeypatch.setattr(audit_module.os, "open", lambda *_args, **_kwargs: 59)
    monkeypatch.setattr(
        audit_module.os,
        "fdopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(interruption),
    )
    monkeypatch.setattr(audit_module.os, "close", lambda descriptor: closed.append(descriptor))
    with pytest.raises(type(interruption)):
        AuditWriter(tmp_path / "audit.jsonl").append({"event": "interrupt"})
    assert closed == [59]


def test_audit_writer_preserves_keys_that_collide_after_redaction(
    tmp_path: Path, workspace: Path
) -> None:
    import json

    first_secret = "secret-one"
    second_secret = "secret-two"
    audit_path = tmp_path / "audit.jsonl"
    event = {
        first_secret: "first",
        second_secret: "second",
        str(workspace / "private"): "workspace",
        "<REDACTED_KEY_1>": "reserved",
    }
    AuditWriter(
        audit_path, workspace=workspace, secrets=(first_secret, second_secret)
    ).append(event)
    line = audit_path.read_text(encoding="utf-8").strip()
    decoded = json.loads(line)
    assert len(decoded) == 4
    assert set(decoded.values()) == {"first", "second", "workspace", "reserved"}
    assert first_secret not in line and second_secret not in line
    assert str(workspace) not in line


def test_outbox_flushes_in_order_retries_after_failure_and_survives_restart(
    app_data: Path, store: StateStore
) -> None:
    first = store.enqueue_audit({"sequence": "wrong", "event": "one"})
    second = store.enqueue_audit({"event": "two"})

    class FailingWriter:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def append(self, event: dict[str, object]) -> None:
            self.events.append(event)
            if len(self.events) == 2:
                raise AuditError("audit_append_failed")

    writer = FailingWriter()
    with pytest.raises(AuditError):
        store.flush_audit(writer)
    assert writer.events == [{"sequence": first, "event": "one"}, {"event": "two", "sequence": second}]
    assert [item.sequence for item in store.list_pending_audit()] == [second]
    store.close()
    reopened = StateStore(app_data / "state.db")
    reopened.initialize()
    retry = FailingWriter()
    assert reopened.flush_audit(retry) == 1
    assert retry.events == [{"event": "two", "sequence": second}]
    assert reopened.list_pending_audit() == ()
    reopened.close()


def test_outbox_replays_same_sequence_when_marking_flushed_fails(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sequence = store.enqueue_audit({"event": "once"})
    events: list[dict[str, object]] = []

    class Writer:
        def append(self, event: dict[str, object]) -> None:
            events.append(event)

    original_execute = store._execute
    failed = False

    def fail_first_update(statement: str, parameters: tuple[object, ...] = ()):
        nonlocal failed
        if statement.startswith("UPDATE audit_outbox SET flushed_at") and not failed:
            failed = True
            raise StorageError("storage_operation_failed")
        return original_execute(statement, parameters)

    monkeypatch.setattr(store, "_execute", fail_first_update)
    with pytest.raises(StorageError, match="storage_operation_failed"):
        store.flush_audit(Writer())
    assert [item.sequence for item in store.list_pending_audit()] == [sequence]
    assert store.flush_audit(Writer()) == 1
    assert events == [
        {"event": "once", "sequence": sequence},
        {"event": "once", "sequence": sequence},
    ]
    assert store.list_pending_audit() == ()


def test_outbox_flush_rejects_outer_transaction_before_writer_call(
    store: StateStore
) -> None:
    events: list[dict[str, object]] = []

    class Writer:
        def append(self, event: dict[str, object]) -> None:
            events.append(event)

    with pytest.raises(RuntimeError, match="rollback outer transaction"), store.transaction():
        store.enqueue_audit({"event": "outer"})
        with pytest.raises(
            StorageError, match="audit_flush_requires_independent_transaction"
        ):
            store.flush_audit(Writer())
        assert events == []
        raise RuntimeError("rollback outer transaction")
    assert store.list_pending_audit() == ()
    sequence = store.enqueue_audit({"event": "committed"})
    assert store.flush_audit(Writer()) == 1
    assert events == [{"event": "committed", "sequence": sequence}]


def test_concurrent_outbox_flush_preserves_global_sequence_and_single_delivery(
    app_data: Path
) -> None:
    import threading

    database = app_data / "concurrent-outbox.db"
    setup = StateStore(database)
    setup.initialize()
    sequences = [setup.enqueue_audit({"event": name}) for name in ("one", "two")]
    setup.close()
    first_append_entered = threading.Event()
    release_first_append = threading.Event()
    second_duplicate_seen = threading.Event()
    lock = threading.Lock()
    output: list[int] = []

    class BlockingWriter:
        def append(self, event: dict[str, object]) -> None:
            sequence = int(event["sequence"])
            with lock:
                output.append(sequence)
                position = len(output)
            if position == 1:
                first_append_entered.set()
                assert release_first_append.wait(5)
            elif sequence == sequences[0]:
                second_duplicate_seen.set()

    writer = BlockingWriter()
    counts: list[int] = []
    errors: list[BaseException] = []

    def flush() -> None:
        store = StateStore(database)
        try:
            store.initialize()
            counts.append(store.flush_audit(writer))
        except Exception as error:  # noqa: BLE001 - surface thread failures to the test.
            errors.append(error)
        finally:
            store.close()

    first_thread = threading.Thread(target=flush)
    second_thread = threading.Thread(target=flush)
    first_thread.start()
    assert first_append_entered.wait(5)
    second_thread.start()
    second_duplicate_seen.wait(0.25)
    release_first_append.set()
    first_thread.join(5)
    second_thread.join(5)
    assert not first_thread.is_alive() and not second_thread.is_alive()
    assert errors == []
    assert output == sequences
    assert sum(counts) == 2
    check = StateStore(database)
    check.initialize()
    assert check.list_pending_audit() == ()
    check.close()


def test_validation_batch_rolls_back_on_second_database_failure(
    store: StateStore, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    results = [
        ValidationResult(
            validator_id=name, stage="fast", status="passed", exit_code=0,
            duration_ms=1, summary=name
        )
        for name in ("first", "second")
    ]
    original_execute = store._execute
    inserts = 0

    def fail_second_insert(statement: str, parameters: tuple[object, ...] = ()):
        nonlocal inserts
        if statement.startswith("INSERT INTO validations"):
            inserts += 1
            if inserts == 2:
                raise StorageError("storage_operation_failed")
        return original_execute(statement, parameters)

    monkeypatch.setattr(store, "_execute", fail_second_insert)
    with pytest.raises(StorageError, match="storage_operation_failed"):
        store.record_validations(session_id, results)
    assert store.list_validations(session_id) == ()
