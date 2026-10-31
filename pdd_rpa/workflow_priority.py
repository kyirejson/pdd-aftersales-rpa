from __future__ import annotations

from copy import deepcopy
from typing import Any

from .aftersale_workbench import AFTERSALE_RETURN_REFUND, AFTERSALE_SHIPPED_REFUND


SETTING_KEY = "workflow_priority_config"

SHIPPED_NO_TRACE = "已发货无轨迹退款处理"
IN_TRANSIT_NO_REASON = "在途无理由退款处理"
CONFIRM_EARLIER_SHIPPING = "确认更早发货时间"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 5,
    "workflows": [
        {"workflow_key": "shipped_no_trace", "title": SHIPPED_NO_TRACE, "priority": 100, "enabled": True},
        {"workflow_key": "in_transit_no_reason", "title": IN_TRANSIT_NO_REASON, "priority": 50, "enabled": True},
        {"workflow_key": "confirm_earlier_shipping", "title": CONFIRM_EARLIER_SHIPPING, "priority": 40, "enabled": True},
        {"workflow_key": "aftersale_return_refund", "title": AFTERSALE_RETURN_REFUND, "priority": 10, "enabled": True},
        {"workflow_key": "aftersale_shipped_refund", "title": AFTERSALE_SHIPPED_REFUND, "priority": 9, "enabled": True},
    ],
}


class WorkflowPriorityError(ValueError):
    pass


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(DEFAULT_CONFIG)
    result.update(deepcopy(dict(config or {})))
    workflows = list(result.get("workflows") or [])
    required = {
        "shipped_no_trace",
        "in_transit_no_reason",
        "confirm_earlier_shipping",
        "aftersale_return_refund",
        "aftersale_shipped_refund",
    }
    keys = [str(item.get("workflow_key", "")) for item in workflows]
    if len(keys) != len(set(keys)) or not set(keys).issubset(required):
        raise WorkflowPriorityError("流程优先级包含重复或未知流程")
    for default in DEFAULT_CONFIG["workflows"]:
        key = str(default["workflow_key"])
        if key not in keys:
            workflows.append(deepcopy(default))
            keys.append(key)
    if set(keys) != required or len(keys) != len(required):
        raise WorkflowPriorityError(
            "必须且只能配置三类工单流程及两类售后工作台流程"
        )
    for item in workflows:
        try:
            priority = int(item.get("priority"))
        except (TypeError, ValueError) as exc:
            raise WorkflowPriorityError("流程优先级必须是整数") from exc
        if priority < 0 or priority > 1000:
            raise WorkflowPriorityError("流程优先级必须在 0 到 1000 之间")
        item["priority"] = priority
        item["enabled"] = bool(item.get("enabled", True))
    result["workflows"] = workflows
    result["version"] = int(result.get("version", 1))
    return result


def prioritize_tasks(tasks: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    config = validate_config(config)
    by_title = {item["title"]: item for item in config["workflows"]}
    ranked: list[dict[str, Any]] = []
    for task in tasks:
        item = dict(task)
        workflow = by_title.get(str(task.get("facts", {}).get("work_order_title", "")))
        item["workflow_key"] = workflow["workflow_key"] if workflow else "other"
        item["queue_priority"] = workflow["priority"] if workflow and workflow["enabled"] else 0
        item["workflow_enabled"] = bool(workflow and workflow["enabled"])
        ranked.append(item)
    def scan_order(item: dict[str, Any]) -> int:
        value = (item.get("facts") or {}).get("source_evidence", {}).get("scan_order")
        try:
            return int(value)
        except (TypeError, ValueError):
            # Legacy tasks have no page metadata; retain their previous
            # deadline/discovery ordering after newly scanned page-aware tasks.
            return 10**18

    ranked.sort(
        key=lambda item: (
            -int(item["queue_priority"]),
            scan_order(item),
            str(item.get("facts", {}).get("work_order_deadline") or "9999-12-31T23:59:59"),
            str(item.get("created_at", "")),
            int(item.get("id", 0)),
        )
    )
    for index, item in enumerate(ranked, start=1):
        item["queue_rank"] = index
    return ranked
