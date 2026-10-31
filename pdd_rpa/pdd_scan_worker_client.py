"""Parent-side launcher for an isolated per-shop PDD UIA scan.

Windows UI Automation can terminate the hosting Python process with a native
access violation.  A scan worker keeps that failure outside the FastAPI
process, so the controller can record the failed shop and retry it safely.
"""

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


class PddScanWorkerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        crashed: bool = False,
    ):
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


def run_isolated_pdd_scan(
    *,
    project_root: Path,
    evidence_dir: Path,
    config: dict[str, Any],
    shop: dict[str, Any],
    timeout_seconds: float = 180,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run one shop discovery in a child process and return its serialised result."""

    command = [
        sys.executable,
        "-m",
        "pdd_rpa.pdd_scan_worker",
        "--evidence-dir",
        str(evidence_dir),
    ]
    creationflags, startupinfo = _creation_flags()
    worker_env = os.environ.copy()
    worker_env["PYTHONIOENCODING"] = "utf-8"
    request = {"config": config, "shop": shop}
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
            json.dumps(request, ensure_ascii=False),
            timeout_seconds=max(10.0, min(float(timeout_seconds), 900.0)),
            cancel_event=cancel_event,
        )
        returncode = int(process.returncode if process.returncode is not None else process.poll() or 0)
    except WorkerProcessCancelled as exc:
        raise PddScanWorkerError("PDD_SCAN_WORKER_CANCELLED", crashed=False) from exc
    except subprocess.TimeoutExpired as exc:
        raise PddScanWorkerError(
            f"PDD_SCAN_WORKER_TIMEOUT after {timeout_seconds}s",
            crashed=False,
        ) from exc
    except OSError as exc:
        raise PddScanWorkerError(f"PDD_SCAN_WORKER_FAILED: {exc}", crashed=False) from exc
    finally:
        if process is not None:
            ACTIVE_WORKERS.unregister(process)

    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    if returncode != 0:
        crashed = returncode in NATIVE_CRASH_CODES
        label = "PDD_SCAN_WORKER_CRASHED" if crashed else "PDD_SCAN_WORKER_FAILED"
        detail = stderr[-1200:] if stderr else stdout[-1200:]
        suffix = f": {detail}" if detail else ""
        raise PddScanWorkerError(
            f"{label} returncode={returncode}{suffix}",
            returncode=returncode,
            crashed=crashed,
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PddScanWorkerError(
            "PDD_SCAN_WORKER_INVALID_OUTPUT", returncode=returncode
        ) from exc
    if not payload.get("ok"):
        raise PddScanWorkerError(
            f"PDD_SCAN_WORKER_FAILED: {payload.get('error') or 'unknown error'}",
            returncode=returncode,
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise PddScanWorkerError("PDD_SCAN_WORKER_INVALID_RESULT", returncode=returncode)
    return result
