"""Parent-side launcher for the isolated retained-detail PDD UIA worker."""

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


class PddRefundWorkerError(RuntimeError):
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


def run_isolated_pdd_refund_stage(
    *,
    project_root: Path,
    database_path: Path,
    evidence_dir: Path,
    pdd_config: dict[str, Any],
    facts: dict[str, Any],
    operation: str,
    confirm_order: str = "",
    confirm_amount: float = 0,
    reason_text: str = "",
    action: str = "",
    routing_config: dict[str, Any] | None = None,
    live: bool = False,
    reconcile_after_execute: bool = False,
    close_detail_after_execute: bool = False,
    timeout_seconds: float = 180,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "pdd_rpa.pdd_refund_worker",
        "--database",
        str(database_path),
        "--evidence-dir",
        str(evidence_dir),
    ]
    creationflags, startupinfo = _creation_flags()
    worker_env = os.environ.copy()
    worker_env["PYTHONIOENCODING"] = "utf-8"
    request = {
        "pdd_config": pdd_config,
        "facts": facts,
        "operation": operation,
        "confirm_order": confirm_order,
        "confirm_amount": float(confirm_amount),
        "reason_text": reason_text,
        "action": action,
        "routing_config": dict(routing_config or {}),
        "live": bool(live),
        "reconcile_after_execute": bool(reconcile_after_execute),
        "close_detail_after_execute": bool(close_detail_after_execute),
    }
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
        raise PddRefundWorkerError("PDD_REFUND_WORKER_CANCELLED") from exc
    except subprocess.TimeoutExpired as exc:
        raise PddRefundWorkerError(
            f"PDD_REFUND_WORKER_TIMEOUT after {timeout_seconds}s"
        ) from exc
    except OSError as exc:
        raise PddRefundWorkerError(f"PDD_REFUND_WORKER_FAILED: {exc}") from exc
    finally:
        if process is not None:
            ACTIVE_WORKERS.unregister(process)

    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    if returncode != 0:
        crashed = returncode in NATIVE_CRASH_CODES
        label = "PDD_REFUND_WORKER_CRASHED" if crashed else "PDD_REFUND_WORKER_FAILED"
        detail = stderr[-1600:] if stderr else stdout[-1600:]
        suffix = f": {detail}" if detail else ""
        raise PddRefundWorkerError(
            f"{label} returncode={returncode}{suffix}",
            returncode=returncode,
            crashed=crashed,
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PddRefundWorkerError(
            "PDD_REFUND_WORKER_INVALID_OUTPUT", returncode=returncode
        ) from exc
    if not payload.get("ok"):
        raise PddRefundWorkerError(
            f"PDD_REFUND_WORKER_FAILED: {payload.get('error') or 'unknown error'}",
            returncode=returncode,
        )
    return payload
