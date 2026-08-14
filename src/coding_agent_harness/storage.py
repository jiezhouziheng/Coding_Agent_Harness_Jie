"""SQLite-backed authoritative state for governed coding sessions."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coding_agent_harness.config import BudgetConfig
from coding_agent_harness.models import (
    Action,
    ApprovalStatus,
    Decision,
    Observation,
    SessionStatus,
    StrictModel,
    ValidationResult,
    parse_action,
    validate_transition,
)


class StorageError(RuntimeError):
    """Raised when authoritative state cannot be read or changed."""


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    canonical_path: str
    display_name: str


@dataclass(frozen=True)
class SessionRecord:
    id: str
    project_id: str
    task: str
    status: SessionStatus
    budget: dict[str, object]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class StoredAction:
    id: str
    session_id: str
    step: int
    action: Action
    fingerprint: str
    created_at: str


@dataclass(frozen=True)
class StoredPolicyDecision:
    id: str
    action_id: str
    decision: Decision
    reason_code: str
    rule_source: str
    created_at: str


class ApprovalRecord(StrictModel):
    id: str
    action_id: str
    session_id: str
    fingerprint: str
    workspace_fingerprint: str
    nonce_digest: str
    status: ApprovalStatus
    created_at: str
    expires_at: str
    decided_at: str | None
    consumed_at: str | None


@dataclass(frozen=True)
class AuditOutboxRecord:
    sequence: int
    event: dict[str, object]
    created_at: str
    flushed_at: str | None


@dataclass(frozen=True)
class ChangeRecord:
    id: str
    session_id: str
    relative_path: str
    operation: str
    before_digest: str | None
    after_digest: str | None
    backup_ref: str | None
    sequence: int
    created_at: str


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    project_id: str
    source_session_id: str
    memory_type: str
    content: str
    evidence_action_id: str | None
    tags: tuple[str, ...]
    status: str
    created_at: str
    updated_at: str


_ACTIVE_STATUSES = tuple(
    status.value
    for status in (
        SessionStatus.CREATED,
        SessionStatus.RUNNING,
        SessionStatus.PAUSED_APPROVAL,
        SessionStatus.PAUSED_LIMIT_REACHED,
        SessionStatus.PAUSED_PROTOCOL_ERROR,
        SessionStatus.PAUSED_WORKSPACE_DRIFT,
        SessionStatus.PAUSED_INTERNAL_ERROR,
        SessionStatus.NEEDS_USER_DECISION,
    )
)


class StateStore:
    def __init__(self, path: Path, clock: Callable[[], datetime] | None = None) -> None:
        self.path = path
        self._clock = clock or (lambda: datetime.now(UTC))
        self._connection: sqlite3.Connection | None = None
        self._transaction_depth = 0

    def initialize(self) -> None:
        if self._connection is not None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, isolation_level=None)
            self._connection = connection
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            with self.transaction():
                for statement in _SCHEMA:
                    connection.execute(statement)
                self._migrate_approvals(connection)
        except StorageError:
            self._discard_connection()
            raise
        except (OSError, sqlite3.Error) as error:
            self._discard_connection()
            raise StorageError("storage_initialize_failed") from error

    def close(self) -> None:
        if self._connection is None:
            return
        connection, self._connection = self._connection, None
        self._transaction_depth = 0
        try:
            connection.close()
        except sqlite3.Error as error:
            raise StorageError("storage_close_failed") from error

    def _discard_connection(self) -> None:
        try:
            self.close()
        except StorageError:
            pass

    @staticmethod
    def _migrate_approvals(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
        }
        if "workspace_fingerprint" not in columns:
            connection.execute(
                "ALTER TABLE approvals ADD COLUMN workspace_fingerprint "
                "TEXT NOT NULL DEFAULT ''"
            )
            connection.execute(
                "UPDATE approvals SET status = ?, decided_at = COALESCE(decided_at, created_at) "
                "WHERE workspace_fingerprint = '' AND status IN (?, ?, ?)",
                (
                    ApprovalStatus.INVALIDATED.value,
                    ApprovalStatus.PROPOSED.value,
                    ApprovalStatus.PENDING.value,
                    ApprovalStatus.APPROVED.value,
                ),
            )

    @property
    def in_transaction(self) -> bool:
        return self._transaction_depth > 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        connection = self._require_connection()
        outermost = self._transaction_depth == 0
        if outermost:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                raise StorageError("storage_begin_failed") from error
        self._transaction_depth += 1
        try:
            yield
        except BaseException:
            self._transaction_depth -= 1
            if outermost:
                try:
                    connection.rollback()
                except sqlite3.Error as error:
                    self._discard_connection()
                    raise StorageError("storage_rollback_failed") from error
            raise
        else:
            self._transaction_depth -= 1
            if outermost:
                try:
                    connection.commit()
                except sqlite3.Error as error:
                    try:
                        connection.rollback()
                    except sqlite3.Error as rollback_error:
                        self._discard_connection()
                        raise StorageError("storage_rollback_failed") from rollback_error
                    raise StorageError("storage_commit_failed") from error

    def upsert_project(self, path: Path, display_name: str) -> str:
        try:
            canonical_path = str(path.resolve(strict=False))
        except (OSError, RuntimeError):
            raise StorageError("invalid_project_path") from None
        if not display_name:
            raise StorageError("invalid_project")
        with self._write_transaction():
            row = self._execute("SELECT id FROM projects WHERE canonical_path = ?", (canonical_path,)).fetchone()
            if row:
                self._execute("UPDATE projects SET display_name = ? WHERE id = ?", (display_name, row["id"]))
                return str(row["id"])
            project_id = _new_id()
            self._execute(
                "INSERT INTO projects(id, canonical_path, display_name, created_at) VALUES (?, ?, ?, ?)",
                (project_id, canonical_path, display_name, self._now()),
            )
            return project_id

    def get_project(self, project_id: str) -> ProjectRecord:
        row = self._execute("SELECT id, canonical_path, display_name FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise StorageError("project_not_found")
        return ProjectRecord(str(row["id"]), str(row["canonical_path"]), str(row["display_name"]))

    def create_session(
        self, project_id: str, task: str, budget: BudgetConfig | dict[str, object] | None = None
    ) -> str:
        if not task:
            raise StorageError("invalid_session_task")
        try:
            if budget is None:
                budget_data = BudgetConfig().model_dump(mode="json")
            elif isinstance(budget, BudgetConfig):
                budget_data = budget.model_dump(mode="json")
            elif isinstance(budget, dict):
                budget_data = _validate_budget_snapshot(budget, require_complete=False)
            else:
                raise ValueError("unsupported budget")
        except (TypeError, ValueError) as error:
            raise StorageError("invalid_budget") from error
        with self._write_transaction():
            if self._execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise StorageError("project_not_found")
            active = self._execute(
                "SELECT 1 FROM sessions WHERE project_id = ? AND status IN "
                "('CREATED','RUNNING','PAUSED_APPROVAL','PAUSED_LIMIT_REACHED',"
                "'PAUSED_PROTOCOL_ERROR','PAUSED_WORKSPACE_DRIFT',"
                "'PAUSED_INTERNAL_ERROR','NEEDS_USER_DECISION')",
                (project_id,),
            ).fetchone()
            if active is not None:
                raise StorageError("active_session_exists")
            session_id, now = _new_id(), self._now()
            self._execute(
                "INSERT INTO sessions(id, project_id, task, status, budget_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, project_id, task, SessionStatus.CREATED.value, _dump(budget_data), now, now),
            )
            return session_id

    def get_session(self, session_id: str) -> SessionRecord:
        row = self._execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise StorageError("session_not_found")
        try:
            budget = _validate_budget_snapshot(
                _load_object(row["budget_json"]), require_complete=True
            )
            return SessionRecord(
                str(row["id"]), str(row["project_id"]), str(row["task"]),
                SessionStatus(row["status"]), budget, str(row["created_at"]), str(row["updated_at"])
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise StorageError("corrupt_session") from error

    def list_sessions(self) -> tuple[SessionRecord, ...]:
        rows = self._execute("SELECT id FROM sessions ORDER BY created_at").fetchall()
        return tuple(self.get_session(str(row["id"])) for row in rows)

    def transition_session(self, session_id: str, target: SessionStatus) -> SessionRecord:
        if not isinstance(target, SessionStatus):
            raise StorageError("illegal_session_transition")
        with self._write_transaction():
            session = self.get_session(session_id)
            if not validate_transition(session.status, target):
                raise StorageError("illegal_session_transition")
            self._execute("UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?", (target.value, self._now(), session_id))
            return self.get_session(session_id)

    def save_budget_tracker(self, session_id: str, tracker_or_mapping: object) -> None:
        data = _mapping(tracker_or_mapping)
        with self._write_transaction():
            current = self.get_session(session_id).budget
            updated = dict(current)
            updated.update(data)
            try:
                snapshot = _validate_budget_snapshot(updated, require_complete=True)
            except (TypeError, ValueError) as error:
                raise StorageError("invalid_budget") from error
            self._execute("UPDATE sessions SET budget_json = ?, updated_at = ? WHERE id = ?", (_dump(snapshot), self._now(), session_id))

    def record_action(self, session_id: str, step: int, action: Action, fingerprint: str) -> str:
        if step < 1 or not fingerprint:
            raise StorageError("invalid_action")
        try:
            normalized = parse_action(action)
        except Exception as error:
            raise StorageError("invalid_action") from error
        with self._write_transaction():
            self._require_session(session_id)
            action_id = _new_id()
            self._execute(
                "INSERT INTO actions(id, session_id, step, tool, normalized_json, fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (action_id, session_id, step, normalized.tool, _dump(normalized.model_dump(mode="json")), fingerprint, self._now()),
            )
            return action_id

    def create_change(
        self,
        *,
        session_id: str,
        relative_path: str,
        operation: str,
        before_digest: str | None,
        backup_ref: str | None,
        after_digest: str | None = None,
    ) -> ChangeRecord:
        if not session_id or not relative_path or operation not in {"create", "modify", "delete"}:
            raise StorageError("invalid_change")
        with self._write_transaction():
            self._require_session(session_id)
            row = self._execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM changes WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            sequence = int(row["next_sequence"])
            change_id = _new_id()
            self._execute(
                "INSERT INTO changes(id, session_id, relative_path, operation, before_digest, after_digest, backup_ref, sequence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (change_id, session_id, relative_path, operation, before_digest, after_digest, backup_ref, sequence, self._now()),
            )
            return self.get_change(change_id)

    def get_change(self, change_id: str) -> ChangeRecord:
        row = self._execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()
        if row is None:
            raise StorageError("change_not_found")
        return ChangeRecord(
            id=str(row["id"]), session_id=str(row["session_id"]),
            relative_path=str(row["relative_path"]), operation=str(row["operation"]),
            before_digest=None if row["before_digest"] is None else str(row["before_digest"]),
            after_digest=None if row["after_digest"] is None else str(row["after_digest"]),
            backup_ref=None if row["backup_ref"] is None else str(row["backup_ref"]),
            sequence=int(row["sequence"]), created_at=str(row["created_at"]),
        )

    def list_changes(self, session_id: str) -> tuple[ChangeRecord, ...]:
        self._require_session(session_id)
        rows = self._execute("SELECT id FROM changes WHERE session_id = ? ORDER BY sequence", (session_id,)).fetchall()
        return tuple(self.get_change(str(row["id"])) for row in rows)

    def finish_change(self, change_id: str, *, after_digest: str) -> ChangeRecord:
        if not after_digest:
            raise StorageError("invalid_change")
        with self._write_transaction():
            cursor = self._execute("UPDATE changes SET after_digest = ? WHERE id = ?", (after_digest, change_id))
            if cursor.rowcount != 1:
                raise StorageError("change_not_found")
            return self.get_change(change_id)

    def create_memory(self, project_id: str, session_id: str, memory_type: str, content: str, evidence_action_id: str | None, tags: tuple[str, ...], status: str) -> MemoryRecord:
        if not project_id or not session_id or not memory_type or not content or status not in {"CANDIDATE", "ACTIVE"}:
            raise StorageError("invalid_memory")
        with self._write_transaction():
            self._require_session(session_id)
            record_id = _new_id()
            now = self._now()
            self._execute("INSERT INTO memory_entries(id, project_id, source_session_id, memory_type, content, evidence_action_id, tags_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (record_id, project_id, session_id, memory_type, content, evidence_action_id, _dump(list(tags)), status, now, now))
            return self.get_memory(record_id)

    def get_memory(self, memory_id: str) -> MemoryRecord:
        row = self._execute("SELECT * FROM memory_entries WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise StorageError("memory_not_found")
        tags = json.loads(str(row["tags_json"]))
        if not isinstance(tags, list) or any(not isinstance(item, str) for item in tags):
            raise StorageError("corrupt_memory")
        return MemoryRecord(str(row["id"]), str(row["project_id"]), str(row["source_session_id"]), str(row["memory_type"]), str(row["content"]), None if row["evidence_action_id"] is None else str(row["evidence_action_id"]), tuple(tags), str(row["status"]), str(row["created_at"]), str(row["updated_at"]))

    def transition_memory(self, memory_id: str, allowed: set[str], target: str) -> MemoryRecord:
        with self._write_transaction():
            current = self.get_memory(memory_id)
            if current.status not in allowed:
                raise StorageError("illegal_memory_transition")
            self._execute("UPDATE memory_entries SET status = ?, updated_at = ? WHERE id = ?", (target, self._now(), memory_id))
            return self.get_memory(memory_id)

    def search_active_memory(self, project_id: str, keywords: tuple[str, ...], limit: int) -> tuple[MemoryRecord, ...]:
        rows = self._execute("SELECT id FROM memory_entries WHERE project_id = ? AND status = 'ACTIVE' ORDER BY rowid DESC", (project_id,)).fetchall()
        values = tuple(self.get_memory(str(row["id"])) for row in rows)
        if not keywords:
            return values[: min(limit, 5)]
        terms = tuple(term.casefold() for term in keywords)
        matching = tuple(item for item in values if any(term in (item.content + " " + " ".join(item.tags)).casefold() for term in terms))
        return matching[: min(limit, 5)]

    def action_has_successful_validation(self, session_id: str, action_id: str) -> bool:
        row = self._execute("SELECT 1 FROM validations WHERE session_id = ? AND status = 'passed' AND (id = ? OR validator_id = ?)", (session_id, action_id, action_id)).fetchone()
        return row is not None

    def get_action(self, action_id: str) -> StoredAction:
        row = self._execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        if row is None:
            raise StorageError("action_not_found")
        try:
            action = parse_action(_load_object(row["normalized_json"]))
            if row["tool"] != action.tool:
                raise ValueError("action tool mismatch")
            return StoredAction(str(row["id"]), str(row["session_id"]), int(row["step"]), action, str(row["fingerprint"]), str(row["created_at"]))
        except Exception as error:
            raise StorageError("corrupt_action") from error

    def record_policy_decision(self, action_id: str, *, decision: Decision, reason_code: str, rule_source: str) -> str:
        if not isinstance(decision, Decision) or not reason_code or not rule_source:
            raise StorageError("invalid_policy_decision")
        with self._write_transaction():
            if self._execute("SELECT 1 FROM actions WHERE id = ?", (action_id,)).fetchone() is None:
                raise StorageError("action_not_found")
            if self._execute("SELECT 1 FROM policy_decisions WHERE action_id = ?", (action_id,)).fetchone() is not None:
                raise StorageError("policy_decision_exists")
            decision_id = _new_id()
            self._execute("INSERT INTO policy_decisions(id, action_id, decision, reason_code, rule_source, created_at) VALUES (?, ?, ?, ?, ?, ?)", (decision_id, action_id, decision.value, reason_code, rule_source, self._now()))
            return decision_id

    def get_policy_decision(self, decision_id: str) -> StoredPolicyDecision:
        row = self._execute("SELECT * FROM policy_decisions WHERE id = ?", (decision_id,)).fetchone()
        if row is None:
            raise StorageError("policy_decision_not_found")
        try:
            return StoredPolicyDecision(str(row["id"]), str(row["action_id"]), Decision(row["decision"]), str(row["reason_code"]), str(row["rule_source"]), str(row["created_at"]))
        except ValueError as error:
            raise StorageError("corrupt_policy_decision") from error

    def get_policy_decision_for_action(self, action_id: str) -> StoredPolicyDecision:
        row = self._execute(
            "SELECT id FROM policy_decisions WHERE action_id = ?", (action_id,)
        ).fetchone()
        if row is None:
            raise StorageError("policy_decision_not_found")
        return self.get_policy_decision(str(row["id"]))

    def create_approval(
        self,
        *,
        approval_id: str,
        action_id: str,
        session_id: str,
        fingerprint: str,
        workspace_fingerprint: str,
        nonce_digest: str,
        expires_at: str,
    ) -> ApprovalRecord:
        if not all(
            (
                approval_id,
                action_id,
                session_id,
                fingerprint,
                workspace_fingerprint,
                nonce_digest,
                expires_at,
            )
        ):
            raise StorageError("invalid_approval")
        with self._write_transaction():
            action = self.get_action(action_id)
            if action.session_id != session_id or action.fingerprint != fingerprint:
                raise StorageError("approval_binding_mismatch")
            if self._execute(
                "SELECT 1 FROM approvals WHERE action_id = ?", (action_id,)
            ).fetchone() is not None:
                raise StorageError("approval_exists")
            self._execute(
                "INSERT INTO approvals("
                "id, action_id, session_id, fingerprint, workspace_fingerprint, "
                "nonce_digest, status, created_at, expires_at, decided_at, consumed_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    approval_id,
                    action_id,
                    session_id,
                    fingerprint,
                    workspace_fingerprint,
                    nonce_digest,
                    ApprovalStatus.PROPOSED.value,
                    self._now(),
                    expires_at,
                ),
            )
            return self.get_approval(approval_id)

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        row = self._execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise StorageError("approval_not_found")
        try:
            return ApprovalRecord(
                id=str(row["id"]),
                action_id=str(row["action_id"]),
                session_id=str(row["session_id"]),
                fingerprint=str(row["fingerprint"]),
                workspace_fingerprint=str(row["workspace_fingerprint"]),
                nonce_digest=str(row["nonce_digest"]),
                status=ApprovalStatus(row["status"]),
                created_at=str(row["created_at"]),
                expires_at=str(row["expires_at"]),
                decided_at=None if row["decided_at"] is None else str(row["decided_at"]),
                consumed_at=None if row["consumed_at"] is None else str(row["consumed_at"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise StorageError("corrupt_approval") from error

    def get_approval_for_action(self, action_id: str) -> ApprovalRecord:
        row = self._execute(
            "SELECT id FROM approvals WHERE action_id = ?", (action_id,)
        ).fetchone()
        if row is None:
            raise StorageError("approval_not_found")
        return self.get_approval(str(row["id"]))

    def list_pending_approvals(
        self, session_id: str | None = None
    ) -> tuple[ApprovalRecord, ...]:
        if session_id is None:
            rows = self._execute(
                "SELECT id FROM approvals WHERE status = ? ORDER BY rowid",
                (ApprovalStatus.PENDING.value,),
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT id FROM approvals WHERE status = ? AND session_id = ? ORDER BY rowid",
                (ApprovalStatus.PENDING.value, session_id),
            ).fetchall()
        return tuple(self.get_approval(str(row["id"])) for row in rows)

    def get_latest_approval_for_session(self, session_id: str) -> ApprovalRecord:
        row = self._execute(
            "SELECT id FROM approvals WHERE session_id = ? ORDER BY rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            raise StorageError("approval_not_found")
        return self.get_approval(str(row["id"]))

    def transition_approval(
        self,
        approval_id: str,
        *,
        target: ApprovalStatus,
    ) -> ApprovalRecord:
        if not isinstance(target, ApprovalStatus):
            raise StorageError("illegal_approval_transition")
        with self._write_transaction():
            current = self.get_approval(approval_id)
            if (current.status, target) not in _APPROVAL_TRANSITIONS:
                raise StorageError("illegal_approval_transition")
            decided_at = current.decided_at
            consumed_at = current.consumed_at
            now = self._now()
            if target in {
                ApprovalStatus.APPROVED,
                ApprovalStatus.DENIED,
                ApprovalStatus.EXPIRED,
                ApprovalStatus.INVALIDATED,
            }:
                decided_at = now
            if target is ApprovalStatus.CONSUMED:
                consumed_at = now
            cursor = self._execute(
                "UPDATE approvals SET status = ?, decided_at = ?, consumed_at = ? "
                "WHERE id = ? AND status = ?",
                (target.value, decided_at, consumed_at, approval_id, current.status.value),
            )
            if cursor.rowcount != 1:
                raise StorageError("approval_transition_conflict")
            return self.get_approval(approval_id)

    def is_consumed_approval(
        self,
        approval_id: str | None,
        fingerprint: str,
        *,
        action_id: str | None = None,
        session_id: str | None = None,
        policy_decision_id: str | None = None,
    ) -> bool:
        if approval_id is None:
            return False
        row = self._execute(
            """SELECT 1
               FROM approvals AS approval
               JOIN actions AS action ON action.id = approval.action_id
               JOIN policy_decisions AS policy ON policy.action_id = action.id
               WHERE approval.id = ? AND approval.fingerprint = ? AND approval.status = ?
                 AND policy.decision = ?
                 AND (? IS NULL OR approval.action_id = ?)
                 AND (? IS NULL OR action.session_id = ?)
                 AND (? IS NULL OR policy.id = ?)""",
            (
                approval_id,
                fingerprint,
                ApprovalStatus.CONSUMED.value,
                Decision.REQUIRE_APPROVAL.value,
                action_id,
                action_id,
                session_id,
                session_id,
                policy_decision_id,
                policy_decision_id,
            ),
        ).fetchone()
        return row is not None

    def record_observation(self, session_id: str, observation: Observation) -> str:
        if not isinstance(observation, Observation):
            raise StorageError("invalid_observation")
        with self._write_transaction():
            self._require_session(session_id)
            if observation.action_id:
                row = self._execute("SELECT session_id FROM actions WHERE id = ?", (observation.action_id,)).fetchone()
                if row is None or row["session_id"] != session_id:
                    raise StorageError("observation_action_mismatch")
            record_id = _new_id()
            self._execute(
                "INSERT INTO observations(id, session_id, action_id, category, summary, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record_id, session_id, observation.action_id, observation.category, observation.summary, observation.evidence, self._now()),
            )
            return record_id

    def latest_observation(self, session_id: str) -> Observation | None:
        self._require_session(session_id)
        row = self._execute("SELECT action_id, category, summary, evidence FROM observations WHERE session_id = ? ORDER BY rowid DESC LIMIT 1", (session_id,)).fetchone()
        if row is None:
            return None
        try:
            return Observation.model_validate(dict(row))
        except Exception as error:
            raise StorageError("corrupt_observation") from error

    def query_safe_report_rows(self, session_id: str) -> dict[str, object]:
        """Project persisted state into the report schema's explicit allowlist."""
        session = self.get_session(session_id)
        project = self.get_project(session.project_id)
        action_rows = self._execute(
            "SELECT id, tool, normalized_json FROM actions "
            "WHERE session_id = ? ORDER BY step",
            (session_id,),
        ).fetchall()
        actions: list[dict[str, object]] = []
        approvals: list[dict[str, object]] = []
        for row in action_rows:
            action = self.get_action(str(row["id"]))
            decision = self.get_policy_decision_for_action(action.id)
            safe_action: dict[str, object] = {
                "tool": action.action.tool,
                "decision": decision.decision.value,
                "reason_code": decision.reason_code,
            }
            if hasattr(action.action, "path"):
                raw_path = str(action.action.path)
                safe_action["path"] = _relative_report_path(raw_path, project.canonical_path)
            actions.append(safe_action)
            try:
                approval = self.get_approval_for_action(action.id)
            except StorageError as error:
                if str(error) != "approval_not_found":
                    raise
                approval = None
            if approval is not None:
                approvals.append({"status": approval.status.value})
        validations = [
            {
                "validator_id": result.validator_id,
                "stage": result.stage,
                "status": result.status,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "summary": _safe_summary(result.summary),
            }
            for result in self.list_validations(session_id)
        ]
        return {
            "schema_version": "1.0",
            "session_id": session_id,
            "project": {"display_name": project.display_name},
            "status": session.status.value,
            "actions": tuple(actions),
            "approvals": tuple(approvals),
            "validations": tuple(validations),
            "final_summary": "",
        }

    def query_context_inputs(self, session_id: str, *, memory_limit: int = 5) -> dict[str, object]:
        """Return bounded, redaction-ready inputs for the model context."""
        session = self.get_session(session_id)
        latest = self.latest_observation(session_id)
        validations = self.list_validations(session_id)
        memories = self.search_active_memory(session.project_id, (), min(memory_limit, 5))
        return {
            "task": session.task,
            "completion_criteria": "all required validators pass",
            "policy_summary": "actions are evaluated by the governed policy gateway",
            "validator_summary": validations[-1].summary if validations else "",
            "current_failure": latest.summary if latest and latest.category != "success" else "",
            "observations": ((f"{latest.category}: {latest.summary}",) if latest is not None else ()),
            "memories": tuple(memory.content for memory in memories),
        }

    def record_validation(self, session_id: str, result: ValidationResult) -> str:
        return self.record_validations(session_id, [result])[0]

    def record_validations(self, session_id: str, results: object) -> list[str]:
        if not isinstance(results, Iterable):
            raise StorageError("invalid_validation")
        values = list(results)
        if any(not isinstance(result, ValidationResult) for result in values):
            raise StorageError("invalid_validation")
        with self._write_transaction():
            self._require_session(session_id)
            ids: list[str] = []
            for result in values:
                record_id = _new_id()
                self._execute(
                    "INSERT INTO validations(id, session_id, validator_id, stage, status, exit_code, duration_ms, summary, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (record_id, session_id, result.validator_id, result.stage, result.status, result.exit_code, result.duration_ms, result.summary, result.evidence, self._now()),
                )
                ids.append(record_id)
            return ids

    def list_validations(self, session_id: str) -> tuple[ValidationResult, ...]:
        self._require_session(session_id)
        rows = self._execute("SELECT validator_id, stage, status, exit_code, duration_ms, summary, evidence FROM validations WHERE session_id = ? ORDER BY rowid", (session_id,)).fetchall()
        try:
            return tuple(ValidationResult.model_validate(dict(row)) for row in rows)
        except Exception as error:
            raise StorageError("corrupt_validation") from error

    def enqueue_audit(self, event: dict[str, object]) -> int:
        if not isinstance(event, dict):
            raise StorageError("invalid_audit_event")
        with self._write_transaction():
            cursor = self._execute("INSERT INTO audit_outbox(event_json, created_at) VALUES (?, ?)", (_dump(event), self._now()))
            if cursor.lastrowid is None:
                raise StorageError("audit_enqueue_failed")
            return int(cursor.lastrowid)

    def list_pending_audit(self) -> tuple[AuditOutboxRecord, ...]:
        rows = self._execute("SELECT * FROM audit_outbox WHERE flushed_at IS NULL ORDER BY sequence").fetchall()
        try:
            return tuple(AuditOutboxRecord(int(row["sequence"]), _load_object(row["event_json"]), str(row["created_at"]), row["flushed_at"]) for row in rows)
        except (TypeError, json.JSONDecodeError) as error:
            raise StorageError("corrupt_audit_outbox") from error

    def flush_audit(self, writer: Any) -> int:
        if self._transaction_depth != 0:
            raise StorageError("audit_flush_requires_independent_transaction")
        count = 0
        while True:
            with self.transaction():
                row = self._execute(
                    "SELECT sequence, event_json FROM audit_outbox "
                    "WHERE flushed_at IS NULL ORDER BY sequence LIMIT 1"
                ).fetchone()
                if row is None:
                    return count
                try:
                    event = _load_object(row["event_json"])
                except (TypeError, json.JSONDecodeError) as error:
                    raise StorageError("corrupt_audit_outbox") from error
                sequence = int(row["sequence"])
                event["sequence"] = sequence
                writer.append(event)
                cursor = self._execute(
                    "UPDATE audit_outbox SET flushed_at = ? "
                    "WHERE sequence = ? AND flushed_at IS NULL",
                    (self._now(), sequence),
                )
                if cursor.rowcount != 1:
                    raise StorageError("audit_flush_conflict")
                count += 1

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        if self._transaction_depth:
            yield
        else:
            with self.transaction():
                yield

    def _require_session(self, session_id: str) -> None:
        if self._execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
            raise StorageError("session_not_found")

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StorageError("storage_closed")
        return self._connection

    def _execute(self, statement: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        try:
            return self._require_connection().execute(statement, parameters)
        except StorageError:
            raise
        except sqlite3.Error as error:
            raise StorageError("storage_operation_failed") from error

    def _now(self) -> str:
        return self._clock().astimezone(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _relative_report_path(raw_path: str, project_path: str) -> str:
    try:
        candidate = Path(raw_path)
        root = Path(project_path).resolve(strict=False)
        resolved = candidate.resolve(strict=False) if candidate.is_absolute() else candidate
        if candidate.is_absolute():
            return resolved.relative_to(root).as_posix()
        return candidate.as_posix()
    except (OSError, RuntimeError, ValueError):
        return "<REDACTED_PATH>"


def _safe_summary(_value: str) -> str:
    return ""


def _dump(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise StorageError("storage_serialization_failed") from error


def _load_object(value: object) -> dict[str, object]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise TypeError("expected JSON object")
    return decoded


def _mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, BudgetConfig):
        return value.model_dump(mode="json")
    try:
        snapshotter = getattr(value, "to_snapshot", None)
        if callable(snapshotter):
            snapshot = snapshotter()
            if not isinstance(snapshot, Mapping):
                raise StorageError("invalid_budget")
            return dict(snapshot)
    except StorageError:
        raise
    except Exception as error:
        raise StorageError("invalid_budget") from error
    if is_dataclass(value) and not isinstance(value, type):
        return dict(asdict(value))
    raise StorageError("invalid_budget")


_RUNTIME_BUDGET_FIELDS = frozenset(
    {"steps", "llm_calls", "consecutive_failures", "fingerprints"}
)


_APPROVAL_TRANSITIONS = frozenset(
    {
        (ApprovalStatus.PROPOSED, ApprovalStatus.PENDING),
        (ApprovalStatus.PENDING, ApprovalStatus.APPROVED),
        (ApprovalStatus.PENDING, ApprovalStatus.DENIED),
        (ApprovalStatus.PENDING, ApprovalStatus.EXPIRED),
        (ApprovalStatus.PENDING, ApprovalStatus.INVALIDATED),
        (ApprovalStatus.APPROVED, ApprovalStatus.CONSUMED),
        (ApprovalStatus.APPROVED, ApprovalStatus.EXPIRED),
        (ApprovalStatus.APPROVED, ApprovalStatus.INVALIDATED),
    }
)


def _validate_budget_snapshot(
    value: Mapping[str, object], *, require_complete: bool
) -> dict[str, object]:
    if any(not isinstance(key, str) for key in value):
        raise TypeError("budget keys must be strings")
    payload = {str(key): item for key, item in value.items()}
    config_fields = frozenset(BudgetConfig.model_fields)
    if set(payload) - config_fields - _RUNTIME_BUDGET_FIELDS:
        raise ValueError("unknown budget field")
    config_payload = {name: payload[name] for name in config_fields if name in payload}
    if require_complete and set(config_payload) != config_fields:
        raise ValueError("incomplete budget config")
    config = BudgetConfig.model_validate(config_payload).model_dump(mode="json")
    for name in ("steps", "llm_calls", "consecutive_failures"):
        if name in payload:
            item = payload[name]
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise TypeError("invalid runtime counter")
            config[name] = item
    if "fingerprints" in payload:
        fingerprints = payload["fingerprints"]
        if not isinstance(fingerprints, dict) or any(
            not isinstance(key, str)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            for key, count in fingerprints.items()
        ):
            raise TypeError("invalid fingerprints")
        config["fingerprints"] = dict(fingerprints)
    return config


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, canonical_path TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), task TEXT NOT NULL CHECK(length(task) > 0), status TEXT NOT NULL, budget_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), step INTEGER NOT NULL CHECK(step >= 1), tool TEXT NOT NULL, normalized_json TEXT NOT NULL, fingerprint TEXT NOT NULL CHECK(length(fingerprint) > 0), created_at TEXT NOT NULL, UNIQUE(session_id, step))",
    "CREATE TABLE IF NOT EXISTS policy_decisions (id TEXT PRIMARY KEY, action_id TEXT NOT NULL UNIQUE REFERENCES actions(id), decision TEXT NOT NULL, reason_code TEXT NOT NULL CHECK(length(reason_code) > 0), rule_source TEXT NOT NULL CHECK(length(rule_source) > 0), created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, action_id TEXT NOT NULL UNIQUE REFERENCES actions(id), session_id TEXT NOT NULL REFERENCES sessions(id), fingerprint TEXT NOT NULL, workspace_fingerprint TEXT NOT NULL, nonce_digest TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, decided_at TEXT, consumed_at TEXT)",
    "CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), action_id TEXT REFERENCES actions(id), category TEXT NOT NULL, summary TEXT NOT NULL, evidence TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS validations (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), validator_id TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL, exit_code INTEGER, duration_ms INTEGER NOT NULL, summary TEXT NOT NULL, evidence TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS memory_entries (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), source_session_id TEXT NOT NULL REFERENCES sessions(id), memory_type TEXT NOT NULL, content TEXT NOT NULL, evidence_action_id TEXT REFERENCES actions(id), tags_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS changes (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), relative_path TEXT NOT NULL, operation TEXT NOT NULL, before_digest TEXT, after_digest TEXT, backup_ref TEXT, sequence INTEGER NOT NULL, created_at TEXT NOT NULL, UNIQUE(session_id, sequence))",
    "CREATE TABLE IF NOT EXISTS audit_outbox (sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_json TEXT NOT NULL, created_at TEXT NOT NULL, flushed_at TEXT)",
    "CREATE UNIQUE INDEX IF NOT EXISTS one_active_writer ON sessions(project_id) WHERE status IN ('CREATED','RUNNING','PAUSED_APPROVAL','PAUSED_LIMIT_REACHED','PAUSED_PROTOCOL_ERROR','PAUSED_WORKSPACE_DRIFT','PAUSED_INTERNAL_ERROR','NEEDS_USER_DECISION')",
)
