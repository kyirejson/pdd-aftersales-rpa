from __future__ import annotations

"""Small local bridge to the Windows built-in OCR engine.

This intentionally has no network dependency.  It is only used to read a
cropped screenshot after a desktop component has been captured, primarily for
chat clients whose message panes have no useful UIA tree.
"""

import json
import os
from pathlib import Path
import subprocess
from typing import Any


class WindowsOcrError(RuntimeError):
    """Raised when a local Windows OCR invocation cannot return text."""


def recognize_image(
    image_path: str | Path,
    *,
    language: str = "zh-Hans-CN",
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    """Return OCR text/lines for a local image, or raise a clear local error."""

    image = Path(image_path).resolve()
    if not image.is_file():
        raise WindowsOcrError(f"OCR image does not exist: {image}")
    script = Path(__file__).resolve().parents[1] / "scripts" / "windows_ocr.ps1"
    if not script.is_file():
        raise WindowsOcrError(f"Windows OCR helper does not exist: {script}")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-ImagePath",
        str(image),
        "-Language",
        str(language),
    ]
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(2.0, min(float(timeout_seconds), 30.0)),
            check=False,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise WindowsOcrError("Windows OCR timed out") from exc
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "Windows OCR failed").strip()
        raise WindowsOcrError(error[-1000:])
    payload_text = (completed.stdout or "").strip()
    try:
        result = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise WindowsOcrError(f"Windows OCR returned invalid JSON: {payload_text[-500:]}") from exc
    if not isinstance(result, dict):
        raise WindowsOcrError("Windows OCR returned an invalid payload")
    result["text"] = str(result.get("text") or "")
    result["lines"] = list(result.get("lines") or [])
    return result
