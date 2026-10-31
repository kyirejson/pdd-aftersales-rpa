"""Isolated worker for retained PDD detail-page UIA actions.

The API process must not touch Edge UIA/COM after the desktop worker returns.
On Windows, pywinauto's UIA bridge can tear down through ``_ctypes.pyd`` when
the parent process resumes after another UIA process has owned the desktop.
This worker therefore owns the complete retained-detail PDD sequence and returns
JSON-only data to the API process.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from .db import Repository
from .detail_collector import PddDetailCollector
from .earlier_shipping_workflow import EarlierShippingComponentWorkflow
from .in_transit_routing import decide_interception
from .in_transit_workflow import InTransitComponentWorkflow
from .refund_workflow import RefundComponentWorkflow


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PDD refund UIA worker")
    parser.add_argument("--database", required=True)
    parser.add_argument("--evidence-dir", required=True)
    return parser


def _run(request: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    repository = Repository(Path(args.database))
    repository.initialize()
    pdd_config = dict(request.get("pdd_config") or {})
    # Every operation reuses the exact detail URL already collected by the
    # detail worker. No collector re-open is permitted here.
    collector = PddDetailCollector(pdd_config, (), Path(args.evidence_dir))
    workflow = RefundComponentWorkflow(repository, collector, pdd_config)
    facts = dict(request.get("facts") or {})
    operation = str(request.get("operation") or "prepare")

    activation = workflow.activate_detail_after_wdt(facts)
    if operation == "confirm_earlier_shipping":
        earlier_shipping = EarlierShippingComponentWorkflow(
            repository,
            pdd_config,
            browser=workflow.browser,
        )
        result = earlier_shipping.execute(facts, live=bool(request.get("live")))
        detail_cleanup = None
        if bool(request.get("close_detail_after_execute")):
            try:
                detail_cleanup = workflow.close_retained_detail(facts)
            except Exception as exc:
                detail_cleanup = {
                    "closed": 0,
                    "list_ready": False,
                    "error": str(exc),
                }
        return {
            "ok": True,
            "operation": operation,
            "detail_activation": activation,
            "result": result,
            "detail_cleanup": detail_cleanup,
            "business_write_performed": bool(result.get("business_write_performed")),
        }
    if operation == "in_transit_complete":
        # One Edge detail tab, one UIA session, one worker.  The former
        # refresh/reason/final worker chain rediscovered the same tab three
        # times and could intermittently observe zero matches after refresh.
        refreshed = workflow.browser.refresh_detail(facts)
        for field in ("tracking_no", "carrier", "latest_logistics_text"):
            if field in refreshed:
                facts[field] = refreshed[field]

        routing = decide_interception(
            facts,
            dict(request.get("routing_config") or {}),
        )
        if not routing.get("ok"):
            raise RuntimeError(routing.get("reason") or "拦截后物流路由无法匹配")

        in_transit = InTransitComponentWorkflow(
            repository,
            pdd_config,
            browser=workflow.browser,
        )
        reason = in_transit.prepare_reason(
            facts,
            str(request.get("reason_text") or "主观原因不想要"),
            live=bool(request.get("live")),
        )
        facts["in_transit_detail_form"] = str(reason.get("detail_form") or "reason")
        facts["in_transit_final_action_override"] = str(
            reason.get("final_action_override") or ""
        )
        facts["in_transit_refund_reason"] = str(
            reason.get("refund_reason")
            or request.get("reason_text")
            or "主观原因不想要"
        )
        if isinstance(reason.get("decision"), dict):
            facts["in_transit_detail_decision"] = dict(reason["decision"])

        requested_action = str(routing.get("final_action") or "REJECT_REFUND")
        if routing.get("force_reject_after_interception"):
            effective_action = (
                "AGREED_CONSENSUS"
                if str(reason.get("detail_form") or "reason") == "outcome"
                else "REJECT_REFUND"
            )
        else:
            effective_action = str(
                reason.get("final_action_override") or requested_action
            )
        if bool(request.get("live")):
            in_transit.wait_for_final_form(facts, effective_action)
        terminal = in_transit.finish(
            facts,
            effective_action,
            live=bool(request.get("live")),
        )
        detail_cleanup = None
        if bool(request.get("close_detail_after_execute")):
            try:
                detail_cleanup = workflow.close_retained_detail(facts)
            except Exception as exc:
                detail_cleanup = {
                    "closed": 0,
                    "list_ready": False,
                    "error": str(exc),
                }
        return {
            "ok": True,
            "operation": operation,
            "detail_activation": activation,
            "refreshed": refreshed,
            "routing": routing,
            "reason": reason,
            "result": terminal,
            "requested_action": requested_action,
            "effective_action": effective_action,
            "detail_cleanup": detail_cleanup,
            "business_write_performed": bool(request.get("live")),
        }
    if operation == "shipped_interception_complete":
        result = workflow.execute_shipped_after_interception(
            facts,
            live=bool(request.get("live")),
        )
        detail_cleanup = None
        if bool(request.get("close_detail_after_execute")):
            try:
                detail_cleanup = workflow.close_retained_detail(facts)
            except Exception as exc:
                detail_cleanup = {
                    "closed": 0,
                    "list_ready": False,
                    "error": str(exc),
                }
        return {
            "ok": True,
            "operation": operation,
            "detail_activation": activation,
            "result": result,
            "detail_cleanup": detail_cleanup,
            "business_write_performed": bool(result.get("business_write_performed")),
        }
    if operation == "in_transit_refresh":
        result = workflow.browser.refresh_detail(facts)
        return {
            "ok": True,
            "operation": operation,
            "detail_activation": activation,
            "result": result,
            "business_write_performed": False,
        }
    if operation in {"in_transit_reason", "in_transit_finish"}:
        in_transit = InTransitComponentWorkflow(
            repository,
            pdd_config,
            browser=workflow.browser,
        )
        if operation == "in_transit_reason":
            result = in_transit.prepare_reason(
                facts,
                str(request.get("reason_text") or "主观原因不想要"),
                live=bool(request.get("live")),
            )
        else:
            result = in_transit.finish(
                facts,
                str(request.get("action") or "REJECT_REFUND"),
                live=bool(request.get("live")),
            )
        # The terminal in-transit decision is the last action that needs the
        # retained PDD work-order tab.  Close that exact tab in this same
        # isolated UIA worker; a later cross-worker cleanup can miss it after
        # the page transitions.  A cleanup error must not invalidate an
        # already-submitted merchant decision.
        detail_cleanup = None
        if operation == "in_transit_finish" and bool(request.get("close_detail_after_execute")):
            try:
                detail_cleanup = workflow.close_retained_detail(facts)
            except Exception as exc:
                detail_cleanup = {
                    "closed": 0,
                    "list_ready": False,
                    "error": str(exc),
                }
        return {
            "ok": True,
            "operation": operation,
            "detail_activation": activation,
            "result": result,
            "detail_cleanup": detail_cleanup,
            # The alternate `处理情况` form only captures controls in its
            # prepare stage; it has not selected or submitted anything yet.
            "business_write_performed": bool(
                result.get("business_write_performed")
            ),
        }
    if operation == "prepare":
        preview = workflow.preview_first_stage(facts, retain_detail=True)
        return {
            "ok": True,
            "operation": operation,
            "detail_activation": activation,
            "preview": preview,
            "business_write_performed": False,
        }
    if operation == "execute":
        # Establish the prepared-detail marker inside this same UIA process;
        # the real refund never depends on a marker held by the API process.
        preview = workflow.preview_first_stage(facts, retain_detail=True)
        try:
            result = workflow.execute_real_pilot(
                facts,
                str(request.get("confirm_order") or ""),
                float(request.get("confirm_amount") or 0),
            )
        except Exception as exc:
            # A submit can succeed while the platform keeps the detail page in
            # an asynchronous "退款中" state.  Inspect that state in the same
            # isolated UIA process; never make the parent inspect it after a
            # worker returns.
            try:
                state = workflow.browser.inspect_page_state(facts)
            except Exception:
                raise
            submitted_async = (
                state.get("merchant_agreed")
                and state.get("refund_processing")
                and not state.get("visible_forms")
            )
            if not submitted_async:
                raise
            result = {
                "ok": True,
                "order_no": facts["order_no"],
                "refund_amount": float(facts.get("refund_amount") or 0),
                "wdt_order_status": facts.get("wdt_order_status"),
                "captured_components": [
                    "CMP-PDD-SELECT-UNHANDED",
                    "CMP-PDD-SUBMIT-UNHANDED",
                    "CMP-PDD-SELECT-AGREE",
                    "CMP-PDD-SUBMIT-FINISH",
                ],
                "success": state,
                "submit_exception": str(exc),
                "recovered_after_submit": True,
                "business_write_performed": True,
            }
        reconciliation = None
        if bool(request.get("reconcile_after_execute")):
            reconciliation = workflow.reconcile_real_result(facts)
        detail_cleanup = None
        if bool(request.get("close_detail_after_execute")):
            detail_cleanup = workflow.close_retained_detail(facts)
        return {
            "ok": True,
            "operation": operation,
            "detail_activation": activation,
            "preview": preview,
            "result": result,
            "reconciliation": reconciliation,
            "detail_cleanup": detail_cleanup,
            "business_write_performed": True,
        }
    if operation == "reconcile":
        result = workflow.reconcile_real_result(facts)
        return {
            "ok": True,
            "operation": operation,
            "detail_activation": activation,
            "result": result,
            "business_write_performed": True,
        }
    raise RuntimeError(f"未知 PDD UIA Worker 操作：{operation}")


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
