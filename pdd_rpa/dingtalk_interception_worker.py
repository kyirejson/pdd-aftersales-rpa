"""Isolated UIA worker that appends one interception record to DingTalk."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from .dingtalk_interception_register import (
    DingTalkInterceptionRegisterConfig,
    DingTalkInterceptionRegisterWriter,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DingTalk interception-register worker")
    parser.add_argument("--evidence-dir", required=True)
    return parser


def _run(request: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    config = DingTalkInterceptionRegisterConfig.from_raw(
        dict(request.get("dingtalk_config") or {})
    )
    writer = DingTalkInterceptionRegisterWriter(config, Path(args.evidence_dir))
    result = writer.append(
        dict(request.get("record") or {}),
        local_duplicate_tracking=bool(request.get("local_duplicate_tracking", False)),
    )
    return {"ok": True, "result": result}


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
