"""Isolated desktop-message worker for carrier interception requests.

The parent API process must never own a UIA object for WeChat, Enterprise
WeChat, or the YTO service page.  Native automation failures are contained in
this short-lived worker and reported over a small JSON protocol instead.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from .connectors.desktop_message import DesktopMessageConnector
from .db import Repository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Carrier interception message worker")
    parser.add_argument("--database", required=True)
    parser.add_argument("--evidence-dir", required=True)
    return parser


def _run(request: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    repository = Repository(Path(args.database))
    connector = DesktopMessageConnector(repository, Path(args.evidence_dir))
    result = connector.send(
        dict(request.get("group") or {}),
        str(request.get("message") or ""),
    )
    return {
        "ok": True,
        "result": {
            "ok": bool(result.ok),
            "data": result.data,
            "evidence": result.evidence,
            "error": result.error,
            "connector": connector.name,
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request = json.load(sys.stdin)
        print(json.dumps(_run(request, args), ensure_ascii=False))
        return 0
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
