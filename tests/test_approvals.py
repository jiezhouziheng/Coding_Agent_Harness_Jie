from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from coding_agent_harness.approvals import ApprovalError, ApprovalService, BudgetTracker
from coding_agent_harness.config import BudgetConfig
from coding_agent_harness.models import ApprovalStatus, Decision, parse_action
from coding_agent_harness.policy import PendingAction
from coding_agent_harness.security import (
    action_fingerprint,
    normalize_action,
    workspace_fingerprint,
)
from coding_agent_harness.storage import ApprovalRecord, StateStore, StorageError


class Writer:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[dict[str, object]] = []
        self.fail = fail

    def append(self, event: dict[str, object]) -> None:
        self.events.append(event)
        if self.fail:
            raise OSError("private audit path")


def _clock_box(now: datetime):
    value = [now]
    return value, lambda: value[0]


def _pending(
    store: StateStore,
    workspace: Path,
    *,
    step: int = 1,
    action_payload: dict[str, object] | None = None,
) -> PendingAction:
    action = normalize_action(
        parse_action(
            action_payload
            or {"tool": "create_file", "path": "src/new.py", "content": "value = 1\n"}
        ),
        __import__("coding_agent_harness.security", fromlist=["WorkspaceGuard"]).WorkspaceGuard(
            workspace
        ),
    )
    project_id = store.upsert_project(workspace, "Demo")
    try:
        session_id = store.create_session(project_id, "task")
    except StorageError:
        session_id = str(
            store._execute(
                "SELECT id FROM sessions WHERE project_id = ? ORDER BY rowid DESC LIMIT 1",
                (project_id,),
            ).fetchone()[0]
        )
    fingerprint = action_fingerprint(action)
    action_id = store.record_action(session_id, step, action, fingerprint)
    store.record_policy_decision(
        action_id,
        decision=Decision.REQUIRE_APPROVAL,
        reason_code="file_lifecycle_change",
        rule_source="builtin",
    )
    return PendingAction(
        action_id=action_id,
        session_id=session_id,
        action=action,
        fingerprint=fingerprint,
    )


def test_approval_schema_migrates_existing_task4_database(app_data: Path) -> None:
    database = app_data / "legacy.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE approvals (id TEXT PRIMARY KEY, action_id TEXT NOT NULL UNIQUE, "
        "session_id TEXT NOT NULL, fingerprint TEXT NOT NULL, nonce_digest TEXT NOT NULL, "
        "status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
        "decided_at TEXT, consumed_at TEXT)"
    )
    connection.execute(
        "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
        (
            "legacy-approval",
            "legacy-action",
            "legacy-session",
            "legacy-fingerprint",
            "legacy-nonce-digest",
            ApprovalStatus.APPROVED.value,
            "2026-08-12T00:00:00+00:00",
            "2026-08-12T01:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()
    store = StateStore(database)
    store.initialize()
    columns = {
        str(row["name"]) for row in store._execute("PRAGMA table_info(approvals)").fetchall()
    }
    assert "workspace_fingerprint" in columns
    migrated = store.get_approval("legacy-approval")
    assert migrated.workspace_fingerprint == ""
    assert migrated.status is ApprovalStatus.INVALIDATED
    store.close()
    reopened = StateStore(database)
    reopened.initialize()
    assert "workspace_fingerprint" in {
        str(row["name"])
        for row in reopened._execute("PRAGMA table_info(approvals)").fetchall()
    }
    reopened.close()


def test_request_persists_proposed_then_pending_with_256_bit_nonce(
    store: StateStore, workspace: Path
) -> None:
    (workspace / "src").mkdir()
    pending = _pending(store, workspace)
    writer = Writer()
    token = "ab" * 32
    service = ApprovalService(store, writer, token_source=lambda: token)
    approval = service.request(pending, workspace, expires_in=timedelta(minutes=10))
    assert approval.id == token
    assert approval.nonce_digest == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert approval.status is ApprovalStatus.PENDING
    assert approval.workspace_fingerprint == workspace_fingerprint(pending.action, workspace)
    assert approval.workspace_fingerprint != pending.fingerprint
    assert [event["status"] for event in writer.events] == ["PROPOSED", "PENDING"]
    assert all(token not in str(event) for event in writer.events)
    assert store.list_pending_approvals(pending.session_id) == (approval,)
    with pytest.raises(ValidationError):
        approval.status = ApprovalStatus.APPROVED  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ApprovalRecord.model_validate({**approval.model_dump(), "unknown": True})


def test_request_in_transaction_rejects_calls_without_outer_transaction(
    store: StateStore, workspace: Path
) -> None:
    (workspace / "src").mkdir()
    pending = _pending(store, workspace)
    service = ApprovalService(store, Writer(), token_source=lambda: "aa" * 32)

    with pytest.raises(ApprovalError, match="approval_transaction_required"):
        service.request_in_transaction(
            pending, workspace, expires_in=timedelta(minutes=10)
        )

    assert store.list_pending_approvals() == ()
    assert store._execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    assert store.list_pending_audit() == ()


def test_request_rejects_deny_policy_decision(store: StateStore, workspace: Path) -> None:
    action = normalize_action(
        parse_action({"tool": "create_file", "path": "denied.py", "content": "x"}),
        __import__("coding_agent_harness.security", fromlist=["WorkspaceGuard"]).WorkspaceGuard(
            workspace
        ),
    )
    project_id = store.upsert_project(workspace, "Demo")
    session_id = store.create_session(project_id, "task")
    fingerprint = action_fingerprint(action)
    action_id = store.record_action(session_id, 1, action, fingerprint)
    store.record_policy_decision(
        action_id, decision=Decision.DENY, reason_code="deny", rule_source="builtin"
    )
    pending = PendingAction(
        action_id=action_id,
        session_id=session_id,
        action=action,
        fingerprint=fingerprint,
    )
    with pytest.raises(ApprovalError, match="approval_policy_not_approvable"):
        ApprovalService(store, Writer()).request(
            pending, workspace, expires_in=timedelta(minutes=10)
        )
    assert store.list_pending_approvals() == ()


def test_approved_action_survives_restart_and_is_consumed_once(
    app_data: Path, workspace: Path
) -> None:
    (workspace / "src").mkdir()
    database = app_data / "restart.db"
    store = StateStore(database)
    store.initialize()
    pending = _pending(store, workspace)
    writer = Writer()
    token = "cd" * 32
    service = ApprovalService(store, writer, token_source=lambda: token)
    service.request(pending, workspace, expires_in=timedelta(minutes=10))
    service.approve(token)
    store.close()
    reopened = StateStore(database)
    reopened.initialize()
    restarted = ApprovalService(reopened, writer)
    grant = restarted.consume(token, pending, workspace)
    assert grant.approval_id == token
    assert grant.action_id == pending.action_id
    assert reopened.is_consumed_approval(token, pending.fingerprint)
    assert not reopened.is_consumed_approval(token, "wrong-fingerprint")
    with pytest.raises(ApprovalError, match="approval_not_approved"):
        restarted.consume(token, pending, workspace)
    reopened.close()


def test_target_content_drift_invalidates_approval(store: StateStore, workspace: Path) -> None:
    (workspace / "src").mkdir()
    target = workspace / "src" / "existing.py"
    target.write_text("before\n", encoding="utf-8")
    pending = _pending(
        store,
        workspace,
        action_payload={
            "tool": "replace_in_file",
            "path": "src/existing.py",
            "old_text": "before",
            "new_text": "after",
        },
    )
    service = ApprovalService(store, Writer(), token_source=lambda: "ef" * 32)
    approval = service.request(pending, workspace, expires_in=timedelta(minutes=10))
    service.approve(approval.id)
    target.write_text("external change\n", encoding="utf-8")
    with pytest.raises(ApprovalError, match="approval_workspace_drift"):
        service.consume(approval.id, pending, workspace)
    assert store.get_approval(approval.id).status is ApprovalStatus.INVALIDATED


def test_create_target_appearing_invalidates_approval(store: StateStore, workspace: Path) -> None:
    (workspace / "src").mkdir()
    pending = _pending(store, workspace)
    service = ApprovalService(store, Writer(), token_source=lambda: "12" * 32)
    approval = service.request(pending, workspace, expires_in=timedelta(minutes=10))
    service.approve(approval.id)
    (workspace / "src" / "new.py").write_text("external\n", encoding="utf-8")
    with pytest.raises(ApprovalError, match="approval_workspace_drift"):
        service.consume(approval.id, pending, workspace)
    assert store.get_approval(approval.id).status is ApprovalStatus.INVALIDATED


@pytest.mark.parametrize("mutation", ["modify", "create", "delete"])
def test_delete_directory_workspace_fingerprint_tracks_visible_tree(
    mutation: str, workspace: Path
) -> None:
    target = workspace / "tree"
    target.mkdir()
    original = target / "original.txt"
    original.write_text("before\n", encoding="utf-8")
    action = parse_action({"tool": "delete_file", "path": "tree"})
    before = workspace_fingerprint(action, workspace)
    if mutation == "modify":
        original.write_text("after\n", encoding="utf-8")
    elif mutation == "create":
        (target / "new.txt").write_text("new\n", encoding="utf-8")
    else:
        original.unlink()
    assert workspace_fingerprint(action, workspace) != before


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("action", "approval_action_mismatch"),
        ("action_id", "approval_action_mismatch"),
        ("session", "approval_session_mismatch"),
        ("nonce", "approval_nonce_mismatch"),
    ],
)
def test_consume_binding_failures_invalidate_without_grant(
    mutation: str, expected: str, store: StateStore, workspace: Path
) -> None:
    (workspace / "src").mkdir()
    pending = _pending(store, workspace)
    service = ApprovalService(store, Writer(), token_source=lambda: "34" * 32)
    approval = service.request(pending, workspace, expires_in=timedelta(minutes=10))
    service.approve(approval.id)
    candidate = pending
    approval_id = approval.id
    if mutation == "action":
        changed = parse_action(
            {"tool": "create_file", "path": "src/other.py", "content": "value = 1\n"}
        )
        candidate = pending.model_copy(
            update={"action": changed, "fingerprint": action_fingerprint(changed)}
        )
    elif mutation == "action_id":
        candidate = pending.model_copy(update={"action_id": "other-action"})
    elif mutation == "session":
        candidate = pending.model_copy(update={"session_id": "other-session"})
    else:
        approval_id = "56" * 32
    with pytest.raises(ApprovalError, match=expected):
        service.consume(approval_id, candidate, workspace)
    assert store.get_approval(approval.id).status is ApprovalStatus.INVALIDATED


def test_expired_and_denied_approvals_never_return_grant(
    store: StateStore, workspace: Path
) -> None:
    (workspace / "src").mkdir()
    now, clock = _clock_box(datetime(2026, 8, 12, tzinfo=UTC))
    first = _pending(store, workspace)
    service = ApprovalService(store, Writer(), clock=clock, token_source=lambda: "78" * 32)
    expired = service.request(first, workspace, expires_in=timedelta(seconds=1))
    service.approve(expired.id)
    now[0] += timedelta(seconds=2)
    with pytest.raises(ApprovalError, match="approval_expired"):
        service.consume(expired.id, first, workspace)
    assert store.get_approval(expired.id).status is ApprovalStatus.EXPIRED
    store.transition_session(first.session_id, __import__("coding_agent_harness.models", fromlist=["SessionStatus"]).SessionStatus.RUNNING)
    store.transition_session(first.session_id, __import__("coding_agent_harness.models", fromlist=["SessionStatus"]).SessionStatus.SUCCEEDED)
    second = _pending(store, workspace, step=2)
    denied = ApprovalService(store, Writer(), token_source=lambda: "9a" * 32)
    denied_record = denied.request(second, workspace, expires_in=timedelta(minutes=10))
    denied.deny(denied_record.id)
    with pytest.raises(ApprovalError, match="approval_not_approved"):
        denied.consume(denied_record.id, second, workspace)


def test_expire_and_invalidate_are_persistent_audited_transitions(
    store: StateStore, workspace: Path
) -> None:
    (workspace / "src").mkdir()
    first = _pending(store, workspace)
    writer = Writer()
    service = ApprovalService(store, writer, token_source=lambda: "01" * 32)
    expired = service.request(first, workspace, expires_in=timedelta(minutes=10))
    assert service.expire(expired.id).status is ApprovalStatus.EXPIRED
    store.transition_session(first.session_id, __import__("coding_agent_harness.models", fromlist=["SessionStatus"]).SessionStatus.RUNNING)
    store.transition_session(first.session_id, __import__("coding_agent_harness.models", fromlist=["SessionStatus"]).SessionStatus.SUCCEEDED)
    second = _pending(store, workspace, step=2)
    invalidator = ApprovalService(store, writer, token_source=lambda: "02" * 32)
    invalid = invalidator.request(second, workspace, expires_in=timedelta(minutes=10))
    invalidator.approve(invalid.id)
    assert invalidator.invalidate(invalid.id).status is ApprovalStatus.INVALIDATED
    assert [event["status"] for event in writer.events][-2:] == ["APPROVED", "INVALIDATED"]


def test_approve_marks_already_expired_pending_request_expired(
    store: StateStore, workspace: Path
) -> None:
    (workspace / "src").mkdir()
    current, clock = _clock_box(datetime(2026, 8, 12, tzinfo=UTC))
    pending = _pending(store, workspace)
    service = ApprovalService(
        store, Writer(), clock=clock, token_source=lambda: "03" * 32
    )
    approval = service.request(pending, workspace, expires_in=timedelta(seconds=1))
    current[0] += timedelta(seconds=2)
    with pytest.raises(ApprovalError, match="approval_expired"):
        service.approve(approval.id)
    assert store.get_approval(approval.id).status is ApprovalStatus.EXPIRED


def test_approval_transition_and_audit_enqueue_are_atomic(
    store: StateStore,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (workspace / "src").mkdir()
    pending = _pending(store, workspace)
    service = ApprovalService(store, Writer(), token_source=lambda: "04" * 32)
    approval = service.request(pending, workspace, expires_in=timedelta(minutes=10))
    original = store.enqueue_audit

    def fail(event: dict[str, object]) -> int:
        if event.get("status") == ApprovalStatus.APPROVED.value:
            raise StorageError("private database detail")
        return original(event)

    monkeypatch.setattr(store, "enqueue_audit", fail)
    with pytest.raises(ApprovalError, match="illegal_approval_transition"):
        service.approve(approval.id)
    assert store.get_approval(approval.id).status is ApprovalStatus.PENDING


def test_approval_audit_failure_never_returns_grant(store: StateStore, workspace: Path) -> None:
    (workspace / "src").mkdir()
    pending = _pending(store, workspace)
    setup = ApprovalService(store, Writer(), token_source=lambda: "bc" * 32)
    approval = setup.request(pending, workspace, expires_in=timedelta(minutes=10))
    setup.approve(approval.id)
    result: list[object] = []
    with pytest.raises(ApprovalError, match="approval_audit_failed"):
        result.append(ApprovalService(store, Writer(fail=True)).consume(approval.id, pending, workspace))
    assert result == []
    assert store.get_approval(approval.id).status is ApprovalStatus.CONSUMED


def test_workspace_fingerprint_binds_git_and_pip_inputs_without_reading_dot_git(
    workspace: Path,
) -> None:
    (workspace / "src").mkdir()
    source = workspace / "src" / "a.py"
    source.write_text("one\n", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "secret").write_text("do not read\n", encoding="utf-8")
    git_add = parse_action({"tool": "run_command", "program": "git", "args": ["add", "src/a.py"]})
    commit = parse_action({"tool": "run_command", "program": "git", "args": ["commit", "-m", "message"]})
    before_add = workspace_fingerprint(git_add, workspace)
    before_commit = workspace_fingerprint(commit, workspace)
    (workspace / ".git" / "secret").write_text("changed only git internals\n", encoding="utf-8")
    assert workspace_fingerprint(commit, workspace) == before_commit
    source.write_text("two\n", encoding="utf-8")
    assert workspace_fingerprint(git_add, workspace) != before_add
    assert workspace_fingerprint(commit, workspace) != before_commit


def test_workspace_fingerprint_binds_local_pip_package_contents(workspace: Path) -> None:
    package = workspace / "pkg"
    package.mkdir()
    metadata = package / "pyproject.toml"
    metadata.write_text("[project]\nname='demo'\n", encoding="utf-8")
    action = parse_action(
        {
            "tool": "run_command",
            "program": "python",
            "args": ["-m", "pip", "install", "--no-index", "pkg"],
        }
    )
    before = workspace_fingerprint(action, workspace)
    metadata.write_text("[project]\nname='changed'\n", encoding="utf-8")
    assert workspace_fingerprint(action, workspace) != before


def test_budget_tracker_round_trips_complete_snapshot_and_prioritizes_limits(
    store: StateStore, workspace: Path
) -> None:
    current, clock = _clock_box(datetime(2026, 8, 12, tzinfo=UTC))
    config = BudgetConfig(
        max_steps=2,
        max_llm_calls=1,
        max_consecutive_failures=1,
        max_repeated_action=2,
        command_timeout_seconds=150,
        session_timeout_minutes=1,
        max_observation_bytes=6000,
    )
    tracker = BudgetTracker.from_config(config, clock=clock, started_at=current[0])
    tracker.record_step("fp")
    tracker.record_llm_call()
    tracker.record_validation(False)
    current[0] += timedelta(minutes=2)
    assert tracker.elapsed() == timedelta(minutes=2)
    assert tracker.stop_reason() == "max_llm_calls"
    tracker.record_step("fp")
    assert tracker.stop_reason() == "max_steps"
    snapshot = tracker.to_snapshot()
    assert set(snapshot) == set(BudgetConfig.model_fields) | {
        "steps",
        "llm_calls",
        "consecutive_failures",
        "fingerprints",
    }
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task", config)
    store.save_budget_tracker(session_id, snapshot)
    restored = BudgetTracker.from_session(store.get_session(session_id), clock=clock, started_at=current[0])
    assert restored.to_snapshot() == snapshot
    assert restored.elapsed_seconds() == 0


def test_state_store_saves_budget_tracker_through_snapshot_protocol(
    store: StateStore, workspace: Path
) -> None:
    session_id = store.create_session(store.upsert_project(workspace, "Demo"), "task")
    tracker = BudgetTracker.from_config(BudgetConfig())
    tracker.record_step("fp")
    store.save_budget_tracker(session_id, tracker)
    saved = store.get_session(session_id).budget
    assert saved == tracker.to_snapshot()
    assert set(saved) == set(BudgetConfig.model_fields) | {
        "steps",
        "llm_calls",
        "consecutive_failures",
        "fingerprints",
    }

    class InvalidSnapshot:
        def to_snapshot(self) -> list[object]:
            return []

    with pytest.raises(StorageError, match="invalid_budget"):
        store.save_budget_tracker(session_id, InvalidSnapshot())


def test_consumed_approval_can_verify_full_dispatch_binding(
    store: StateStore, workspace: Path
) -> None:
    (workspace / "src").mkdir()
    pending = _pending(store, workspace)
    service = ApprovalService(store, Writer(), token_source=lambda: "05" * 32)
    approval = service.request(pending, workspace, expires_in=timedelta(minutes=10))
    service.approve(approval.id)
    grant = service.consume(approval.id, pending, workspace)
    assert store.is_consumed_approval(
        approval.id,
        pending.fingerprint,
        action_id=pending.action_id,
        session_id=pending.session_id,
        policy_decision_id=grant.policy_decision_id,
    )
    assert not store.is_consumed_approval(
        approval.id,
        pending.fingerprint,
        action_id="wrong-action",
        session_id=pending.session_id,
        policy_decision_id=grant.policy_decision_id,
    )
    assert not store.is_consumed_approval(
        approval.id,
        pending.fingerprint,
        action_id=pending.action_id,
        session_id="wrong-session",
        policy_decision_id=grant.policy_decision_id,
    )
    assert not store.is_consumed_approval(
        approval.id,
        pending.fingerprint,
        action_id=pending.action_id,
        session_id=pending.session_id,
        policy_decision_id="wrong-decision",
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_steps": True},
        {"max_steps": 41},
        {"steps": -1},
        {"fingerprints": {"fp": -1}},
        {"clock": 1},
        {"started_at": "not-a-datetime"},
        {"unknown": 1},
    ],
)
def test_budget_tracker_rejects_invalid_direct_construction(kwargs: dict[str, object]) -> None:
    values = BudgetConfig().model_dump()
    values.update(kwargs)
    with pytest.raises((TypeError, ValueError, ValidationError)):
        BudgetTracker(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("fingerprints", [{}, {1: 1}, {"": 1}, {"fp": True}])
def test_budget_tracker_rejects_corrupt_restored_snapshot(
    fingerprints: dict[object, object],
) -> None:
    snapshot = BudgetConfig().model_dump()
    snapshot.update({"steps": 0, "llm_calls": 0, "consecutive_failures": 0, "fingerprints": fingerprints})
    if fingerprints == {}:
        snapshot["unknown"] = 1
    with pytest.raises((TypeError, ValueError), match="invalid_budget_snapshot"):
        BudgetTracker.from_session(SimpleNamespace(budget=snapshot))
