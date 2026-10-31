"""Read-only WDT lookup used by the staged Qianniu after-sale workflow."""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .db import Repository
from .desktop_executor import DesktopComponentExecutor


class QianniuWdtError(RuntimeError):
    pass


BASE_COMPONENTS = (
    "CMP-WDT-ACTIVATE",
    "CMP-WDT_ORDER_MANAGEMENT",
    "CMP-WDT_WAYBILL_INPUT",
    "CMP-WDT_SEARCH",
    "CMP-WDT-WAIT-RESULT",
    "CMP-WDT-RESULT-COUNT",
    "CMP-WDT_WAYBILL",
    "CMP-WDT_ORDER_ID",
    "CMP-WDT_PLATFORM",
    "CMP-WDT_ORIGINAL_ORDER",
    "CMP-WDT_ORDER_STATUS",
    "CMP-WDT-WAREHOUSE",
    "CMP-WDT-LOGISTICS-COMPANY",
)
JST_COMPONENTS = (
    "CMP-WDT-GOODS-LIST-TAB",
    "CMP-WDT-PRODUCT-NAMES",
)
TAOBAO_PLATFORMS = frozenset({"淘宝", "天猫"})
KNOWN_WAREHOUSES = ("示例代发总仓", "示例自营仓")
LOGISTICS_CELL_PATTERN = re.compile(
    r"(圆通|中通|韵达|顺丰|申通|极兔|邮政|京东|德邦|物流|快递|速递|一淘)",
    re.IGNORECASE,
)


def _recover_warehouse_cell(value: Any) -> tuple[str, bool]:
    raw = str(value or "").strip()
    compact = re.sub(r"\s+", "", raw)
    if compact in KNOWN_WAREHOUSES:
        return compact, False
    matches = [item for item in KNOWN_WAREHOUSES if item in compact]
    return (matches[0], True) if len(matches) == 1 else (raw, False)


def _recover_logistics_cell(value: Any) -> tuple[str, bool]:
    raw = str(value or "").strip()
    if "\t" not in raw and "\n" not in raw:
        return raw, False
    cells = [item.strip() for item in re.split(r"[\t\r\n]+", raw) if item.strip()]
    candidates = [item for item in cells if LOGISTICS_CELL_PATTERN.search(item)]
    return (candidates[-1], True) if candidates else (raw, False)


def normalize_qianniu_wdt_grid_context(context: dict[str, Any]) -> dict[str, Any]:
    """Recover WDT cells when its custom grid copies the selected whole row.

    The lookup has already been bound to one exact waybill and one visual row,
    so extracting one unique configured warehouse and the carrier-like cell is
    deterministic even when clipboard selection spans the painted row.
    """

    normalized = dict(context)
    warehouse_raw = str(context.get("wdt_warehouse") or "").strip()
    logistics_raw = str(context.get("wdt_logistics_company") or "").strip()
    warehouse, warehouse_recovered = _recover_warehouse_cell(warehouse_raw)
    logistics, logistics_recovered = _recover_logistics_cell(logistics_raw)
    normalized["wdt_warehouse"] = warehouse
    normalized["wdt_logistics_company"] = logistics
    normalized["wdt_warehouse_raw"] = warehouse_raw
    normalized["wdt_logistics_company_raw"] = logistics_raw
    normalized["wdt_grid_row_recovered"] = bool(
        warehouse_recovered or logistics_recovered
    )
    return normalized


def validate_qianniu_lookup_identifiers(
    order_no: str, tracking_no: str
) -> tuple[str, str]:
    order = str(order_no or "").strip()
    tracking = re.sub(r"\s+", "", str(tracking_no or "")).upper()
    if not re.fullmatch(r"\d{15,24}", order):
        raise QianniuWdtError("千牛订单号格式无效")
    if not re.fullmatch(r"[A-Z0-9]{8,30}", tracking):
        raise QianniuWdtError("千牛物流单号格式无效")
    return order, tracking


def validate_qianniu_wdt_context(
    context: dict[str, Any], *, order_no: str, tracking_no: str
) -> dict[str, Any]:
    context = normalize_qianniu_wdt_grid_context(context)
    expected_tracking = re.sub(r"\s+", "", str(tracking_no or ""))
    actual_tracking = re.sub(r"\s+", "", str(context.get("wdt_waybill") or ""))
    if not expected_tracking or actual_tracking != expected_tracking:
        raise QianniuWdtError("旺店通回读物流单号与千牛详情不一致")
    platform = str(context.get("wdt_platform") or "").strip()
    if platform not in TAOBAO_PLATFORMS:
        raise QianniuWdtError(f"旺店通平台不是淘宝/天猫：{platform or '空'}")
    expected_order = re.sub(r"\s+", "", str(order_no or ""))
    actual_order = re.sub(
        r"\s+", "", str(context.get("wdt_original_order") or "")
    )
    order_matches = bool(expected_order and expected_order in actual_order)
    result_count = int(context.get("wdt_result_count") or 0)
    exact_fallback = (
        result_count == 0
        and bool(str(context.get("wdt_order_id") or "").strip())
        and bool(str(context.get("wdt_order_status") or "").strip())
    )
    if result_count != 1 and not exact_fallback:
        raise QianniuWdtError(f"旺店通稳定后查询结果数量不是1：{result_count}")
    status = str(context.get("wdt_order_status") or "").strip()
    if not status:
        raise QianniuWdtError("旺店通订单状态为空")
    return {
        "ok": True,
        "order_no": expected_order,
        "tracking_no": expected_tracking,
        "wdt_order_id": str(context.get("wdt_order_id") or "").strip(),
        "wdt_original_order": actual_order,
        "original_order_matches": order_matches,
        "platform": platform,
        "order_status": status,
        "warehouse": str(context.get("wdt_warehouse") or "").strip(),
        "warehouse_raw": str(context.get("wdt_warehouse_raw") or "").strip(),
        "logistics_company": str(
            context.get("wdt_logistics_company") or ""
        ).strip(),
        "logistics_company_raw": str(
            context.get("wdt_logistics_company_raw") or ""
        ).strip(),
        "grid_row_recovered": bool(context.get("wdt_grid_row_recovered")),
        "product_names": [
            line.strip()
            for line in str(context.get("wdt_product_names") or "").splitlines()
            if line.strip()
        ],
        "unique_result_verified_by": (
            "exact_tracking_platform_unique_row"
            if exact_fallback
            else "visual_count_tracking_platform_unique_row"
        ),
        "business_write_performed": False,
    }


def run_qianniu_wdt_lookup(
    repository: Repository,
    evidence_dir: Path,
    *,
    order_no: str,
    tracking_no: str,
    executor_factory: Callable[[Path], Any] = DesktopComponentExecutor,
) -> dict[str, Any]:
    order, tracking = validate_qianniu_lookup_identifiers(order_no, tracking_no)
    context: dict[str, Any] = {"order_no": order, "tracking_no": tracking}
    trace: list[dict[str, Any]] = []
    executor = executor_factory(Path(evidence_dir))
    execution_id = f"QIANNIU-WDT:{order}:{uuid.uuid4().hex[:12]}"

    def execute(component_id: str) -> None:
        component = repository.get_component(component_id)
        if not component:
            raise QianniuWdtError(f"千牛旺店通组件不存在：{component_id}")
        started = time.perf_counter()
        try:
            value = executor.execute(component, context)
        except Exception as exc:
            raise QianniuWdtError(
                f"千牛旺店通组件“{component.get('name') or component_id}”失败：{exc}"
            ) from exc
        trace.append(
            {
                "component_id": component_id,
                "name": str(component.get("name") or component_id),
                "status": "success",
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "value": value,
            }
        )

    for component_id in BASE_COMPONENTS:
        execute(component_id)
    context.update(normalize_qianniu_wdt_grid_context(context))
    if str(context.get("wdt_warehouse") or "").strip() == "示例代发总仓":
        for component_id in JST_COMPONENTS:
            execute(component_id)

    result = validate_qianniu_wdt_context(
        context, order_no=order, tracking_no=tracking
    )
    return {**result, "execution_id": execution_id, "trace": trace}
