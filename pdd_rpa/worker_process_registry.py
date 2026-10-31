"""Track and promptly terminate automation worker processes.

The runtime controller is intentionally kept in the API process, while the
UIA-heavy PDD and WDT operations run in short-lived child processes.  A
threading stop event cannot interrupt a blocking ``Popen.communicate`` call,
so the parent keeps the exact process handles it created and can terminate
only those workers when the user presses the immediate-stop button.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any


class WorkerProcessRegistry:
    """Small thread-safe registry for child processes owned by this runtime."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._processes: dict[int, Any] = {}

    def register(self, process: Any) -> None:
        pid = int(getattr(process, "pid", 0) or 0)
        if pid <= 0:
            return
        with self._lock:
            self._processes[pid] = process

    def unregister(self, process: Any) -> None:
        pid = int(getattr(process, "pid", 0) or 0)
        if pid <= 0:
            return
        with self._lock:
            self._processes.pop(pid, None)

    def pids(self) -> list[int]:
        with self._lock:
            return list(self._processes)

    def terminate_all(self) -> list[int]:
        """Terminate only registered worker trees and return affected PIDs."""

        with self._lock:
            processes = list(self._processes.values())
        affected: list[int] = []
        for process in processes:
            pid = int(getattr(process, "pid", 0) or 0)
            if pid <= 0:
                continue
            if _terminate_process_tree(process):
                affected.append(pid)
        return affected


class WorkerProcessCancelled(RuntimeError):
    """Raised when the runtime stop signal cancels a child process."""


def communicate_registered_process(
    process: Any,
    input_text: str,
    *,
    timeout_seconds: float,
    cancel_event: threading.Event | None = None,
) -> tuple[str, str]:
    """Communicate with a registered worker while polling cancellation."""

    timeout = max(0.1, float(timeout_seconds))
    deadline = time.monotonic() + timeout
    pending_input: str | None = input_text
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _terminate_process_tree(process)
            try:
                process.communicate(timeout=1)
            except Exception:
                pass
            raise WorkerProcessCancelled("worker process cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process_tree(process)
            try:
                process.communicate(timeout=1)
            except Exception:
                pass
            raise subprocess.TimeoutExpired(getattr(process, "args", "worker"), timeout)
        try:
            stdout, stderr = process.communicate(
                input=pending_input,
                timeout=min(0.25, remaining),
            )
            return str(stdout or ""), str(stderr or "")
        except subprocess.TimeoutExpired:
            # Popen caches the input after the first communicate call; do not
            # submit the JSON body a second time when polling.
            pending_input = None


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _terminate_process_tree(process: Any) -> bool:
    """Terminate a known child and its descendants without broad enumeration."""

    pid = int(getattr(process, "pid", 0) or 0)
    if pid <= 0:
        return False
    try:
        if process.poll() is not None:
            return False
    except Exception:
        pass

    if os.name == "nt":
        # ``/T`` is important: UI automation workers can create a native
        # helper process whose pipe keeps the parent blocked after the parent
        # alone has been terminated.  The PID is from our registry, never from
        # a user-supplied or broad process search.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=2,
                creationflags=_creation_flags(),
            )
            return True
        except (OSError, subprocess.SubprocessError):
            pass

    try:
        process.terminate()
        return True
    except Exception:
        try:
            process.kill()
            return True
        except Exception:
            return False


ACTIVE_WORKERS = WorkerProcessRegistry()


__all__ = [
    "ACTIVE_WORKERS",
    "WorkerProcessCancelled",
    "WorkerProcessRegistry",
    "_terminate_process_tree",
    "communicate_registered_process",
]
