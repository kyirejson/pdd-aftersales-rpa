"""Cycle-order contract for the staged twelfth Qianniu/Tmall shop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class QianniuCycleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QianniuCycleConfig:
    workflow_enabled: bool
    production_enabled: bool
    scan_position: int
    scan_after_pdd_shop_count: int
    process_after_all_pdd_tasks: bool

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> "QianniuCycleConfig":
        data = dict(raw or {})
        return cls(
            workflow_enabled=bool(data.get("workflow_enabled", False)),
            production_enabled=bool(data.get("production_enabled", False)),
            scan_position=int(data.get("scan_position", 12)),
            scan_after_pdd_shop_count=int(
                data.get("scan_after_pdd_shop_count", 11)
            ),
            process_after_all_pdd_tasks=bool(
                data.get("process_after_all_pdd_tasks", True)
            ),
        )

    def validate(self, pdd_shop_count: int) -> None:
        if self.scan_after_pdd_shop_count != 11 or self.scan_position != 12:
            raise QianniuCycleError("千牛必须固定在11家拼多多之后，作为第12家扫描")
        if int(pdd_shop_count) != self.scan_after_pdd_shop_count:
            raise QianniuCycleError(
                f"启用千牛前必须恰好配置11家拼多多店铺，当前={pdd_shop_count}"
            )
        if not self.process_after_all_pdd_tasks:
            raise QianniuCycleError("千牛处理阶段必须排在本轮全部拼多多任务之后")
        if self.production_enabled and not self.workflow_enabled:
            raise QianniuCycleError("千牛正式执行不能在工作流关闭时单独开启")


def build_cycle_contract(
    pdd_shop_ids: list[str], qianniu_shop_id: str, raw: dict[str, Any] | None
) -> dict[str, Any]:
    config = QianniuCycleConfig.from_raw(raw)
    config.validate(len(pdd_shop_ids))
    scans = [
        {"position": index, "platform": "pdd", "shop_id": shop_id}
        for index, shop_id in enumerate(pdd_shop_ids, start=1)
    ]
    scans.append(
        {
            "position": config.scan_position,
            "platform": "qianniu",
            "shop_id": qianniu_shop_id,
        }
    )
    return {
        "workflow_enabled": config.workflow_enabled,
        "production_enabled": config.production_enabled,
        "scan_order": scans,
        "processing_phases": [
            {
                "phase": 1,
                "platform": "pdd",
                "scope": "all_tasks_in_current_cycle",
            },
            {
                "phase": 2,
                "platform": "qianniu",
                "scope": "eligible_unreceived_refunds",
                "requires_pdd_phase_complete": True,
            },
        ],
    }
