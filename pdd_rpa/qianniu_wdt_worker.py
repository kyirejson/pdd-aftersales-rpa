"""Isolated WDT desktop worker for one Qianniu order lookup."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from .db import Repository
from .qianniu_wdt import run_qianniu_wdt_lookup


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qianniu WDT lookup worker")
    parser.add_argument("--database", required=True)
    parser.add_argument("--evidence-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request = json.load(sys.stdin)
        repository = Repository(Path(args.database))
        repository.initialize()
        result = run_qianniu_wdt_lookup(
            repository,
            Path(args.evidence_dir),
            order_no=str(request.get("order_no") or ""),
            tracking_no=str(request.get("tracking_no") or ""),
        )
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
