"""Interception routing adapter for Qianniu/Tmall after-sale tasks."""

from __future__ import annotations

from typing import Any

from .in_transit_routing import decide_interception, validate_config


def _carrier_matches(value: str, mapping: dict[str, Any]) -> bool:
    names = {
        str(mapping.get("carrier") or "").strip(),
        *(str(item or "").strip() for item in mapping.get("aliases") or []),
    }
    return str(value or "").strip() in {name for name in names if name}


def decide_qianniu_interception(
    inputs: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """Reuse configured groups without requiring PDD-style WDT name suffixes.

    Product-routed warehouses remain fully delegated to the existing router.
    For 示例自营仓, Qianniu's detail page gives the canonical carrier and is the
    exact source used for group selection; labels such as ``韵达一淘1`` in WDT
    therefore do not block the Tmall interception branch.
    """

    validated = validate_config(config)
    standard = decide_interception(inputs, validated)
    if standard.get("ok"):
        routing_only = dict(standard)
        for obsolete_key in (
            "final_action",
            "force_reject_after_interception",
            "fixed_reject",
        ):
            routing_only.pop(obsolete_key, None)
        return routing_only
    warehouse = str(inputs.get("wdt_warehouse") or "").strip()
    if warehouse != "示例自营仓":
        return standard
    carrier = str(inputs.get("carrier") or "").strip()
    matches = [
        item
        for item in validated.get("carrier_groups") or []
        if item.get("enabled", True) and _carrier_matches(carrier, item)
    ]
    if len(matches) != 1:
        return {
            "ok": False,
            "manual_review": True,
            "reason_code": "QIANNIU_CARRIER_GROUP_NOT_UNIQUE",
            "reason": f"千牛承运商 {carrier or '空'} 的拦截群映射数量应为1，实际为{len(matches)}",
        }
    tracking_no = str(inputs.get("tracking_no") or "").strip()
    if not tracking_no:
        return {"ok": False, "manual_review": True, "reason": "快递单号为空"}
    message = str(
        validated.get("message_template") or "{tracking_no}+拦截"
    ).format(tracking_no=tracking_no, carrier=carrier)
    return {
        "ok": True,
        "manual_review": False,
        "wdt_warehouse": warehouse,
        "wdt_warehouse_raw": warehouse,
        "wdt_warehouse_read_source": "exact_cell",
        "route_type": "QIANNIU_DIRECT_CARRIER",
        "rule_id": "QIANNIU-DETAIL-CARRIER",
        "logistics_company": str(inputs.get("wdt_logistics_company") or "").strip(),
        "tracking_no": tracking_no,
        "carrier": carrier,
        "group": matches[0],
        "message": message,
        "evidence_source": "QIANNIU_DETAIL_CARRIER",
    }
