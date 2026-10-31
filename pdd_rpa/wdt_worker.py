"""Isolated worker for the read-only 旺店通 component workflow.

The worker owns all desktop automation objects for one task and communicates
with the API process only through JSON on stdin/stdout.  A native UIA crash in
this process therefore cannot take down the control console.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from .db import Repository
from .desktop_workflow import DesktopWorkflowRunner


def _configure_stdio() -> None:
    """Make the worker protocol independent of the Windows console code page."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError):
            # Test doubles and older stream wrappers need no reconfiguration.
            pass


def _emit(payload: dict[str, Any]) -> None:
    """Write protocol JSON using ASCII escapes so GBK can never reject a value."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PDD RPA 旺店通隔离 Worker")
    parser.add_argument("--database", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--task-id", required=True, type=int)
    parser.add_argument("--execution-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = _parser().parse_args(argv)
    try:
        context: dict[str, Any] = json.load(sys.stdin)
        repository = Repository(Path(args.database))
        repository.initialize()
        runner = DesktopWorkflowRunner(repository, Path(args.evidence_dir))
        result = runner.run(
            args.workflow_id,
            context,
            task_id=args.task_id,
            execution_id=args.execution_id,
        )
        _emit({"ok": True, "result": result})
        return 0
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        _emit({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
