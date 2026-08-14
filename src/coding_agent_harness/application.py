"""Composition root for the control plane and governed engine."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent_harness.approvals import ApprovalService
from coding_agent_harness.audit import AuditWriter
from coding_agent_harness.credentials import CredentialService, KeyringCredentialBackend
from coding_agent_harness.journal import ChangeJournal
from coding_agent_harness.memory import MemoryService
from coding_agent_harness.session_service import SessionService, WorkspaceLock
from coding_agent_harness.storage import StateStore


class EngineFactory:
    """Build one governed engine graph for each workspace session."""

    def __init__(self, store: Any, audit: Any, credentials: CredentialService, *, llm_factory: Any = None, clock: Any = None, app_data: Path) -> None:
        self.store = store
        self.audit = audit
        self.credentials = credentials
        self.llm_factory = llm_factory
        self.clock = clock
        self.app_data = app_data
        self.journal = ChangeJournal(store, app_data / "backups")
        self.approvals = ApprovalService(store, audit, clock=clock)
        self.memory = MemoryService(store)
        self.lock_factory = WorkspaceLock
        self.changes = self.journal

    def create(self, *, workspace: Path | None = None, task: str | None = None, mock_script: Path | None = None, session_id: str | None = None) -> tuple[str, Any]:
        from coding_agent_harness.command_runner import CommandRunner
        from coding_agent_harness.context import ContextBuilder
        from coding_agent_harness.dispatcher import Dispatcher
        from coding_agent_harness.engine import HarnessEngine
        from coding_agent_harness.file_tools import FileTools
        from coding_agent_harness.llm import ScriptedMockLLM
        from coding_agent_harness.policy import PolicyEngine, PolicyGateway
        from coding_agent_harness.validation import ValidationPipeline

        if session_id is not None:
            session = self.store.get_session(session_id)
            workspace = Path(self.store.get_project(session.project_id).canonical_path)
            task = session.task
        if workspace is None or task is None:
            raise ValueError("workspace_and_task_required")
        workspace = workspace.resolve(strict=False)
        project_id = self.store.upsert_project(workspace, workspace.name or "workspace")
        if session_id is None:
            session_id = self.store.create_session(project_id, task)
        if mock_script is not None:
            try:
                payload = json.loads(mock_script.read_text(encoding="utf-8"))
                if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                    raise ValueError("invalid_mock_script")
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("invalid_mock_script") from error
            llm = ScriptedMockLLM(payload)
        else:
            llm = self.llm_factory() if callable(self.llm_factory) else ScriptedMockLLM([])
        runner = CommandRunner()
        validators = ValidationPipeline.default(runner)
        file_tools = FileTools(workspace, self.journal)
        dispatcher = Dispatcher(self.store, file_tools, runner, self.memory, workspace)
        policy = PolicyGateway(PolicyEngine(), self.store, self.audit, self.approvals)
        engine = HarnessEngine(llm=llm, store=self.store, policy=policy, dispatcher=dispatcher, validators=validators, workspace=workspace, context_builder=ContextBuilder(), clock=self.clock)
        return session_id, engine


@dataclass
class HarnessApplication:
    store: Any
    sessions: SessionService
    approvals: Any
    changes: Any
    credentials: CredentialService
    memory: MemoryService
    reports: Any
    engine_factory: Any
    default_workspace: Path | None = None

    def run(self, *, task: str, workspace: Path | None = None, mock_script: Path | None = None) -> Any:
        if self.engine_factory is None:
            raise RuntimeError("engine_factory_unavailable")
        selected_workspace = workspace or self.default_workspace
        if selected_workspace is None:
            raise ValueError("workspace_and_task_required")
        session_id, engine = self.engine_factory.create(workspace=selected_workspace, task=task, mock_script=mock_script)
        return engine.run(session_id)


def create_control_application(
    app_data: Path,
    *,
    credential_backend: Any = None,
    llm_factory: Any = None,
    clock: Any = None,
) -> HarnessApplication:
    app_data.mkdir(parents=True, exist_ok=True)
    store = StateStore(app_data / "state.db")
    store.initialize()
    audit = AuditWriter(app_data / "audit" / "events.jsonl")
    credentials = CredentialService(credential_backend or KeyringCredentialBackend())
    engine_factory = EngineFactory(store, audit, credentials, llm_factory=llm_factory, clock=clock, app_data=app_data)
    from coding_agent_harness.reporting import ReportExporter
    sessions = SessionService(store, engine_factory.journal, engine_factory.approvals, WorkspaceLock, app_data / "locks")
    sessions.engine_factory = engine_factory
    return HarnessApplication(
        store=store,
        sessions=sessions,
        approvals=engine_factory.approvals,
        changes=engine_factory.changes,
        credentials=credentials,
        memory=engine_factory.memory,
        reports=ReportExporter(store),
        engine_factory=engine_factory,
    )
