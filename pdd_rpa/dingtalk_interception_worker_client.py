"""Parent-side launcher for the isolated DingTalk register UIA worker."""

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


class DingTalkInterceptionWorkerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        crashed: bool = False,
    ) -> None:
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


def run_isolated_dingtalk_interception_register(
    *,
    project_root: Path,
    evidence_dir: Path,
    dingtalk_config: dict[str, Any],
    record: dict[str, Any],
    local_duplicate_tracking: bool,
    timeout_seconds: float = 120,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "pdd_rpa.dingtalk_interception_worker",
        "--evidence-dir",
        str(evidence_dir),
    ]
    creationflags, startupinfo = _creation_flags()
    worker_env = os.environ.copy()
    worker_env["PYTHONIOENCODING"] = "utf-8"
    request = {
        "dingtalk_config": dingtalk_config,
        "record": record,
        "local_duplicate_tracking": bool(local_duplicate_tracking),
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
            timeout_seconds=max(10.0, min(float(timeout_seconds), 300.0)),
            cancel_event=cancel_event,
        )
        returncode = int(process.returncode if process.returncode is not None else process.poll() or 0)
    except WorkerProcessCancelled as exc:
        raise DingTalkInterceptionWorkerError("DINGTALK_INTERCEPTION_WORKER_CANCELLED") from exc
    except subprocess.TimeoutExpired as exc:
        raise DingTalkInterceptionWorkerError(
            f"DINGTALK_INTERCEPTION_WORKER_TIMEOUT after {timeout_seconds}s"
        ) from exc
    except OSError as exc:
        raise DingTalkInterceptionWorkerError(
            f"DINGTALK_INTERCEPTION_WORKER_FAILED: {exc}"
        ) from exc
    finally:
        if process is not None:
            ACTIVE_WORKERS.unregister(process)

    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    if returncode != 0:
        crashed = returncode in NATIVE_CRASH_CODES
        label = "DINGTALK_INTERCEPTION_WORKER_CRASHED" if crashed else "DINGTALK_INTERCEPTION_WORKER_FAILED"
        detail = stderr[-1600:] if stderr else stdout[-1600:]
        suffix = f": {detail}" if detail else ""
        raise DingTalkInterceptionWorkerError(
            f"{label} returncode={returncode}{suffix}",
            returncode=returncode,
            crashed=crashed,
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DingTalkInterceptionWorkerError(
            "DINGTALK_INTERCEPTION_WORKER_INVALID_OUTPUT", returncode=returncode
        ) from exc
    if not payload.get("ok"):
        raise DingTalkInterceptionWorkerError(
            f"DINGTALK_INTERCEPTION_WORKER_FAILED: {payload.get('error') or 'unknown error'}",
            returncode=returncode,
        )
    return payload


__all__ = [
    "DingTalkInterceptionWorkerError",
    "run_isolated_dingtalk_interception_register",
]
