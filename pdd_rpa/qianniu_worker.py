"""Isolated Edge/UIA worker for the staged Qianniu after-sale workflow."""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path
from typing import Any

from .qianniu_edge import QianniuEdgeConfig
from .qianniu_uia import QianniuUiaSession


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qianniu after-sale UIA worker")
    parser.add_argument("--evidence-dir", required=True)
    return parser


def _evidence_identity(order_no: str, aftersale_no: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "", aftersale_no or order_no) or "unknown"


def _screenshot(session: QianniuUiaSession, window: Any, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    session.prompt.screenshot(int(window.handle), path)
    return str(path)


def _run(request: dict[str, Any], evidence_dir: Path) -> dict[str, Any]:
    config = QianniuEdgeConfig.from_raw(dict(request.get("config") or {}))
    reasons = config.validate()
    if reasons:
        raise RuntimeError("；".join(reasons))
    operation = str(request.get("operation") or "scan")
    order_no = str(request.get("order_no") or "").strip()
    aftersale_no = str(request.get("aftersale_no") or "").strip()
    session = QianniuUiaSession(config)

    if operation == "scan":
        records = session.scan_unreceived_refunds()
        return {
            "ok": True,
            "operation": operation,
            "records": [record.to_dict() for record in records],
            "eligible_records": [record.to_dict() for record in records if record.processable],
            "total": len(records),
            "eligible_total": sum(1 for record in records if record.processable),
            "business_write_performed": False,
        }

    if operation == "collect":
        opened = session.open_detail(order_no, aftersale_no)
        detail = session.read_detail(order_no=order_no, aftersale_no=aftersale_no)
        if not bool(detail.get("processable")):
            return {
                "ok": True,
                "operation": operation,
                "opened": opened,
                "externally_handled": True,
                "reason": "详情实时状态已不再满足退款待处理且含拦截快递",
                "detail": detail,
                "evidence": [],
                "business_write_performed": False,
            }
        window = session.window(allowed_kinds={"refund_detail"})
        evidence = evidence_dir / (
            f"qianniu_{config.shop_id}_{_evidence_identity(order_no, aftersale_no)}_detail.png"
        )
        return {
            "ok": True,
            "operation": operation,
            "opened": opened,
            "detail": detail,
            "evidence": [_screenshot(session, window, evidence)],
            "business_write_performed": False,
        }

    if operation == "revalidate":
        state = session.read_current_state(
            order_no=order_no, aftersale_no=aftersale_no
        )
        externally_handled = not bool(state.get("processable"))
        return {
            "ok": True,
            "operation": operation,
            "state": state,
            "processable": not externally_handled,
            "externally_handled": externally_handled,
            "reason": (
                "详情实时状态已变化，人工或其他流程已经处理"
                if externally_handled
                else ""
            ),
            "business_write_performed": False,
        }

    if operation == "read_trajectory":
        trajectory = session.read_logistics_trajectory(
            order_no=order_no, aftersale_no=aftersale_no
        )
        window = session.window(allowed_kinds={"refund_detail"})
        evidence = evidence_dir / (
            f"qianniu_{config.shop_id}_{_evidence_identity(order_no, aftersale_no)}"
            "_logistics_trajectory.png"
        )
        return {
            "ok": True,
            "operation": operation,
            "trajectory": trajectory,
            "evidence": [_screenshot(session, window, evidence)],
            "business_write_performed": False,
        }

    if operation == "close_detail":
        return {
            "ok": True,
            "operation": operation,
            "cleanup": session.close_detail(
                order_no=order_no, aftersale_no=aftersale_no
            ),
            "business_write_performed": False,
        }

    if operation in {"execute_intercept", "execute_agree_refund"}:
        if not bool(request.get("live")):
            return {
                "ok": True,
                "operation": operation,
                "preview": True,
                "business_write_performed": False,
            }
        window = session.window(allowed_kinds={"refund_detail"})
        identity = _evidence_identity(order_no, aftersale_no)
        before = evidence_dir / f"qianniu_{config.shop_id}_{identity}_{operation}_before.png"
        before_path = _screenshot(session, window, before)
        if operation == "execute_intercept":
            result = session.execute_intercept(
                order_no=order_no, aftersale_no=aftersale_no
            )
        else:
            result = session.execute_agree_refund(
                order_no=order_no, aftersale_no=aftersale_no
            )
        after = evidence_dir / f"qianniu_{config.shop_id}_{identity}_{operation}_after.png"
        after_path = _screenshot(session, window, after)
        return {
            "ok": True,
            "operation": operation,
            "result": result,
            "externally_handled": bool(result.get("externally_handled")),
            "reason": str(result.get("reason") or ""),
            "evidence": [before_path, after_path],
            "business_write_performed": bool(result.get("business_write_performed")),
        }

    raise RuntimeError(f"未知千牛 Worker 操作：{operation}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request: dict[str, Any] = json.load(sys.stdin)
        print(json.dumps(_run(request, Path(args.evidence_dir)), ensure_ascii=False))
        return 0
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
