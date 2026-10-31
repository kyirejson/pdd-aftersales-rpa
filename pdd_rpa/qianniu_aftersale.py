"""Pure decision rules for Qianniu unreceived-refund after-sales orders."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any


QIANNIU_UNRECEIVED_REFUND = "千牛未收货退款"
AGREE_REFUND = "agree_refund"
INTERCEPT_EXPRESS = "intercept_express"
MANUAL_REVIEW = "manual_review"

PRE_SHIPMENT_WDT_STATUSES = frozenset({"待客审", "已审核", "已取消", "未审核"})
SHIPPED_WDT_STATUSES = frozenset({"已发货", "已完成"})
WAITING_PICKUP_MARKER = "包裹正在等待揽收"
JST_WAREHOUSE = "示例供应链代发总仓"
SHIPPED_REFUND_TRACE_LIMIT = 5


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


@dataclass(frozen=True, slots=True)
class QianniuAftersaleDecision:
    action: str
    reason_code: str
    reason: str
    requires_group_interception: bool
    requires_dingtalk_registration: bool
    needs_product_routing: bool
    eligible_for_refund: bool
    logistics_trajectory_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_qianniu_unreceived_refund(
    *,
    wdt_order_status: str,
    wdt_warehouse: str,
    logistics_text: str,
    logistics_trajectory_count: int | None = None,
) -> QianniuAftersaleDecision:
    """Apply the operator-approved Qianniu refund/interception rules.

    The decision is pure and performs no UI operation.  In particular,
    interception and DingTalk registration are prerequisites returned to the
    caller; the final platform action must not run before they are completed.
    """

    status = _compact(wdt_order_status)
    warehouse = _compact(wdt_warehouse)
    all_logistics = _compact(logistics_text)
    if status in PRE_SHIPMENT_WDT_STATUSES:
        if WAITING_PICKUP_MARKER in all_logistics:
            return QianniuAftersaleDecision(
                action=AGREE_REFUND,
                reason_code="PRE_SHIPMENT_WAITING_PICKUP",
                reason="旺店通处于未实际发货阶段，且千牛物流显示包裹正在等待揽收",
                requires_group_interception=False,
                requires_dingtalk_registration=False,
                needs_product_routing=False,
                eligible_for_refund=True,
                logistics_trajectory_count=0,
            )
        return QianniuAftersaleDecision(
            action=MANUAL_REVIEW,
            reason_code="PRE_SHIPMENT_LOGISTICS_NOT_WAITING_PICKUP",
            reason="旺店通处于未实际发货阶段，但千牛详情未读取到等待揽收标记",
            requires_group_interception=False,
            requires_dingtalk_registration=False,
            needs_product_routing=False,
            eligible_for_refund=False,
            logistics_trajectory_count=0,
        )

    if status not in SHIPPED_WDT_STATUSES:
        return QianniuAftersaleDecision(
            action=MANUAL_REVIEW,
            reason_code="UNSUPPORTED_WDT_STATUS",
            reason=f"旺店通订单状态不属于已配置自动化范围：{wdt_order_status or '空'}",
            requires_group_interception=False,
            requires_dingtalk_registration=False,
            needs_product_routing=False,
            eligible_for_refund=False,
            logistics_trajectory_count=0,
        )

    try:
        trace_count = int(logistics_trajectory_count)
    except (TypeError, ValueError):
        trace_count = 0
    if trace_count <= 0:
        return QianniuAftersaleDecision(
            action=MANUAL_REVIEW,
            reason_code="SHIPPED_TRAJECTORY_UNREADABLE",
            reason="旺店通已发货/已完成，但千牛悬浮物流框未读取到走件条数",
            requires_group_interception=True,
            requires_dingtalk_registration=False,
            needs_product_routing=warehouse == JST_WAREHOUSE,
            eligible_for_refund=False,
            logistics_trajectory_count=trace_count,
        )
    if warehouse == JST_WAREHOUSE:
        return QianniuAftersaleDecision(
            action=INTERCEPT_EXPRESS,
            reason_code="JST_SHIPPED_INTERCEPT_ONLY",
            reason="示例供应链代发总仓仅未揽收可自动退款；已揽收完成群拦截后点击拦截快递",
            requires_group_interception=True,
            requires_dingtalk_registration=False,
            needs_product_routing=True,
            eligible_for_refund=False,
            logistics_trajectory_count=trace_count,
        )
    if trace_count <= SHIPPED_REFUND_TRACE_LIMIT:
        return QianniuAftersaleDecision(
            action=AGREE_REFUND,
            reason_code="SHIPPED_TRACE_WITHIN_REFUND_LIMIT",
            reason=f"已揽收走件{trace_count}条，不超过5条；群拦截后同意退款并登记钉钉",
            requires_group_interception=True,
            requires_dingtalk_registration=True,
            needs_product_routing=False,
            eligible_for_refund=True,
            logistics_trajectory_count=trace_count,
        )
    return QianniuAftersaleDecision(
        action=INTERCEPT_EXPRESS,
        reason_code="SHIPPED_TRACE_OVER_REFUND_LIMIT",
        reason=f"已揽收走件{trace_count}条，超过5条；群拦截后点击拦截快递，不登记钉钉",
        requires_group_interception=True,
        requires_dingtalk_registration=False,
        needs_product_routing=False,
        eligible_for_refund=False,
        logistics_trajectory_count=trace_count,
    )
