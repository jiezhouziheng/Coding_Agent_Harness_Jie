"""Composition root for the control plane and governed engine."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent_harness.approvals import ApprovalService
from coding_agent_harness.audit import AuditWriter
from coding_agent_harness.config import (
    BudgetConfig,
    ConfigError,
    ResolvedConfig,
    UserConfig,
    load_project_config,
    load_user_config,
    resolve_config,
)
from coding_agent_harness.credentials import CredentialService, KeyringCredentialBackend
from coding_agent_harness.journal import ChangeJournal
from coding_agent_harness.memory import MemoryService
from coding_agent_harness.session_service import SessionService, WorkspaceLock
from coding_agent_harness.storage import StateStore


class EngineFactory:
    """Build one governed engine graph for each workspace session."""

    def __init__(
        self,
        store: Any,
        audit: Any,
        credentials: CredentialService,
        *,
        user_config: UserConfig,
        llm_factory: Any = None,
        clock: Any = None,
        app_data: Path,
    ) -> None:
        self.store = store
        self.audit = audit
        self.credentials = credentials
        self.user_config = user_config
        self.llm_factory = llm_factory
        self.clock = clock
        self.app_data = app_data
        self.journal = ChangeJournal(store, app_data / "backups")
        self.approvals = ApprovalService(store, audit, clock=clock)
        self.memory = MemoryService(store)
        self.lock_factory = WorkspaceLock
        self.changes = self.journal

    def create(
        self,
        *,
        workspace: Path | None = None,
        task: str | None = None,
        mock_script: Path | None = None,
        session_id: str | None = None,
        cli_budget: BudgetConfig | None = None,
    ) -> tuple[str, Any]:
        from coding_agent_harness.command_runner import CommandRunner
        from coding_agent_harness.context import ContextBuilder
        from coding_agent_harness.dispatcher import Dispatcher
        from coding_agent_harness.engine import HarnessEngine
        from coding_agent_harness.file_tools import FileTools
        from coding_agent_harness.llm import OpenAICompatibleClient, ScriptedMockLLM
        from coding_agent_harness.policy import PolicyContext, PolicyEngine, PolicyGateway
        from coding_agent_harness.validation import ValidationPipeline

        if session_id is not None:
            session = self.store.get_session(session_id)
            workspace = Path(self.store.get_project(session.project_id).canonical_path)
            task = session.task
        if workspace is None or task is None:
            raise ValueError("workspace_and_task_required")
        workspace = workspace.resolve(strict=False)
        project_config_path = workspace / "harness.toml"
        project_config = (
            load_project_config(project_config_path) if project_config_path.exists() else None
        )
        resolved = resolve_config(self.user_config, project_config, cli_budget)
        self._validate_project_config(workspace, resolved)

        llm: Any
        if mock_script is not None:
            try:
                payload = json.loads(mock_script.read_text(encoding="utf-8"))
                if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                    raise ValueError("invalid_mock_script")
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                raise ValueError("invalid_mock_script") from None
            llm = ScriptedMockLLM(payload)
        elif callable(self.llm_factory):
            llm = self.llm_factory()
        else:
            llm = OpenAICompatibleClient(
                resolved.provider_url,
                resolved.model,
                self.credentials.get_for_client(resolved.credential_profile),
            )

        project_id = self.store.upsert_project(workspace, workspace.name or "workspace")
        if session_id is None:
            session_id = self.store.create_session(project_id, task, resolved.budgets)
        runner = CommandRunner()
        validators = ValidationPipeline(
            runner,
            resolved.validators,
            command_timeout_seconds=resolved.budgets.command_timeout_seconds,
        )
        file_tools = FileTools(workspace, self.journal)
        dispatcher = Dispatcher(self.store, file_tools, runner, self.memory, workspace)
        policy = PolicyGateway(
            PolicyEngine(),
            self.store,
            self.audit,
            self.approvals,
            context=PolicyContext.for_workspace(workspace, budgets=resolved.budgets),
        )
        engine = HarnessEngine(
            llm=llm,
            store=self.store,
            policy=policy,
            dispatcher=dispatcher,
            validators=validators,
            workspace=workspace,
            context_builder=ContextBuilder(
                max_bytes=resolved.budgets.max_observation_bytes,
                source_roots=resolved.source_roots,
            ),
            clock=self.clock,
        )
        return session_id, engine

    @staticmethod
    def _validate_project_config(workspace: Path, resolved: ResolvedConfig) -> None:
        from coding_agent_harness.models import Decision, RunCommandAction
        from coding_agent_harness.policy import PolicyContext, PolicyEngine
        from coding_agent_harness.security import SecurityViolation, WorkspaceGuard

        try:
            guard = WorkspaceGuard(workspace)
            for source_root in resolved.source_roots:
                guard.resolve(source_root, must_exist=False)
        except SecurityViolation:
            raise ConfigError("unsafe_source_root") from None

        context = PolicyContext.for_workspace(workspace, budgets=resolved.budgets)
        policy = PolicyEngine()
        for validator in resolved.validators:
            action = RunCommandAction(
                program=validator.program,
                args=validator.args,
                cwd=".",
                timeout_seconds=resolved.budgets.command_timeout_seconds,
            )
            if policy.evaluate(action, context).decision is not Decision.ALLOW:
                raise ConfigError("unsafe_project_validator")


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
    demo: Any = None
    default_workspace: Path | None = None

    def run(
        self,
        *,
        task: str,
        workspace: Path | None = None,
        mock_script: Path | None = None,
        cli_budget: BudgetConfig | None = None,
    ) -> Any:
        if self.engine_factory is None:
            raise RuntimeError("engine_factory_unavailable")
        selected_workspace = workspace or self.default_workspace
        if selected_workspace is None:
            raise ValueError("workspace_and_task_required")
        session_id, engine = self.engine_factory.create(
            workspace=selected_workspace,
            task=task,
            mock_script=mock_script,
            cli_budget=cli_budget,
        )
        return engine.run(session_id)


def create_control_application(
    app_data: Path,
    *,
    credential_backend: Any = None,
    llm_factory: Any = None,
    clock: Any = None,
) -> HarnessApplication:
    app_data.mkdir(parents=True, exist_ok=True)
    config_path = app_data / "config.toml"
    user_config = load_user_config(config_path) if config_path.exists() else UserConfig()
    store = StateStore(app_data / "state.db")
    store.initialize()
    audit = AuditWriter(app_data / "audit" / "events.jsonl")
    credentials = CredentialService(credential_backend or KeyringCredentialBackend())
    engine_factory = EngineFactory(
        store,
        audit,
        credentials,
        user_config=user_config,
        llm_factory=llm_factory,
        clock=clock,
        app_data=app_data,
    )
    from coding_agent_harness.demo import DemoFacade
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
        demo=DemoFacade(engine_factory, app_data),
    )
