"""Parent-side launcher for one isolated PDD detail UIA operation."""

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


class PddDetailWorkerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        crashed: bool = False,
        error_type: str = "",
    ):
        super().__init__(message)
        self.returncode = returncode
        self.crashed = crashed
        self.error_type = error_type


def _creation_flags() -> tuple[int, Any]:
    if os.name != "nt":
        return 0, None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW, startupinfo


def run_isolated_pdd_detail(
    *,
    project_root: Path,
    evidence_dir: Path,
    config: dict[str, Any],
    shop: dict[str, Any],
    order_no: str,
    keep_detail_open: bool,
    require_logistics: bool = True,
    timeout_seconds: float = 180,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run one PDD detail collect in a child process.

    The child may leave the Edge detail tab open for the following WDT/refund
    steps, but all UIA/COM objects are owned by the child. A native UIA crash
    therefore cannot terminate the FastAPI process.
    """
    command = [
        sys.executable,
        "-m",
        "pdd_rpa.pdd_detail_worker",
        "--evidence-dir",
        str(evidence_dir),
    ]
    creationflags, startupinfo = _creation_flags()
    worker_env = os.environ.copy()
    worker_env["PYTHONIOENCODING"] = "utf-8"
    request = {
        "operation": "collect",
        "config": config,
        "shop": shop,
        "order_no": order_no,
        "keep_detail_open": bool(keep_detail_open),
        "require_logistics": bool(require_logistics),
    }
    return _run_worker(
        command=command,
        project_root=project_root,
        request=request,
        env=worker_env,
        creationflags=creationflags,
        startupinfo=startupinfo,
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    )


def run_isolated_pdd_close(
    *,
    project_root: Path,
    evidence_dir: Path,
    config: dict[str, Any],
    shop: dict[str, Any],
    timeout_seconds: float = 60,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Close active detail tabs in an isolated UIA process."""
    command = [
        sys.executable,
        "-m",
        "pdd_rpa.pdd_detail_worker",
        "--evidence-dir",
        str(evidence_dir),
    ]
    creationflags, startupinfo = _creation_flags()
    worker_env = os.environ.copy()
    worker_env["PYTHONIOENCODING"] = "utf-8"
    request = {"operation": "close", "config": config, "shop": shop}
    return _run_worker(
        command=command,
        project_root=project_root,
        request=request,
        env=worker_env,
        creationflags=creationflags,
        startupinfo=startupinfo,
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    )


def _run_worker(
    *,
    command: list[str],
    project_root: Path,
    request: dict[str, Any],
    env: dict[str, str],
    creationflags: int,
    startupinfo: Any,
    timeout_seconds: float,
    cancel_event: threading.Event | None,
) -> dict[str, Any]:
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
            env=env,
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
        raise PddDetailWorkerError("PDD_DETAIL_WORKER_CANCELLED") from exc
    except subprocess.TimeoutExpired as exc:
        raise PddDetailWorkerError(
            f"PDD_DETAIL_WORKER_TIMEOUT after {timeout_seconds}s"
        ) from exc
    except OSError as exc:
        raise PddDetailWorkerError(f"PDD_DETAIL_WORKER_FAILED: {exc}") from exc
    finally:
        if process is not None:
            ACTIVE_WORKERS.unregister(process)

    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    if returncode != 0:
        crashed = returncode in NATIVE_CRASH_CODES
        label = "PDD_DETAIL_WORKER_CRASHED" if crashed else "PDD_DETAIL_WORKER_FAILED"
        detail = stderr[-1200:] if stderr else stdout[-1200:]
        suffix = f": {detail}" if detail else ""
        raise PddDetailWorkerError(
            f"{label} returncode={returncode}{suffix}",
            returncode=returncode,
            crashed=crashed,
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PddDetailWorkerError("PDD_DETAIL_WORKER_INVALID_OUTPUT", returncode=returncode) from exc
    if not payload.get("ok"):
        error_type = str(payload.get("error_type") or "")
        raise PddDetailWorkerError(
            f"PDD_DETAIL_WORKER_FAILED: {payload.get('error') or 'unknown error'}",
            returncode=returncode,
            error_type=error_type,
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise PddDetailWorkerError("PDD_DETAIL_WORKER_INVALID_RESULT", returncode=returncode)
    return result
