from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
from pathlib import Path
import threading
from typing import Any, Callable

from .base import ConnectorResult, PddConnector
from ..aftersale_uia import PddAftersaleUiaSession, card_to_item
from ..aftersale_workbench import (
    AFTERSALE_RETURN_REFUND,
    AFTERSALE_SHIPPED_REFUND,
)
from ..edge_uia import PddEdgeUiaSession
from ..pdd_scan_worker_client import PddScanWorkerError, run_isolated_pdd_scan


AFTERSALE_REFUND_DISPLAY = "售后工作台：退款"


def _fallback_aftersale_token(card_data: dict[str, Any]) -> str:
    """Create a deterministic non-volatile suffix for legacy list variants."""

    payload = "\x1f".join(
        (
            str(card_data.get("order_no") or "").strip(),
            str(card_data.get("refund_type") or "").strip(),
            str(card_data.get("apply_time") or "").strip(),
            f"{float(card_data.get('refund_amount') or 0):.2f}",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class PddEdgeUiaConnector(PddConnector):
    name = "pdd_edge_uia"
    supports_live_writes = False

    def __init__(
        self,
        config: dict[str, Any],
        evidence_dir: Path,
        session_factory: Callable[[dict[str, Any]], PddEdgeUiaSession] = PddEdgeUiaSession,
        *,
        isolate: bool | None = None,
    ):
        self.config = config
        self.evidence_dir = Path(evidence_dir)
        self.session_factory = session_factory
        # Keep test/fake sessions in-process unless isolation is explicitly
        # enabled.  Production settings enable the child worker because UIA
        # native crashes must not terminate the FastAPI process.
        self.isolate = bool(config.get("scan_worker_isolation", False)) if isolate is None else bool(isolate)

    def health(self) -> ConnectorResult:
        session = None
        try:
            session = self.session_factory(self.config)
            return ConnectorResult(
                True,
                {
                    "mode": "edge_uia",
                    "visible_edge_windows": len(session._windows()),
                    "worker_isolated": self.isolate,
                    "javascript": False,
                    "clipboard": False,
                },
            )
        except Exception as exc:
            return ConnectorResult(False, {"mode": "edge_uia"}, error=str(exc))

    def discover(
        self,
        shop: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
    ) -> ConnectorResult:
        if self.isolate:
            try:
                payload = run_isolated_pdd_scan(
                    project_root=self.evidence_dir.parent.parent,
                    evidence_dir=self.evidence_dir,
                    config=self.config,
                    shop=shop,
                    timeout_seconds=float(self.config.get("scan_worker_timeout_seconds", 180)),
                    cancel_event=cancel_event,
                )
                return ConnectorResult(
                    bool(payload.get("ok")),
                    dict(payload.get("data") or {}),
                    list(payload.get("evidence") or []),
                    payload.get("error"),
                )
            except PddScanWorkerError as exc:
                return ConnectorResult(
                    False,
                    {
                        "items": [],
                        "engine": "edge_uia",
                        "worker_isolated": True,
                        "worker_crashed": bool(exc.crashed),
                        "worker_returncode": exc.returncode,
                    },
                    error=str(exc),
                )
        try:
            session = self.session_factory(self.config)
            # refresh_pending_list owns list recovery.  In particular it must
            # navigate a lone detail tab back to the list instead of the old
            # unsafe Ctrl+W pre-cleanup that could close the whole Edge window.
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            evidence = self.evidence_dir / f"pdd_{shop['id']}_{stamp}_uia.png"
            result = None
            work_order_error = ""
            work_order_succeeded = False
            work_order_evidence: list[str] = []
            items = []
            try:
                result = session.refresh_pending_list(shop)
                login_recovery = getattr(session, "last_login_recovery", None)
                session.screenshot(result.handle, evidence)
                work_order_evidence.append(str(evidence))
                for card in result.cards:
                    items.append({
                        "shop_id": shop["id"], "shop_name": shop["name"], "edge_profile": shop["edge_profile"],
                        "work_order_no": card.order_no, "order_no": card.order_no,
                        "work_order_title": card.work_order_title, "refund_type": "", "refund_reason": card.work_order_title,
                        "refund_amount": 0.0, "pdd_order_status": "", "pdd_logistics_status": "", "carrier": "", "tracking_no": "",
                        "work_order_deadline": "", "buyer_description": "", "screenshots": [],
                        "source_evidence": {
                            "collection_mode": "edge_uia",
                            "task_source": "work_order",
                            "automation_eligible": True,
                            "display_only": False,
                            "list_url": result.url,
                            "window_title": result.title,
                            "scan_screenshot": str(evidence),
                            "uia_card": asdict(card),
                            "page_no": card.page_no,
                            "page_index": card.page_index,
                            "login_recovery": login_recovery,
                        },
                    })
                work_order_succeeded = True
            except Exception as exc:
                # 工单管理和售后工作台是两个独立数据源。工单页面恢复失败时，
                # 仍必须继续采集本店售后，不能再把整家店直接跳过。
                work_order_error = str(exc)
                login_recovery = getattr(session, "last_login_recovery", None)
            aftersale_error = ""
            aftersale_succeeded = False
            aftersale_return_to_work_order: dict[str, Any] = {}
            aftersale_return_error = ""
            aftersale_evidence: list[str] = []
            aftersale_collection_summary: dict[str, Any] = {}
            aftersale_discovered_total = 0
            if bool(self.config.get("aftersale_enabled", False)):
                try:
                    aftersale_session = PddAftersaleUiaSession(self.config)
                    aftersale_result = aftersale_session.scan_pending(shop)
                    aftersale_discovered_total = int(aftersale_result.total)
                    aftersale_collection_summary = dict(
                        getattr(aftersale_session, "last_collection_summary", {}) or {}
                    )
                    aftersale_shot = (
                        self.evidence_dir
                        / f"pdd_{shop['id']}_{stamp}_aftersale_uia.png"
                    )
                    aftersale_session.screenshot(aftersale_result.handle, aftersale_shot)
                    aftersale_evidence.append(str(aftersale_shot))
                    aftersale_succeeded = True
                    seen_orders: dict[str, int] = {}
                    for card in aftersale_result.cards:
                        card_data = card_to_item(card)
                        order_no = str(card.order_no or "").strip()
                        order_occurrence = seen_orders.get(order_no, 0)
                        seen_orders[order_no] = order_occurrence + 1
                        is_primary_order_candidate = order_occurrence == 0
                        is_return_refund = card.refund_type == "退货退款"
                        is_shipped_refund = bool(
                            card.refund_type == "退款"
                            and "".join(str(card.shipping_status or "").split())
                            == "已发货"
                        )
                        automation_eligible = bool(
                            (
                                is_return_refund
                                and (
                                    is_primary_order_candidate
                                    or bool(str(card.aftersale_no or "").strip())
                                )
                            )
                            or is_shipped_refund
                        )
                        if is_primary_order_candidate and is_return_refund:
                            # Preserve the historical key for the first result
                            # only when it is the historically automated
                            # return-refund branch.  An ordinary refund must
                            # never overwrite a completed legacy return task.
                            work_order_no = f"AFTERSALE:{order_no}"
                            persistence_identity = "legacy_primary_order"
                        elif str(card.aftersale_no or "").strip():
                            identity_prefix = "REFUND:" if not is_return_refund else ""
                            work_order_no = (
                                f"AFTERSALE:{identity_prefix}"
                                f"{str(card.aftersale_no).strip()}"
                            )
                            persistence_identity = "aftersale_no"
                        else:
                            fallback_token = _fallback_aftersale_token(card_data)
                            identity_prefix = "REFUND:" if not is_return_refund else ""
                            work_order_no = (
                                f"AFTERSALE:{identity_prefix}{order_no}:"
                                f"FALLBACK:{fallback_token}"
                            )
                            persistence_identity = "stable_fallback"
                        record_identity = (
                            f"aftersale_no:{str(card.aftersale_no).strip()}"
                            if str(card.aftersale_no or "").strip()
                            else f"stable_fallback:{_fallback_aftersale_token(card_data)}"
                        )
                        automation_block_reason = ""
                        if not is_return_refund and not is_shipped_refund:
                            automation_block_reason = "ordinary_refund_not_shipped"
                        elif not automation_eligible:
                            # A sibling without a stable aftersale number cannot
                            # be targeted more precisely than the old first-row
                            # rule, so keep it visible but never auto-execute it.
                            automation_block_reason = "missing_exact_aftersale_no"
                        items.append(
                            {
                                "shop_id": shop["id"],
                                "shop_name": shop["name"],
                                "edge_profile": shop["edge_profile"],
                                "work_order_no": work_order_no,
                                "order_no": order_no,
                                "work_order_title": (
                                    AFTERSALE_RETURN_REFUND
                                    if is_return_refund
                                    else (
                                        AFTERSALE_SHIPPED_REFUND
                                        if is_shipped_refund
                                        else AFTERSALE_REFUND_DISPLAY
                                    )
                                ),
                                "refund_type": card.refund_type,
                                "refund_reason": card.refund_reason,
                                "refund_amount": card.refund_amount,
                                "pdd_order_status": card.shipping_status,
                                "pdd_logistics_status": "",
                                "carrier": "",
                                "tracking_no": "",
                                "work_order_deadline": "",
                                "buyer_description": "",
                                "screenshots": [],
                                "source_evidence": {
                                    "collection_mode": "edge_uia",
                                    "source_system": "aftersale_workbench",
                                    "task_source": "aftersale_workbench",
                                    "queue_scope": "pending_soon_overdue",
                                    "filter_label": "待处理即将逾期",
                                    "automation_eligible": automation_eligible,
                                    "display_only": not automation_eligible,
                                    "automation_block_reason": automation_block_reason,
                                    "record_identity": record_identity,
                                    "persistence_identity": persistence_identity,
                                    "list_url": aftersale_result.url,
                                    "window_title": aftersale_result.title,
                                    "scan_screenshot": str(aftersale_shot),
                                    "aftersale_card": card_data,
                                    "page_no": card.page_no,
                                    "page_index": card.page_index,
                                    "login_recovery": login_recovery,
                                },
                            }
                        )
                    try:
                        aftersale_return_to_work_order = session.restore_work_order_list_after_aftersale(
                            shop,
                            aftersale_result.handle,
                        )
                    except Exception as exc:
                        # Returning the tab to 工单管理 is cleanup, not part of
                        # aftersale capture.  Keep freshly captured aftersales
                        # current even when this cleanup needs another pass.
                        aftersale_return_error = str(exc)
                    # The page/badge total is only a diagnostic signal.  PDD's
                    # virtualized list and transient overlays can make that
                    # number differ from the rows currently exposed through
                    # UIA.  Keep the comparison in ``last_collection_summary``
                    # for troubleshooting, but never turn a count mismatch
                    # into an aftersale source error: every row that was read
                    # successfully must be uploaded to the console immediately.
                except Exception as exc:
                    # The two lists are independent. A temporary workbench
                    # read failure must not discard already captured work
                    # orders or trigger an expensive whole-shop rescan.
                    aftersale_error = str(exc)
            source_succeeded = bool(work_order_succeeded or aftersale_succeeded)
            source_errors = [
                f"工单管理：{work_order_error}" if work_order_error else "",
                f"售后工作台：{aftersale_error}" if aftersale_error else "",
            ]
            return ConnectorResult(
                source_succeeded,
                {
                    "items": items,
                    "total": int(result.total) if result is not None else 0,
                    "work_order_error": work_order_error,
                    "work_order_total": sum(
                        1
                        for item in items
                        if (item.get("source_evidence") or {}).get("task_source")
                        == "work_order"
                    ),
                    "aftersale_discovered_total": aftersale_discovered_total,
                    "aftersale_total": sum(
                        1
                        for item in items
                        if (item.get("source_evidence") or {}).get("source_system")
                        == "aftersale_workbench"
                    ),
                    "aftersale_error": aftersale_error,
                    "aftersale_collection_summary": aftersale_collection_summary,
                    "aftersale_return_to_work_order": aftersale_return_to_work_order,
                    "aftersale_return_error": aftersale_return_error,
                    "shop_name": shop["name"],
                    "engine": "edge_uia",
                    "login_recovery": login_recovery,
                    "pagination_trace": list(getattr(session, "last_pagination_trace", [])),
                },
                evidence=[*work_order_evidence, *aftersale_evidence],
                error=(
                    None
                    if source_succeeded
                    else "；".join(item for item in source_errors if item)
                ),
            )
        except Exception as exc:
            return ConnectorResult(
                False,
                {
                    "items": [],
                    "engine": "edge_uia",
                    "pagination_trace": list(getattr(session, "last_pagination_trace", [])),
                },
                error=str(exc),
            )
