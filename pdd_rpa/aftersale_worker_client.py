"""Parent-side launcher for the isolated aftersale-workbench UIA worker."""

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


class AftersaleWorkerError(RuntimeError):
    def __init__(self, message: str, *, returncode: int | None = None, crashed: bool = False):
        super().__init__(message)
        self.returncode = returncode
        self.crashed = crashed


def run_isolated_aftersale(
    *,
    project_root: Path,
    evidence_dir: Path,
    config: dict[str, Any],
    shop: dict[str, Any],
    operation: str,
    order_no: str = "",
    aftersale_no: str = "",
    facts: dict[str, Any] | None = None,
    action: str = "",
    live: bool = False,
    keep_detail_open: bool = True,
    timeout_seconds: float = 180,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "pdd_rpa.aftersale_worker",
        "--evidence-dir",
        str(evidence_dir),
    ]
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    request = {
        "config": config,
        "shop": shop,
        "operation": operation,
        "order_no": order_no,
        "aftersale_no": str(aftersale_no or ""),
        "facts": dict(facts or {}),
        "action": action,
        "live": bool(live),
        "keep_detail_open": bool(keep_detail_open),
    }
    worker_env = os.environ.copy()
    worker_env["PYTHONIOENCODING"] = "utf-8"
    process = None
    stdout = ""
    stderr = ""
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
        raise AftersaleWorkerError("AFTERSALE_WORKER_CANCELLED") from exc
    except subprocess.TimeoutExpired as exc:
        raise AftersaleWorkerError(f"AFTERSALE_WORKER_TIMEOUT after {timeout_seconds}s") from exc
    except OSError as exc:
        raise AftersaleWorkerError(f"AFTERSALE_WORKER_FAILED: {exc}") from exc
    finally:
        if process is not None:
            ACTIVE_WORKERS.unregister(process)
    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    if returncode != 0:
        crashed = returncode in NATIVE_CRASH_CODES
        label = "AFTERSALE_WORKER_CRASHED" if crashed else "AFTERSALE_WORKER_FAILED"
        detail = stderr[-1600:] if stderr else stdout[-1600:]
        raise AftersaleWorkerError(
            f"{label} returncode={returncode}{': ' + detail if detail else ''}",
            returncode=returncode,
            crashed=crashed,
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AftersaleWorkerError("AFTERSALE_WORKER_INVALID_OUTPUT", returncode=returncode) from exc
    if not payload.get("ok"):
        raise AftersaleWorkerError(
            f"AFTERSALE_WORKER_FAILED: {payload.get('error') or 'unknown error'}",
            returncode=returncode,
        )
    return payload


__all__ = ["AftersaleWorkerError", "run_isolated_aftersale"]
