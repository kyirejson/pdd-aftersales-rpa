"""Isolated UIA worker for PDD aftersale-workbench details."""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path
from typing import Any

from .aftersale_uia import (
    PddAftersaleSearchResultsClosed,
    PddAftersaleUiaSession,
)
from .aftersale_workbench import (
    AFTERSALE_SHIPPED_REFUND,
    AGREE_RECALL_AFTER_REFUND,
    AGREE_REFUND,
    AGREE_RETURN_AFTER_REFUND,
    EXTEND_RECEIPT,
    REJECT_REFUND,
    SKIP_REFUND_CLOSED,
    decide_aftersale,
    shipped_refund_trace_limit,
)


def _cleanup_handled(cleanup: dict[str, Any] | None) -> bool:
    if not cleanup:
        return False
    if "handled" in cleanup:
        return bool(cleanup.get("handled"))
    return bool(cleanup.get("list_ready")) and bool(
        cleanup.get("closed") or cleanup.get("navigated")
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PDD aftersale UIA worker")
    parser.add_argument("--evidence-dir", required=True)
    return parser


def _run(request: dict[str, Any], evidence_dir: Path) -> dict[str, Any]:
    config = dict(request.get("config") or {})
    shop = dict(request.get("shop") or {})
    operation = str(request.get("operation") or "collect")
    session = PddAftersaleUiaSession(config)
    if operation == "collect":
        request_facts = dict(request.get("facts") or {})
        order_no = str(request.get("order_no") or "")
        aftersale_no = str(request.get("aftersale_no") or "").strip()
        shipped_refund_flow = bool(
            str(request_facts.get("work_order_title") or "")
            == AFTERSALE_SHIPPED_REFUND
        )
        # Return-refund scans with a stable aftersale number search that exact
        # application.  The shipped-refund branch must search the original
        # order number so it can inspect every same-order result row and remove
        # historical refund-closed applications before opening one detail.
        try:
            window, detail_url = session.open_detail(
                shop,
                order_no,
                candidate_index=0,
                search_value=(order_no if shipped_refund_flow else aftersale_no or order_no),
                expected_aftersale_no=aftersale_no,
                require_shipped_refund_result=shipped_refund_flow,
            )
        except PddAftersaleSearchResultsClosed as exc:
            return {
                "ok": True,
                "operation": operation,
                "facts": {
                    "order_no": order_no,
                    "list_shipping_status": str(
                        request_facts.get("pdd_order_status")
                        or request_facts.get("list_shipping_status")
                        or ""
                    ),
                    "aftersale_search_candidate_count": int(
                        getattr(session, "last_search_candidate_count", 0)
                    ),
                    "aftersale_search_candidate_rows": list(
                        getattr(session, "last_search_candidate_rows", []) or []
                    ),
                },
                "decision": {
                    # ``SKIP_REFUND_CLOSED`` is the legacy no-op action name;
                    # it now covers both refund-closed and refund-succeeded
                    # terminal search rows.
                    "action": SKIP_REFUND_CLOSED,
                    "reason": str(exc),
                },
                "detail_url": "",
                "evidence": [],
                "detail_cleanup": {
                    "handled": True,
                    "disposition": "search_results_ignored_without_opening_detail",
                    "closed": 0,
                    "list_ready": True,
                },
                "detail_retained": False,
                "business_write_performed": False,
            }
        facts = session.read_detail(window, order_no, detail_url, shop)
        facts["list_shipping_status"] = str(
            request_facts.get("pdd_order_status")
            or request_facts.get("list_shipping_status")
            or ""
        )
        if str(facts.get("order_no") or "").strip() != order_no:
            raise RuntimeError(
                "售后详情原订单号校验失败："
                f"期望={order_no}，实际={facts.get('order_no') or '无法读取'}"
            )
        selected_aftersale_no = str(
            getattr(session, "last_selected_aftersale_no", "") or ""
        ).strip()
        verified_aftersale_no = selected_aftersale_no or aftersale_no
        if verified_aftersale_no:
            actual_aftersale_no = str(facts.get("aftersale_no") or "").strip()
            if not actual_aftersale_no:
                actual_aftersale_no = session._aftersale_no_from_values([detail_url])
            if actual_aftersale_no != verified_aftersale_no:
                raise RuntimeError(
                    "售后详情编号校验失败："
                    f"期望={verified_aftersale_no}，实际={actual_aftersale_no or '无法读取'}"
                )
        evidence_identity = re.sub(
            r"[^A-Za-z0-9_-]+",
            "",
            selected_aftersale_no
            or str(facts.get("aftersale_no") or "").strip()
            or aftersale_no
            or order_no,
        ) or "unknown"
        evidence_paths: list[str] = []
        if shipped_refund_flow:
            action_evidence = evidence_dir / (
                f"aftersale_{shop.get('id')}_{evidence_identity}"
                "_candidate_1_actions_top.png"
            )
            session.screenshot(int(window.handle), action_evidence)
            evidence_paths.append(str(action_evidence))

        logistics_read = (
            str(facts.get("refund_type") or "").strip() == "退款"
            and "".join(str(facts.get("list_shipping_status") or "").split())
            == "已发货"
            and "退款关闭"
            not in "".join(str(facts.get("aftersale_status") or "").split())
        )
        if logistics_read:
            facts.update(
                session.read_logistics_detail(window, order_no, detail_url, shop)
            )
            if shipped_refund_flow:
                logistics_evidence = evidence_dir / (
                    f"aftersale_{shop.get('id')}_{evidence_identity}"
                    "_candidate_1_logistics_all.png"
                )
                session.screenshot(int(window.handle), logistics_evidence)
                evidence_paths.append(str(logistics_evidence))
        decision = decide_aftersale(facts).to_dict()
        if not evidence_paths:
            evidence = evidence_dir / (
                f"aftersale_{shop.get('id')}_{evidence_identity}_candidate_1.png"
            )
            session.screenshot(int(window.handle), evidence)
            evidence_paths.append(str(evidence))
        candidate_count = int(getattr(session, "last_search_candidate_count", 1))
        facts["aftersale_candidates_inspected"] = [
            {
                "candidate_index": 0,
                "detail_url": detail_url,
                "has_extend_receipt": bool(facts.get("has_extend_receipt")),
                "action_controls_read_complete": bool(
                    facts.get("action_controls_read_complete")
                ),
                "available_aftersale_actions": list(
                    facts.get("available_aftersale_actions") or []
                ),
                "has_agree_refund": bool(facts.get("has_agree_refund")),
                "has_agree_return_after_refund": bool(
                    facts.get("has_agree_return_after_refund")
                ),
                "has_agree_recall_after_refund": bool(
                    facts.get("has_agree_recall_after_refund")
                ),
                "has_reject_refund": bool(facts.get("has_reject_refund")),
                "action": decision.get("action"),
            }
        ]
        facts["aftersale_search_candidate_count"] = candidate_count
        facts["aftersale_search_candidate_rows"] = list(
            getattr(session, "last_search_candidate_rows", []) or []
        )
        facts["aftersale_candidate_policy"] = (
            "SHIPPED_REFUND_NON_CLOSED_SEARCH_RESULT"
            if shipped_refund_flow
            else ("EXACT_AFTERSALE_NO" if aftersale_no else "FIRST_RESULT_ONLY")
        )

        # Keep only a verified actionable detail open for the following write
        # worker. Otherwise close the exact first detail and return it for
        # manual review without inspecting any additional result.
        retain_detail = (
            bool(request.get("keep_detail_open", True))
            and decision.get("action")
            in {
                EXTEND_RECEIPT,
                AGREE_REFUND,
                AGREE_RETURN_AFTER_REFUND,
                AGREE_RECALL_AFTER_REFUND,
                REJECT_REFUND,
            }
        )
        cleanup = None
        if not retain_detail:
            cleanup = session.close_detail(window, shop)
            if not _cleanup_handled(cleanup):
                raise RuntimeError("售后第一条详情页未能安全关闭，已停止处理该订单")
        return {
            "ok": True,
            "operation": operation,
            "facts": facts,
            "decision": decision,
            "detail_url": detail_url,
            "evidence": evidence_paths,
            "detail_cleanup": cleanup,
            "detail_retained": retain_detail,
            "business_write_performed": False,
        }
    if operation == "execute":
        facts = dict(request.get("facts") or {})
        detail_url = str(facts.get("aftersale_detail_url") or facts.get("detail_url") or "")
        order_no = str(facts.get("order_no") or "")
        action = str(request.get("action") or "")
        window = session.activate_detail(shop, detail_url, order_no)
        if not bool(request.get("live")):
            return {
                "ok": True,
                "operation": operation,
                "action": action,
                "preview": True,
                "business_write_performed": False,
            }
        evidence_identity = re.sub(
            r"[^A-Za-z0-9_-]+",
            "",
            str(facts.get("aftersale_no") or order_no),
        ) or "unknown"
        if action in {
            AGREE_REFUND,
            AGREE_RETURN_AFTER_REFUND,
            AGREE_RECALL_AFTER_REFUND,
            REJECT_REFUND,
        }:
            top_detail = session.read_detail(
                window, order_no, detail_url, shop
            )
            if str(top_detail.get("order_no") or "").strip() != order_no:
                raise RuntimeError("售后执行前顶部动作区订单号校验失败")
        before_evidence = evidence_dir / (
            f"aftersale_execute_{shop.get('id')}_{evidence_identity}_{action}"
            "_actions_top_before.png"
        )
        session.screenshot(int(window.handle), before_evidence)
        try:
            result = session.execute_action(
                window,
                action,
                shop=shop,
                order_no=order_no,
                detail_url=detail_url,
                max_agree_refund_trace_count=shipped_refund_trace_limit(facts),
            )
        except Exception:
            failed_evidence = evidence_dir / (
                f"aftersale_execute_{shop.get('id')}_{evidence_identity}_{action}_failed.png"
            )
            try:
                session.screenshot(int(window.handle), failed_evidence)
            except Exception:
                pass
            raise
        post_action_render_ready = False
        wait_for_render = getattr(session, "wait_for_post_action_render", None)
        if callable(wait_for_render):
            post_action_render_ready = bool(
                wait_for_render(
                    window,
                    expected_url=detail_url,
                    order_no=order_no,
                    timeout_seconds=float(
                        config.get("aftersale_post_action_render_wait_seconds", 15)
                    ),
                )
            )
        after_evidence = evidence_dir / (
            f"aftersale_execute_{shop.get('id')}_{evidence_identity}_{action}_after.png"
        )
        session.screenshot(int(window.handle), after_evidence)
        business_write_performed = bool(result.get("business_write_performed"))
        try:
            cleanup = session.close_detail(window, shop)
        except Exception as exc:
            # Cleanup is operational hygiene, not the business outcome.  Once
            # execute_action has returned a verified result, a transient blank
            # repaint must not erase that receipt or turn it into an unsafe
            # retry.  The next list activation can recover the leftover tab.
            cleanup = {
                "handled": False,
                "closed": 0,
                "list_ready": False,
                "disposition": "post_action_cleanup_deferred",
                "error": str(exc),
            }
        return {
            "ok": True,
            "operation": operation,
            "action": action,
            "result": result,
            "evidence": [str(before_evidence), str(after_evidence)],
            "detail_cleanup": cleanup,
            "post_action_render_ready": post_action_render_ready,
            "business_write_performed": business_write_performed,
        }
    if operation == "close":
        facts = dict(request.get("facts") or {})
        detail_url = str(facts.get("aftersale_detail_url") or facts.get("detail_url") or "")
        order_no = str(facts.get("order_no") or "")
        window = session.activate_detail(shop, detail_url, order_no)
        return {
            "ok": True,
            "operation": operation,
            "detail_cleanup": session.close_detail(window, shop),
            "business_write_performed": False,
        }
    raise RuntimeError(f"未知售后工作台 Worker 操作：{operation}")


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
