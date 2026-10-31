"""Keep the local RPA console backend alive on Windows.

The supervisor is intentionally separate from the FastAPI process.  If the
backend exits because of an uncaught exception or an external process cleanup,
the supervisor records the exit and starts it again.  It never resumes the
runtime controller; production work still requires an explicit UI action.
"""

from __future__ import annotations

import msvcrt
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
RUNTIME_DIR = PROJECT_ROOT / "runtime"
LOCK_PATH = RUNTIME_DIR / "console_supervisor.lock"
LOG_PATH = RUNTIME_DIR / "console_supervisor.log"
BACKEND_LOG_PATH = RUNTIME_DIR / "backend.log"
# Listen on all local interfaces.  Windows Firewall restricts port 8876 to the
# current private LAN subnet; the FastAPI console also requires authentication.
HOST = "0.0.0.0"
PORT = "8876"
RESTART_DELAY_SECONDS = 3
MAX_RESTART_DELAY_SECONDS = 60


def write_log(message: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def acquire_single_instance_lock():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+b")
    handle.seek(0)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def backend_command() -> list[str]:
    return [
        str(PYTHON),
        "-m",
        "pdd_rpa.cli",
        "serve",
        "--host",
        HOST,
        "--port",
        PORT,
    ]


def spawn_backend(log_handle):
    if not PYTHON.exists():
        raise RuntimeError(f"未找到虚拟环境 Python：{PYTHON}")
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    return subprocess.Popen(
        backend_command(),
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        startupinfo=startupinfo,
        close_fds=False,
    )


def stop_child(child) -> None:
    if child is None or child.poll() is not None:
        return
    try:
        child.terminate()
        child.wait(timeout=5)
    except Exception:
        try:
            child.kill()
        except Exception:
            pass


def main() -> int:
    lock_handle = acquire_single_instance_lock()
    if lock_handle is None:
        return 0

    child = None
    delay = RESTART_DELAY_SECONDS
    try:
        write_log("守护进程启动；后端异常退出会自动重启")
        with BACKEND_LOG_PATH.open("a", encoding="utf-8") as backend_log_handle:
            while True:
                try:
                    child = spawn_backend(backend_log_handle)
                    write_log(f"后端已启动，PID={child.pid}")
                    delay = RESTART_DELAY_SECONDS
                    return_code = child.wait()
                    write_log(f"后端退出，returncode={return_code}；{delay}秒后重启")
                except KeyboardInterrupt:
                    write_log("收到停止信号，守护进程退出")
                    stop_child(child)
                    return 0
                except Exception as exc:
                    write_log(f"启动后端失败：{exc}；{delay}秒后重试")
                finally:
                    child = None
                time.sleep(delay)
                delay = min(delay * 2, MAX_RESTART_DELAY_SECONDS)
    finally:
        stop_child(child)
        try:
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
