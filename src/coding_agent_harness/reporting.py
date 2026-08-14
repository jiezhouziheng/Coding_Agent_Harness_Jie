"""Allowlisted session report export for the static, read-only viewer."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from coding_agent_harness.file_tools import _atomic_write
from coding_agent_harness.models import StrictModel

if TYPE_CHECKING:
    from coding_agent_harness.storage import StateStore


class ReportProject(StrictModel):
    display_name: str


class SessionReport(StrictModel):
    schema_version: str = "1.0"
    session_id: str
    project: ReportProject
    status: str
    actions: tuple[dict[str, object], ...]
    approvals: tuple[dict[str, object], ...]
    validations: tuple[dict[str, object], ...]
    final_summary: str


class ReportExporter:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def build(self, session_id: str) -> SessionReport:
        safe = self.store.query_safe_report_rows(session_id)
        return SessionReport.model_validate(safe)

    def export(self, session_id: str, destination: Path) -> Path:
        report = self.build(session_id)
        _atomic_write(destination, report.model_dump_json(indent=2).encode("utf-8"))
        return destination
