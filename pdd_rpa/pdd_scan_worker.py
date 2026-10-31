"""Isolated worker for one read-only PDD Edge UIA shop scan."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from .connectors.pdd_edge_uia import PddEdgeUiaConnector


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PDD read-only UIA scan worker")
    parser.add_argument("--evidence-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request: dict[str, Any] = json.load(sys.stdin)
        config = dict(request.get("config") or {})
        # The child owns the UIA objects and must not recursively spawn another
        # scan worker.
        config["scan_worker_isolation"] = False
        shop = dict(request.get("shop") or {})
        connector = PddEdgeUiaConnector(
            config,
            Path(args.evidence_dir),
            isolate=False,
        )
        result = connector.discover(shop)
        print(
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "ok": bool(result.ok),
                        "data": result.data,
                        "evidence": result.evidence,
                        "error": result.error,
                    },
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

