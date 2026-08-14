import json
from pathlib import Path

from coding_agent_harness.models import (
    Decision,
    Observation,
    ReadFileAction,
    SessionStatus,
    ValidationResult,
)
from coding_agent_harness.reporting import ReportExporter


def test_report_excludes_secrets_absolute_paths_and_source(store, tmp_path: Path) -> None:
    project_id = store.upsert_project(tmp_path, "Private project")
    session_id = store.create_session(project_id, "diagnose sk-secret")
    store.transition_session(session_id, SessionStatus.RUNNING)
    action = ReadFileAction(path=str(tmp_path / "private.py"))
    action_id = store.record_action(session_id, 1, action, "fingerprint-1")
    store.record_policy_decision(
        action_id, decision=Decision.ALLOW, reason_code="read_only", rule_source="policy"
    )
    store.record_observation(
        session_id,
        Observation(
            category="tool_error",
            summary="private source",
            evidence="sk-secret and private source",
            action_id=action_id,
        ),
    )
    store.record_validation(
        session_id,
        ValidationResult(
            validator_id="pytest",
            stage="baseline",
            status="failed",
            exit_code=1,
            duration_ms=2,
            summary="private source",
            evidence="sk-secret and private source",
        ),
    )
    store.transition_session(session_id, SessionStatus.NEEDS_USER_DECISION)

    report = ReportExporter(store).build(session_id)
    payload = report.model_dump_json()

    assert report.schema_version == "1.0"
    assert "sk-secret" not in payload
    assert str(tmp_path) not in payload
    assert "private source" not in payload
    assert report.project.display_name == "Private project"
    assert report.actions[0]["path"] == "private.py"
    assert set(report.actions[0]) <= {"tool", "path", "decision", "reason_code"}


def test_report_export_writes_json_with_schema(tmp_path: Path, store) -> None:
    project_id = store.upsert_project(tmp_path, "Export project")
    session_id = store.create_session(project_id, "export")
    destination = tmp_path / "report.json"

    output = ReportExporter(store).export(session_id, destination)

    assert output == destination
    data = json.loads(destination.read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.0"
    assert data["session_id"] == session_id


def test_viewer_uses_text_content_and_only_mock_fetch() -> None:
    script = Path("src/coding_agent_harness/web/app.js").read_text(encoding="utf-8")
    assert "textContent" in script
    assert "innerHTML" not in script
    assert 'fetch("./mock-report.json")' in script
    assert "WebSocket" not in script
    assert "sqlite" not in script.lower()
