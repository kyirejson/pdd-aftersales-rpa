"""Parent-side launcher for the isolated 旺店通 worker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .worker_process_registry import (
    ACTIVE_WORKERS,
    WorkerProcessCancelled,
    communicate_registered_process,
)


NATIVE_CRASH_CODES = {3221225477, 4294967295, -1073741819}


class WdtWorkerError(RuntimeError):
    def __init__(self, message: str, *, returncode: int | None = None, crashed: bool = False):
        super().__init__(message)
        self.returncode = returncode
        self.crashed = crashed


def _creation_flags() -> tuple[int, Any]:
    if os.name != "nt":
        return 0, None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW, startupinfo


def run_isolated_wdt_workflow(
    *,
    project_root: Path,
    database_path: Path,
    evidence_dir: Path,
    workflow_id: str,
    task_id: int,
    execution_id: str,
    context: dict[str, Any],
    timeout_seconds: float = 180,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "pdd_rpa.wdt_worker",
        "--database",
        str(database_path),
        "--evidence-dir",
        str(evidence_dir),
        "--workflow-id",
        workflow_id,
        "--task-id",
        str(task_id),
        "--execution-id",
        execution_id,
    ]
    creationflags, startupinfo = _creation_flags()
    # Windows defaults to the active code page (usually GBK) for pipes.  The
    # worker emits JSON with Chinese field values, so using the locale codec
    # can fail before the JSON parser ever sees the payload.  Pin both sides
    # of the isolated protocol to UTF-8 and keep a replacement fallback for
    # third-party UI text that is not valid UTF-8.  This is a transport-only
    # safeguard; it does not alter the business values returned by the worker.
    worker_env = os.environ.copy()
    worker_env["PYTHONIOENCODING"] = "utf-8:backslashreplace"
    worker_env["PYTHONUTF8"] = "1"
    process = None
    try:
        process = subprocess.Popen(
            command,
            cwd=str(project_root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=worker_env,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
        ACTIVE_WORKERS.register(process)
        stdout, stderr = communicate_registered_process(
            process,
            json.dumps(context, ensure_ascii=True),
            timeout_seconds=max(10.0, min(float(timeout_seconds), 900.0)),
            cancel_event=cancel_event,
        )
        returncode = int(process.returncode if process.returncode is not None else process.poll() or 0)
    except WorkerProcessCancelled as exc:
        raise WdtWorkerError("WDT_WORKER_CANCELLED", crashed=False) from exc
    except subprocess.TimeoutExpired as exc:
        raise WdtWorkerError(
            f"WDT_WORKER_TIMEOUT after {timeout_seconds}s",
            crashed=False,
        ) from exc
    except OSError as exc:
        raise WdtWorkerError(f"WDT_WORKER_FAILED: {exc}", crashed=False) from exc
    finally:
        if process is not None:
            ACTIVE_WORKERS.unregister(process)
    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    payload = None
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
    if returncode != 0:
        crashed = returncode in NATIVE_CRASH_CODES
        label = "WDT_WORKER_CRASHED" if crashed else "WDT_WORKER_FAILED"
        if not crashed and isinstance(payload, dict) and not payload.get("ok"):
            raise WdtWorkerError(
                f"{label}: {payload.get('error') or 'unknown error'}",
                returncode=returncode,
                crashed=False,
            )
        detail = stderr[-1000:] if stderr else stdout[-1000:]
        suffix = f": {detail}" if detail else ""
        raise WdtWorkerError(
            f"{label} returncode={returncode}{suffix}",
            returncode=returncode,
            crashed=crashed,
        )
    if not isinstance(payload, dict):
        raise WdtWorkerError("WDT_WORKER_INVALID_OUTPUT", returncode=returncode)
    if not payload.get("ok"):
        raise WdtWorkerError(
            f"WDT_WORKER_FAILED: {payload.get('error') or 'unknown error'}",
            returncode=returncode,
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise WdtWorkerError("WDT_WORKER_INVALID_RESULT", returncode=returncode)
    return result
