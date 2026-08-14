from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_agent_harness.models import Observation, ProposeMemoryAction, RunCommandAction
from coding_agent_harness.policy import AuthorizationGrant
from coding_agent_harness.security import action_fingerprint


class DispatchError(RuntimeError):
    pass


class Dispatcher:
    def __init__(
        self,
        store: Any,
        file_tools: Any,
        command_runner: Any,
        memory_service: Any,
        workspace: Path,
    ) -> None:
        self.store = store
        self.file_tools = file_tools
        self.command_runner = command_runner
        self.memory_service = memory_service
        self.workspace = workspace

    def execute(self, grant: AuthorizationGrant) -> Observation:
        if not isinstance(grant, AuthorizationGrant):
            raise DispatchError("authorization_grant_required")
        if action_fingerprint(grant.action) != grant.fingerprint:
            raise DispatchError("grant_fingerprint_mismatch")
        decision = self.store.get_policy_decision(grant.policy_decision_id)
        if decision.action_id != grant.action_id:
            raise DispatchError("grant_decision_mismatch")
        stored_action = self.store.get_action(grant.action_id)
        if (
            stored_action.session_id != grant.session_id
            or stored_action.fingerprint != grant.fingerprint
            or stored_action.action != grant.action
        ):
            raise DispatchError("grant_action_mismatch")
        if decision.decision.value == "DENY":
            raise DispatchError("denied_action_cannot_dispatch")
        if decision.decision.value == "REQUIRE_APPROVAL" and not self.store.is_consumed_approval(
            grant.approval_id,
            grant.fingerprint,
            action_id=grant.action_id,
            session_id=grant.session_id,
            policy_decision_id=grant.policy_decision_id,
        ):
            raise DispatchError("consumed_approval_required")
        action = grant.action
        try:
            if action.tool == "list_files":
                value = self.file_tools.list_files(action.path, action.glob, action.limit)
            elif action.tool == "read_file":
                value = self.file_tools.read(action.path, action.start_line, action.end_line)
            elif action.tool == "replace_in_file":
                value = self.file_tools.replace(grant.session_id, action.path, action.old_text, action.new_text, action.expected_matches)
            elif action.tool == "create_file":
                value = self.file_tools.create(grant.session_id, action.path, action.content)
            elif action.tool == "delete_file":
                value = self.file_tools.delete(grant.session_id, action.path)
            elif isinstance(action, RunCommandAction):
                value = self.command_runner.run(action, workspace=self.workspace)
            elif isinstance(action, ProposeMemoryAction) and self.memory_service is not None:
                value = self.memory_service.propose_from_action(grant.session_id, action)
            else:
                raise DispatchError("unsupported_dispatch_action")
        except DispatchError:
            raise
        except Exception as error:
            raise DispatchError("tool_execution_failed") from error
        return Observation(action_id=grant.action_id, category="success", summary=f"{action.tool} completed", evidence=str(value)[:50_000])
