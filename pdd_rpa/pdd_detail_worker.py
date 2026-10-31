"""Isolated worker for one read-only PDD detail UIA operation."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .detail_collector import PddDetailCollector
from .edge_uia import PddAlreadyProcessedError, PddOrderNotFoundError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PDD detail UIA worker")
    parser.add_argument("--evidence-dir", required=True)
    return parser


def _shop_namespace(shop: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        id=str(shop.get("id") or ""),
        name=str(shop.get("name") or ""),
        edge_profile=str(shop.get("edge_profile") or ""),
        enabled=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request: dict[str, Any] = json.load(sys.stdin)
        config = dict(request.get("config") or {})
        shop = dict(request.get("shop") or {})
        collector = PddDetailCollector(
            config,
            (_shop_namespace(shop),),
            Path(args.evidence_dir),
        )
        operation = str(request.get("operation") or "collect")
        if operation == "close":
            result = collector.close_active_detail(str(shop.get("id") or ""))
            print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
            return 0
        collected = collector.collect(
            str(shop.get("id") or ""),
            str(request.get("order_no") or ""),
            keep_detail_open=bool(request.get("keep_detail_open")),
            require_logistics=bool(request.get("require_logistics", True)),
        )
        print(json.dumps({"ok": True, "result": collected.to_dict()}, ensure_ascii=False))
        return 0
    except PddAlreadyProcessedError as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "error_type": "already_processed"},
                ensure_ascii=False,
            )
        )
        # A business skip is a valid worker response, not a process failure.
        return 0
    except PddOrderNotFoundError as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "error_type": "not_found"},
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
