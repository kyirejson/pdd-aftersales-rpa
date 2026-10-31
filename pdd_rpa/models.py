from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ExecutionMode(StrEnum):
    OBSERVE = "observe"
    APPROVE = "approve"
    AUTO = "auto"


class TaskState(StrEnum):
    DISCOVERED = "DISCOVERED"
    COLLECTING_DATA = "COLLECTING_DATA"
    WAIT_RULE_MATCH = "WAIT_RULE_MATCH"
    READY = "READY"
    NEED_REVIEW = "NEED_REVIEW"
    DATA_CONFLICT = "DATA_CONFLICT"
    WAIT_APPROVAL = "WAIT_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RETRY_PENDING = "RETRY_PENDING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"


class LogisticsStatus(StrEnum):
    NO_INFO = "NO_INFO"
    LABEL_CREATED = "LABEL_CREATED"
    PICKED_UP = "PICKED_UP"
    IN_TRANSIT = "IN_TRANSIT"
    ARRIVED_STATION = "ARRIVED_STATION"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    SIGNED = "SIGNED"
    REJECTED = "REJECTED"
    RETURNING = "RETURNING"
    RETURNED = "RETURNED"
    ABNORMAL = "ABNORMAL"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class Shop:
    id: str
    name: str
    edge_profile: str
    enabled: bool = True


@dataclass(slots=True)
class WorkOrderFacts:
    shop_id: str
    shop_name: str
    edge_profile: str
    work_order_no: str
    order_no: str
    work_order_title: str = ""
    refund_type: str = ""
    refund_reason: str = ""
    refund_amount: float = 0.0
    pdd_order_status: str = ""
    pdd_logistics_status: str = ""
    carrier: str = ""
    tracking_no: str = ""
    jst_outbound_status: str = ""
    jst_outbound_time: str = ""
    jst_tracking_no: str = ""
    wdt_result_count: int = 0
    wdt_order_id: str = ""
    wdt_platform: str = ""
    wdt_order_status: str = ""
    wdt_warehouse: str = ""
    wdt_logistics_company: str = ""
    wdt_product_names: list[str] = field(default_factory=list)
    wdt_eligible: bool = False
    carrier_tracking_status: str = LogisticsStatus.UNKNOWN
    # Latest visible green PDD logistics sentence. In-transit refunds use its
    # operational content; for 示例自营仓, any occurrence of “无锡” can agree.
    latest_logistics_text: str = ""
    work_order_deadline: str = ""
    buyer_description: str = ""
    screenshots: list[str] = field(default_factory=list)
    source_evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def business_key(self) -> str:
        return f"{self.shop_id}:{self.order_no}:{self.work_order_no}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RuleDecision:
    matched: bool
    rule_id: str | None = None
    rule_name: str | None = None
    actions: list[str] = field(default_factory=list)
    approval_required: bool = True
    reasons: list[str] = field(default_factory=list)
