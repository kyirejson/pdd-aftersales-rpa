from __future__ import annotations

from dataclasses import dataclass
from typing import Any


AFTERSALE_RETURN_REFUND = "售后工作台：退货退款"
AFTERSALE_SHIPPED_REFUND = "售后工作台：已发货-退款"

EXTEND_RECEIPT = "EXTEND_RECEIPT"
AGREE_REFUND = "AGREE_REFUND"
AGREE_RETURN_AFTER_REFUND = "AGREE_RETURN_AFTER_REFUND"
AGREE_RECALL_AFTER_REFUND = "AGREE_RECALL_AFTER_REFUND"
REJECT_REFUND = "REJECT_REFUND"
SKIP_REFUND_CLOSED = "SKIP_REFUND_CLOSED"
NEED_REVIEW = "NEED_REVIEW"


@dataclass(frozen=True, slots=True)
class AftersaleDecision:
    action: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
        }


def shipped_refund_trace_limit(facts: dict[str, Any]) -> int:
    """Return the direct-refund trajectory limit for the verified WDT warehouse.

    The detail collector runs before WDT, so an empty warehouse keeps the
    legacy three-row limit long enough to retain the actionable detail.  The
    runtime must call ``decide_aftersale`` again immediately after WDT; a
    supplier drop-ship warehouse then tightens the limit to one row.
    """

    warehouse = "".join(str(facts.get("wdt_warehouse") or "").split())
    return 1 if ("聚水潭" in warehouse or "示例供应链" in warehouse) else 3


def decide_aftersale(facts: dict[str, Any]) -> AftersaleDecision:
    """Choose an aftersale action only from completely verified page facts."""

    aftersale_status = "".join(
        str(facts.get("aftersale_status") or "").split()
    )
    ignored_status = next(
        (
            marker
            for marker in ("退款关闭", "退款成功")
            if marker in aftersale_status
        ),
        "",
    )
    if ignored_status:
        return AftersaleDecision(
            SKIP_REFUND_CLOSED,
            f"售后状态包含{ignored_status}，按规则忽略",
        )

    refund_type = str(facts.get("refund_type") or "").strip()
    if refund_type == "退款":
        list_shipping_status = "".join(
            str(
                facts.get("list_shipping_status")
                or facts.get("pdd_order_status")
                or ""
            ).split()
        )
        if list_shipping_status != "已发货":
            return AftersaleDecision(
                NEED_REVIEW,
                f"普通退款列表发货状态不是已发货：{list_shipping_status or '空'}",
            )
        if not bool(facts.get("logistics_read_complete")):
            return AftersaleDecision(
                NEED_REVIEW,
                "普通退款物流轨迹尚未完整展开并读取",
            )
        try:
            trace_count = int(facts.get("logistics_trajectory_count"))
        except (TypeError, ValueError):
            return AftersaleDecision(NEED_REVIEW, "普通退款物流轨迹条数无效")
        if trace_count < 0:
            return AftersaleDecision(NEED_REVIEW, "普通退款物流轨迹条数无效")
        trace_limit = shipped_refund_trace_limit(facts)
        warehouse = "".join(str(facts.get("wdt_warehouse") or "").split())
        if trace_count <= trace_limit:
            threshold_reason = (
                "示例供应链仓不超过一条"
                if trace_limit == 1
                else (
                    "示例自营仓不超过三条"
                    if "示例自营仓" in warehouse
                    else "不超过三条"
                )
            )
            return AftersaleDecision(
                AGREE_REFUND,
                f"已发货普通退款完整物流轨迹共 {trace_count} 条，{threshold_reason}",
            )
        if not bool(facts.get("action_controls_read_complete")):
            return AftersaleDecision(
                NEED_REVIEW,
                f"已发货普通退款完整物流轨迹共 {trace_count} 条，"
                "但售后动作控件尚未完整读取",
            )
        has_return = bool(facts.get("has_agree_return_after_refund"))
        has_recall = bool(facts.get("has_agree_recall_after_refund"))
        if has_return and has_recall:
            return AftersaleDecision(
                NEED_REVIEW,
                "同意快递退回后退款与同意召回后退款异常地同时出现，停止自动处理",
            )
        if has_return:
            return AftersaleDecision(
                AGREE_RETURN_AFTER_REFUND,
                f"已发货普通退款完整物流轨迹共 {trace_count} 条，"
                "优先同意快递退回后退款",
            )
        if has_recall:
            return AftersaleDecision(
                AGREE_RECALL_AFTER_REFUND,
                f"已发货普通退款完整物流轨迹共 {trace_count} 条，"
                "页面提供同意召回后退款",
            )
        if bool(facts.get("has_reject_refund")):
            return AftersaleDecision(
                REJECT_REFUND,
                f"已发货普通退款完整物流轨迹共 {trace_count} 条，"
                "没有同意快递退回后退款操作，改走驳回退款",
            )
        return AftersaleDecision(
            NEED_REVIEW,
            f"已发货普通退款完整物流轨迹共 {trace_count} 条，"
            "但没有可安全执行的退回后退款或驳回退款操作",
        )
    if refund_type != "退货退款":
        return AftersaleDecision(
            NEED_REVIEW,
            f"售后工作台不自动处理该售后类型：{refund_type or '空'}",
        )
    if bool(facts.get("has_extend_receipt")):
        return AftersaleDecision(EXTEND_RECEIPT, "退货退款详情存在延长收货操作")
    return AftersaleDecision(NEED_REVIEW, "退货退款详情没有延长收货操作")


__all__ = [
    "AFTERSALE_RETURN_REFUND",
    "AFTERSALE_SHIPPED_REFUND",
    "EXTEND_RECEIPT",
    "AGREE_REFUND",
    "AGREE_RETURN_AFTER_REFUND",
    "AGREE_RECALL_AFTER_REFUND",
    "REJECT_REFUND",
    "SKIP_REFUND_CLOSED",
    "NEED_REVIEW",
    "AftersaleDecision",
    "shipped_refund_trace_limit",
    "decide_aftersale",
]
