from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from coding_agent_harness.models import RunCommandAction
from coding_agent_harness.security import (
    SecurityViolation,
    WorkspaceGuard,
    redact_text,
    scrub_environment,
)


class CommandResult(BaseModel):
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool


class CommandRunner:
    _ALLOWED: ClassVar[frozenset[str]] = frozenset({"python"})

    def __init__(self, max_output_bytes: int = 50_000) -> None:
        self.max_output_bytes = max_output_bytes

    def run(self, action: RunCommandAction, *, workspace: Path) -> CommandResult:
        if action.program.casefold() not in self._ALLOWED:
            raise ValueError("program_not_allowed")
        lowered = tuple(argument.casefold() for argument in action.args)
        if lowered[:2] == ("-m", "pip") or any(token in {"push", "curl", "wget", "powershell", "bash", "cmd"} for token in lowered):
            raise ValueError("command_not_allowed")
        try:
            cwd = WorkspaceGuard(workspace).resolve(action.cwd, must_exist=True)
        except SecurityViolation as error:
            raise ValueError("workspace_cwd_invalid") from error
        executable = sys.executable
        flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="cah-pycache-") as pycache_prefix:
            environment = scrub_environment(dict(os.environ))
            environment["PYTHONPYCACHEPREFIX"] = pycache_prefix
            process = subprocess.Popen(
                [executable, *action.args],
                cwd=cwd,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=flags,
            )
            timed_out = False
            try:
                stdout, stderr = process.communicate(timeout=action.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        capture_output=True,
                        check=False,
                        shell=False,
                    )
                else:
                    killpg = getattr(os, "killpg", None)
                    if callable(killpg):
                        killpg(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                    else:
                        os.kill(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                stdout, stderr = process.communicate()
        combined = (stdout + stderr).encode("utf-8", errors="replace")
        truncated = len(combined) > self.max_output_bytes
        if truncated:
            stdout = combined[: self.max_output_bytes].decode("utf-8", errors="replace") + "\n<TRUNCATED>"
            stderr = ""
        return CommandResult(
            exit_code=None if timed_out else process.returncode,
            stdout=redact_text(stdout, workspace=workspace),
            stderr=redact_text(stderr, workspace=workspace),
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            truncated=truncated,
        )
