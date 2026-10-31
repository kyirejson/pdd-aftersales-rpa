import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from pdd_rpa.edge_uia import PddOrderNotFoundError
from pdd_rpa.aftersale_workbench import (
    AFTERSALE_RETURN_REFUND,
    AFTERSALE_SHIPPED_REFUND,
    AGREE_REFUND,
    AGREE_RETURN_AFTER_REFUND,
    NEED_REVIEW,
    REJECT_REFUND,
)
from pdd_rpa.models import TaskState
from pdd_rpa.runtime_controller import RuntimeController, RuntimeControllerError
from pdd_rpa.workflow_priority import SHIPPED_NO_TRACE
from pdd_rpa.qianniu_aftersale import QIANNIU_UNRECEIVED_REFUND


class FakeRepository:
    def __init__(self):
        self.settings = {}
        self.task_statuses = {}

    def get_json_setting(self, key, default=None):
        return self.settings.get(key, dict(default or {}))

    def set_json_setting(self, key, value):
        self.settings[key] = value

    def is_paused(self):
        return False

    def mark_task_status(self, task_id, target, reason):
        self.task_statuses[int(task_id)] = (target, reason)

    def get_workflow(self, workflow_id):
        return {"workflow_id": workflow_id}


class FakeOrchestrator:
    def __init__(self):
        self.repository = FakeRepository()
        self.config = SimpleNamespace(
            live_actions_enabled=False,
            execution_mode="observe",
            raw={"safety": {"global_failure_circuit_breaker": 3}},
        )
        self.calls = []
        self.detail_options = []
        self.preview_options = []

    def discover_live_readonly(self):
        self.calls.append("discover")
        return {"results": [{"ok": True, "task_ids": [1]}]}

    def list_prioritized_tasks(self, view="all"):
        return [{"id": 1, "queue_priority": 100, "facts": {"work_order_title": "已发货无轨迹退款处理", "shop_name": "测试店", "order_no": "ORDER-1"}}]

    def list_prioritized_tasks_by_ids(self, task_ids):
        selected = {int(item) for item in task_ids}
        return [task for task in self.list_prioritized_tasks() if int(task["id"]) in selected]

    def collect_task_detail_readonly(self, task_id, *, keep_detail_open=False):
        self.calls.append("detail")
        self.detail_options.append(keep_detail_open)
        return {"tracking_no": "TRACK-1", "detail_retained": keep_detail_open}

    def check_wdt_by_components(self, task_id):
        self.calls.append("wdt")
        return {"order_status": "待客审"}

    def preview_refund_components(self, task_id, *, retain_detail=False):
        self.calls.append("preview")
        self.preview_options.append(retain_detail)
        return {"capture_pending": [], "detail_retained": retain_detail}


class IneligibleOrchestrator(FakeOrchestrator):
    def check_wdt_by_components(self, task_id):
        raise RuntimeError("旺店通订单状态不允许退款：已完成")


class NotFoundOrchestrator(FakeOrchestrator):
    def collect_task_detail_readonly(self, task_id, *, keep_detail_open=False):
        raise PddOrderNotFoundError("订单在当前列表不存在")


class ProductionOrchestrator(FakeOrchestrator):
    def __init__(self):
        super().__init__()
        self.config.live_actions_enabled = True
        self.config.execution_mode = "auto"
        self.repository.settings["refund_uia_regression"] = {"confirmed": True}
        self.repository.get_task = lambda task_id: {
            "id": task_id,
            "facts": {
                "order_no": "ORDER-1",
                "refund_amount": 12.34,
            },
        }

    def execute_refund_pilot(
        self,
        task_id,
        confirm_order,
        confirm_amount,
        *,
        reconcile_inline=False,
        close_inline=False,
    ):
        self.calls.append("execute")
        assert reconcile_inline is True
        assert close_inline is True
        return {
            "refund_amount": confirm_amount,
            "success": {"refund_processing": True},
            "preflight": {"capture_pending": [], "detail_retained": True},
            "reconciliation": {"success": {"refund_processing": True}},
            "detail_cleanup": {"closed": 1, "list_ready": True},
        }

    def close_active_shop_detail(self, shop_id):
        self.calls.append("fallback_cleanup")
        return {"closed": 1, "list_ready": True}


def test_dry_run_processes_queue_in_single_worker():
    orchestrator = FakeOrchestrator()
    controller = RuntimeController(orchestrator)
    controller.start("dry_run", ["shipped_no_trace"])
    for _ in range(100):
        status = controller.status()
        if status["state"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.01)
    assert status["state"] == "COMPLETED"
    assert status["counts"]["completed"] == 1
    assert status["results"][0]["status"] == "READY_FOR_PRODUCTION"
    assert orchestrator.calls == ["discover", "detail", "wdt", "preview"]
    assert orchestrator.detail_options == [True]
    assert orchestrator.preview_options == [True]


def test_start_accepts_shipped_refund_aftersale_workflow_key():
    orchestrator = FakeOrchestrator()
    orchestrator.config.raw["pdd_readonly"] = {"aftersale_enabled": True}
    controller = RuntimeController(orchestrator)
    controller._run = lambda: None

    status = controller.start(
        "dry_run",
        ["aftersale_shipped_refund"],
        continuous=False,
    )

    assert status["workflow_keys"] == ["aftersale_shipped_refund"]
    assert status["state"] == "STARTING"


def test_qianniu_queue_is_after_all_pdd_tasks_even_with_higher_priority():
    orchestrator = FakeOrchestrator()
    orchestrator.list_prioritized_tasks = lambda view="all": [
        {
            "id": 2,
            "queue_priority": 9999,
            "state": TaskState.DISCOVERED,
            "facts": {
                "shop_id": "qianniu_01",
                "shop_name": "示例天猫旗舰店",
                "order_no": "5127711625084043334",
                "work_order_title": QIANNIU_UNRECEIVED_REFUND,
                "source_evidence": {
                    "source_system": "qianniu_aftersale",
                    "automation_eligible": True,
                    "shop_scan_order": 11,
                },
            },
        },
        {
            "id": 1,
            "queue_priority": 1,
            "state": TaskState.DISCOVERED,
            "facts": {
                "shop_id": "shop_01",
                "shop_name": "拼多多店铺",
                "order_no": "PDD-ORDER",
                "work_order_title": SHIPPED_NO_TRACE,
                "source_evidence": {"shop_scan_order": 0},
            },
        },
    ]
    controller = RuntimeController(orchestrator)

    queue = controller._build_queue(
        {"results": [{"ok": True, "task_ids": [1, 2]}]},
        ["shipped_no_trace", "qianniu_unreceived_refund"],
    )

    assert [item["task_id"] for item in queue] == [1, 2]


def test_queue_only_processes_current_live_queue_without_discovery():
    orchestrator = FakeOrchestrator()
    orchestrator.repository.settings["live_sync_snapshot"] = {
        "run_id": "existing-sync",
        "in_progress": False,
        "display_task_ids": [1],
    }
    controller = RuntimeController(orchestrator)

    controller.start(
        "dry_run",
        ["shipped_no_trace"],
        continuous=True,
        queue_only=True,
    )
    for _ in range(100):
        status = controller.status()
        if status["state"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.01)

    assert status["state"] == "COMPLETED"
    assert status["queue_only"] is True
    assert status["continuous"] is False
    assert status["queue_snapshot_run_id"] == "existing-sync"
    assert status["queue_task_ids"] == [1]
    assert status["last_sync"]["run_id"] == "existing-sync"
    assert status["phase"] == "当前实时队列单轮处理完成"
    assert orchestrator.calls == ["detail", "wdt", "preview"]


def test_production_queue_only_forces_single_cycle_and_skips_discovery():
    orchestrator = ProductionOrchestrator()
    orchestrator.repository.settings["live_sync_snapshot"] = {
        "run_id": "production-sync",
        "in_progress": False,
        "display_task_ids": [1],
    }
    controller = RuntimeController(orchestrator)

    controller.start(
        "production",
        ["shipped_no_trace"],
        continuous=True,
        queue_only=True,
    )
    for _ in range(100):
        status = controller.status()
        if status["state"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.01)

    assert status["state"] == "COMPLETED"
    assert status["continuous"] is False
    assert status["queue_task_ids"] == [1]
    assert orchestrator.calls == ["detail", "wdt", "execute"]


def test_queue_only_freezes_pending_view_and_excludes_history():
    class ScopedQueueOrchestrator(FakeOrchestrator):
        def __init__(self):
            super().__init__()
            self.requested_views = []

        def list_prioritized_tasks(self, view="all"):
            self.requested_views.append(view)
            current = super().list_prioritized_tasks(view)
            history = {
                "id": 2,
                "queue_priority": 100,
                "facts": {
                    "work_order_title": "已发货无轨迹退款处理",
                    "shop_name": "历史店铺",
                    "order_no": "HISTORY-2",
                },
            }
            return current if view == "pending" else [*current, history]

    orchestrator = ScopedQueueOrchestrator()
    orchestrator.repository.settings["live_sync_snapshot"] = {
        "run_id": "scope-sync",
        "in_progress": False,
        "display_task_ids": [1],
    }
    controller = RuntimeController(orchestrator)

    controller.start("dry_run", ["shipped_no_trace"], queue_only=True)
    for _ in range(100):
        status = controller.status()
        if status["state"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.01)

    assert status["queue_task_ids"] == [1]
    assert status["counts"]["total"] == 1
    assert orchestrator.requested_views[0] == "pending"


def test_queue_only_recovery_never_recaptures_when_frozen_ids_are_missing():
    orchestrator = FakeOrchestrator()
    orchestrator.repository.settings["live_sync_snapshot"] = {
        "run_id": "newer-unconfirmed-sync",
        "in_progress": False,
        "display_task_ids": [1],
    }
    controller = RuntimeController(orchestrator)

    with pytest.raises(RuntimeControllerError, match="已禁止自动改取新队列"):
        controller.start(
            "dry_run",
            ["shipped_no_trace"],
            queue_only=True,
            _recovery=True,
            _frozen_queue_task_ids=[],
            _queue_snapshot_run_id="",
        )


def test_queue_only_requires_an_existing_live_snapshot():
    controller = RuntimeController(FakeOrchestrator())

    with pytest.raises(RuntimeControllerError, match="尚无有效的实时同步快照"):
        controller.start("dry_run", ["shipped_no_trace"], queue_only=True)


def test_queue_only_rejects_a_live_queue_without_supported_targets():
    orchestrator = FakeOrchestrator()
    orchestrator.repository.settings["live_sync_snapshot"] = {"run_id": "existing-sync"}
    orchestrator.list_prioritized_tasks = lambda view="all": [
        {
            "id": 9,
            "queue_priority": 0,
            "facts": {"work_order_title": "商品少发", "order_no": "OTHER-9"},
        }
    ]
    controller = RuntimeController(orchestrator)

    with pytest.raises(RuntimeControllerError, match="没有可处理的目标工单或售后单"):
        controller.start("dry_run", ["shipped_no_trace"], queue_only=True)


def test_queue_only_rejects_snapshot_while_shop_sync_is_in_progress():
    orchestrator = FakeOrchestrator()
    orchestrator.repository.settings["live_sync_snapshot"] = {
        "run_id": "sync-still-running",
        "in_progress": True,
        "display_task_ids": [1],
    }
    controller = RuntimeController(orchestrator)

    with pytest.raises(RuntimeControllerError, match="店铺同步仍在进行"):
        controller.start("dry_run", ["shipped_no_trace"], queue_only=True)


def test_immediate_stop_freezes_live_queue_snapshot():
    class FreezeOrchestrator(FakeOrchestrator):
        def __init__(self):
            super().__init__()
            self.live_queue_frozen = False

        def finalize_live_sync_after_stop(self):
            self.live_queue_frozen = True
            return {"interrupted": True}

    orchestrator = FreezeOrchestrator()
    controller = RuntimeController(orchestrator)
    controller._status["state"] = "RUNNING"

    result = controller.stop()

    assert result["immediate_stop_requested"] is True
    assert orchestrator.live_queue_frozen is True


def test_production_is_blocked_by_readiness_gate():
    controller = RuntimeController(FakeOrchestrator())
    with pytest.raises(RuntimeControllerError, match="live_actions_enabled"):
        controller.start("production", ["shipped_no_trace"])


def test_production_uses_one_inline_refund_worker_and_skips_fallback_cleanup():
    orchestrator = ProductionOrchestrator()
    controller = RuntimeController(orchestrator)
    controller.start("production", ["shipped_no_trace"], continuous=False)
    for _ in range(100):
        status = controller.status()
        if status["state"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.01)
    assert status["state"] == "COMPLETED"
    assert status["counts"]["completed"] == 1
    assert orchestrator.calls == ["discover", "detail", "wdt", "execute"]
    assert [step["step"] for step in status["results"][0]["steps"]] == [
        "PDD_DETAIL",
        "WDT_CHECK",
        "PDD_REFUND_PREVIEW",
        "PDD_REFUND_EXECUTE",
        "PDD_REFUND_RECONCILE",
        "PDD_LIST_RECOVER",
    ]


def test_in_transit_production_is_blocked_without_captured_message_components():
    orchestrator = ProductionOrchestrator()
    controller = RuntimeController(orchestrator)
    with pytest.raises(RuntimeControllerError, match="消息发送组件状态未知"):
        controller.start("production", ["in_transit_no_reason"], continuous=False)


def test_queue_processes_one_shop_no_trace_before_that_shop_in_transit():
    orchestrator = FakeOrchestrator()
    orchestrator.list_prioritized_tasks = lambda: [
        {
            "id": 1,
            "queue_priority": 50,
            "facts": {"work_order_title": "在途无理由退款处理", "shop_name": "店一", "order_no": "I-1", "source_evidence": {"shop_scan_order": 0, "page_no": 1, "page_index": 0}},
        },
        {
            "id": 2,
            "queue_priority": 100,
            "facts": {"work_order_title": "已发货无轨迹退款处理", "shop_name": "店一", "order_no": "S-1", "source_evidence": {"shop_scan_order": 0, "page_no": 2, "page_index": 1}},
        },
        {
            "id": 3,
            "queue_priority": 100,
            "facts": {"work_order_title": "已发货无轨迹退款处理", "shop_name": "店二", "order_no": "S-2", "source_evidence": {"shop_scan_order": 1, "page_no": 1, "page_index": 0}},
        },
        {
            "id": 4,
            "queue_priority": 40,
            "facts": {"work_order_title": "确认更早发货时间", "shop_name": "店一", "order_no": "E-1", "source_evidence": {"shop_scan_order": 0, "page_no": 1, "page_index": 2}},
        },
        {
            "id": 6,
            "queue_priority": 10,
            "facts": {"work_order_title": AFTERSALE_RETURN_REFUND, "shop_name": "店一", "order_no": "R-1", "source_evidence": {"shop_scan_order": 0, "page_no": 1, "page_index": 0}},
        },
    ]
    controller = RuntimeController(orchestrator)
    queue = controller._build_queue(
        {"results": [{"ok": True, "task_ids": [1, 2, 3, 4, 6]}]},
        [
            "shipped_no_trace",
            "in_transit_no_reason",
            "confirm_earlier_shipping",
            "aftersale_return_refund",
        ],
    )
    assert [item["order_no"] for item in queue] == [
        "S-1",
        "I-1",
        "E-1",
        "R-1",
        "S-2",
    ]


def test_build_queue_loads_full_scan_beyond_dashboard_history_cap():
    """Regression: shops scanned early must not fall out of the runtime queue.

    The dashboard caps list_prioritized_tasks at the 200 most recently
    updated rows.  A full multi-shop scan touches more rows than that, so
    the first-scanned shop's work orders can drop out of that window once
    later shops refresh their own rows.  The runtime queue must instead
    load the exact task ids confirmed by the current scan.
    """

    class CappedHistoryOrchestrator(FakeOrchestrator):
        def __init__(self):
            super().__init__()
            self.requested_task_ids = None

        def list_prioritized_tasks(self, view="all", task_ids=None):
            full = [
                {
                    "id": 1,
                    "queue_priority": 100,
                    "state": TaskState.DISCOVERED,
                    "facts": {
                        "shop_id": "shop_01",
                        "shop_name": "最早扫描店铺",
                        "order_no": "TICKET-1",
                        "work_order_title": SHIPPED_NO_TRACE,
                        "source_evidence": {
                            "shop_scan_order": 0,
                            "page_no": 1,
                            "page_index": 0,
                        },
                    },
                },
                {
                    "id": 2,
                    "queue_priority": 10,
                    "state": TaskState.DISCOVERED,
                    "facts": {
                        "shop_id": "shop_01",
                        "shop_name": "最早扫描店铺",
                        "order_no": "RETURN-1",
                        "work_order_title": AFTERSALE_RETURN_REFUND,
                        "source_evidence": {
                            "source_system": "aftersale_workbench",
                            "automation_eligible": True,
                            "display_only": False,
                            "shop_scan_order": 0,
                            "page_no": 1,
                            "page_index": 0,
                        },
                    },
                },
            ]
            if task_ids is None:
                # Dashboard window: only the newest row survived the cap.
                return [task for task in full if task["id"] == 2]
            self.requested_task_ids = list(task_ids)
            return [task for task in full if int(task["id"]) in set(task_ids)]

    orchestrator = CappedHistoryOrchestrator()
    controller = RuntimeController(orchestrator)

    queue = controller._build_queue(
        {"results": [{"ok": True, "task_ids": [1, 2]}]},
        [
            "shipped_no_trace",
            "in_transit_no_reason",
            "confirm_earlier_shipping",
            "aftersale_return_refund",
        ],
    )

    assert orchestrator.requested_task_ids == [1, 2]
    assert [item["order_no"] for item in queue] == ["TICKET-1", "RETURN-1"]


def test_aftersale_queue_skips_display_only_and_stale_but_keeps_legacy_return_tasks():
    orchestrator = FakeOrchestrator()
    tasks = [
        {
            "id": 1,
            "queue_priority": 10,
            "facts": {
                "work_order_title": AFTERSALE_RETURN_REFUND,
                "order_no": "DISPLAY-ONLY",
                "source_evidence": {
                    "source_system": "aftersale_workbench",
                    "automation_eligible": False,
                    "display_only": True,
                },
            },
        },
        {
            "id": 2,
            "source_stale": True,
            "queue_priority": 10,
            "facts": {
                "work_order_title": AFTERSALE_RETURN_REFUND,
                "order_no": "STALE-TOP",
            },
        },
        {
            "id": 3,
            "queue_priority": 10,
            "facts": {
                "work_order_title": AFTERSALE_RETURN_REFUND,
                "order_no": "STALE-EVIDENCE",
                "source_evidence": {"source_stale": True},
            },
        },
        {
            "id": 4,
            "queue_priority": 10,
            "facts": {
                "work_order_title": AFTERSALE_RETURN_REFUND,
                "order_no": "LEGACY-RETURN",
                "source_evidence": {"source_system": "aftersale_workbench"},
            },
        },
        {
            "id": 5,
            "queue_priority": 10,
            "facts": {
                "work_order_title": AFTERSALE_RETURN_REFUND,
                "order_no": "CURRENT-RETURN",
                "source_evidence": {
                    "source_system": "aftersale_workbench",
                    "automation_eligible": True,
                    "display_only": False,
                },
            },
        },
    ]
    controller = RuntimeController(orchestrator)

    queue = controller._build_queue(
        {
            "results": [{"ok": True, "task_ids": [1, 2, 3, 4, 5]}],
            "_tasks": tasks,
        },
        ["aftersale_return_refund"],
    )

    assert [item["order_no"] for item in queue] == [
        "LEGACY-RETURN",
        "CURRENT-RETURN",
    ]


def test_aftersale_shipped_refund_dry_run_builds_full_route_without_writes():
    class AftersaleShippedOrchestrator(FakeOrchestrator):
        def collect_aftersale_detail(self, task_id, *, keep_detail_open=True):
            self.calls.append("aftersale_detail")
            return {
                "decision": {"action": AGREE_REFUND, "reason": "三条以内"},
                "facts": {
                    "refund_type": "退款",
                    "logistics_trajectory_count": 3,
                },
                "detail_cleanup": {},
            }

        def check_wdt_by_components(self, task_id):
            self.calls.append("wdt")
            return {
                "order_status": "已发货",
                "warehouse": "示例供应链代发总仓",
                "logistics_company": "韵达快递",
                "requires_interception": True,
            }

        def prepare_in_transit_routing(self, task_id):
            self.calls.append("route")
            return {
                "ok": True,
                "route_type": "carrier_group",
                "group": {"platform": "企业微信", "group_name": "测试群"},
            }

        def close_aftersale_detail(self, task_id):
            self.calls.append("close")
            return {"closed": 1, "list_ready": True, "handled": True}

    orchestrator = AftersaleShippedOrchestrator()
    controller = RuntimeController(orchestrator)
    result = controller._process_aftersale_item(
        {"task_id": 9, "workflow_key": "aftersale_shipped_refund"},
        False,
    )

    assert result["status"] == "READY_FOR_PRODUCTION"
    assert orchestrator.calls == ["aftersale_detail", "wdt", "route", "close"]
    assert [step["step"] for step in result["steps"]][-2:] == [
        "INTERCEPT",
        "AFTERSALE_ACTION",
    ]
    assert result["steps"][-2]["sent"] is False


def test_aftersale_shipped_refund_production_sends_before_agree_and_register():
    class AftersaleShippedProductionOrchestrator(FakeOrchestrator):
        def collect_aftersale_detail(self, task_id, *, keep_detail_open=True):
            self.calls.append("aftersale_detail")
            return {
                "decision": {"action": AGREE_REFUND, "reason": "三条以内"},
                "facts": {
                    "refund_type": "退款",
                    "logistics_trajectory_count": 3,
                },
                "detail_cleanup": {},
            }

        def check_wdt_by_components(self, task_id):
            self.calls.append("wdt")
            return {
                "order_status": "已发货",
                "warehouse": "示例供应链代发总仓",
                "logistics_company": "韵达快递",
                "requires_interception": True,
            }

        def prepare_in_transit_routing(self, task_id):
            self.calls.append("route")
            return {
                "ok": True,
                "route_type": "carrier_group",
                "group": {"platform": "企业微信", "group_name": "测试群"},
            }

        def dispatch_in_transit_interception(self, task_id, routing):
            self.calls.append("dispatch")
            return {"ok": True, "send": {"sent_echo": True}}

        def validate_aftersale_shipped_refund_safety(self, task_id, *, live):
            self.calls.append("safety")
            assert live is True
            return {"refund_amount": 18.8}

        def complete_aftersale_shipped_refund_after_interception(
            self, task_id, *, live=False
        ):
            self.calls.append("agree_and_register")
            assert live is True
            assert self.calls.index("dispatch") < self.calls.index("agree_and_register")
            return {
                "ok": True,
                "business_write_performed": True,
                "interception_register": {"id": 1},
                "detail_cleanup": {"closed": 1, "list_ready": True},
            }

    orchestrator = AftersaleShippedProductionOrchestrator()
    controller = RuntimeController(orchestrator)
    result = controller._process_aftersale_item(
        {"task_id": 10, "workflow_key": "aftersale_shipped_refund"},
        True,
    )

    assert result["status"] == "COMPLETED"
    assert orchestrator.calls == [
        "aftersale_detail",
        "wdt",
        "route",
        "safety",
        "dispatch",
        "agree_and_register",
    ]
    assert orchestrator.repository.task_statuses[10][0] == TaskState.COMPLETED


def test_aftersale_jushuitan_redecides_two_traces_after_wdt():
    class WarehouseAwareOrchestrator(FakeOrchestrator):
        def collect_aftersale_detail(self, task_id, *, keep_detail_open=True):
            self.calls.append("aftersale_detail")
            return {
                "decision": {"action": AGREE_REFUND, "reason": "WDT前暂按三条"},
                "facts": {
                    "refund_type": "退款",
                    "logistics_trajectory_count": 2,
                },
                "detail_cleanup": {},
            }

        def check_wdt_by_components(self, task_id):
            self.calls.append("wdt")
            return {
                "order_status": "已发货",
                "warehouse": "示例供应链代发总仓",
                "logistics_company": "韵达快递",
                "requires_interception": True,
                "max_agree_refund_trace_count": 1,
                "aftersale_decision": {
                    "action": REJECT_REFUND,
                    "reason": "聚水潭仓两条轨迹已超过直接退款阈值",
                },
            }

        def prepare_in_transit_routing(self, task_id):
            self.calls.append("route")
            return {
                "ok": True,
                "route_type": "carrier_group",
                "group": {"platform": "企业微信", "group_name": "测试群"},
            }

        def close_aftersale_detail(self, task_id):
            self.calls.append("close")
            return {"closed": 1, "list_ready": True, "handled": True}

    orchestrator = WarehouseAwareOrchestrator()
    result = RuntimeController(orchestrator)._process_aftersale_item(
        {"task_id": 11, "workflow_key": "aftersale_shipped_refund"},
        False,
    )

    assert result["status"] == "READY_FOR_PRODUCTION"
    threshold_step = next(
        step
        for step in result["steps"]
        if step["step"] == "AFTERSALE_WAREHOUSE_THRESHOLD"
    )
    assert threshold_step["previous_action"] == AGREE_REFUND
    assert threshold_step["action"] == REJECT_REFUND
    assert threshold_step["max_agree_refund_trace_count"] == 1
    assert result["steps"][-1]["action"] == REJECT_REFUND


def test_retry_pending_aftersale_queue_only_opens_after_valid_not_before_gate():
    orchestrator = FakeOrchestrator()
    now = datetime.now()

    def retry_task(task_id, order_no, retry_not_before_marker):
        retry = (
            {}
            if retry_not_before_marker is None
            else {"retry_not_before": retry_not_before_marker}
        )
        return {
            "id": task_id,
            "state": TaskState.RETRY_PENDING,
            "queue_priority": 10,
            "facts": {
                "work_order_title": AFTERSALE_SHIPPED_REFUND,
                "order_no": order_no,
                "source_evidence": {
                    "source_system": "aftersale_workbench",
                    "automation_eligible": True,
                    "aftersale_retry": retry,
                },
            },
        }

    tasks = [
        retry_task(
            11,
            "FUTURE",
            (now + timedelta(minutes=30)).isoformat(timespec="seconds"),
        ),
        retry_task(
            12,
            "DUE",
            (now - timedelta(seconds=1)).isoformat(timespec="seconds"),
        ),
        retry_task(13, "MISSING", None),
        retry_task(14, "INVALID", "not-an-iso-datetime"),
    ]

    queue = RuntimeController(orchestrator)._build_queue(
        {
            "results": [{"ok": True, "task_ids": [11, 12, 13, 14]}],
            "_tasks": tasks,
        },
        ["aftersale_shipped_refund"],
    )

    # Future, missing and malformed retry gates all fail closed.  Only the
    # persisted gate that has actually expired is allowed back into the queue.
    assert [item["order_no"] for item in queue] == ["DUE"]


def test_reject_cooldown_is_persisted_as_retry_pending_without_completion():
    class RetryRepository(FakeRepository):
        def __init__(self):
            super().__init__()
            self.status_history = []
            self.defer_calls = []

        def mark_task_status(self, task_id, target, reason):
            self.status_history.append((int(task_id), target, reason))
            super().mark_task_status(task_id, target, reason)

        def defer_task_retry(
            self, task_id, retry_not_before, reason_code, prompt_text
        ):
            self.defer_calls.append(
                (task_id, retry_not_before, reason_code, prompt_text)
            )
            self.task_statuses[int(task_id)] = (
                TaskState.RETRY_PENDING,
                prompt_text,
            )
            return {
                "task_id": task_id,
                "state": TaskState.RETRY_PENDING,
                "retry_not_before": retry_not_before,
                "reason_code": reason_code,
            }

    class CooldownOrchestrator(FakeOrchestrator):
        def __init__(self):
            super().__init__()
            self.repository = RetryRepository()

        def collect_aftersale_detail(self, task_id, *, keep_detail_open=True):
            self.calls.append("aftersale_detail")
            return {
                "decision": {"action": REJECT_REFUND, "reason": "物流轨迹超过三条"},
                "facts": {"refund_type": "退款", "logistics_trajectory_count": 9},
            }

        def check_wdt_by_components(self, task_id):
            self.calls.append("wdt")
            return {
                "order_status": "已发货",
                "warehouse": "示例供应链代发总仓",
                "logistics_company": "韵达快递",
                "requires_interception": True,
            }

        def prepare_in_transit_routing(self, task_id):
            self.calls.append("route")
            return {
                "ok": True,
                "route_type": "carrier_group",
                "group": {"platform": "企业微信", "group_name": "测试群"},
            }

        def validate_aftersale_shipped_refund_safety(
            self, task_id, *, live, action
        ):
            self.calls.append("safety")
            assert live is True
            assert action == REJECT_REFUND
            return {"action": action, "refund_amount": 18.8}

        def dispatch_in_transit_interception(self, task_id, routing):
            self.calls.append("dispatch")
            return {"ok": True, "send": {"sent_echo": True}}

        def complete_aftersale_shipped_refund_after_interception(
            self, task_id, *, action, live=False
        ):
            self.calls.append("reject")
            assert action == REJECT_REFUND
            assert live is True
            return {
                "ok": True,
                "completed": False,
                "deferred": True,
                "retryable": True,
                "reason_code": "PDD_REJECT_COOLDOWN",
                "retry_after_seconds": 1800,
                "prompt_text": (
                    "请耐心等待消费者回复，并协商解决方案；待倒计时结束，"
                    "或消费者回复后，您可继续处理驳回"
                ),
                "business_write_performed": False,
                "submit_attempted": False,
                "detail_cleanup": {"closed": 1, "list_ready": True},
            }

    orchestrator = CooldownOrchestrator()
    started_at = datetime.now()

    result = RuntimeController(orchestrator)._process_aftersale_item(
        {"task_id": 21, "workflow_key": "aftersale_shipped_refund"},
        production=True,
    )

    assert result["status"] == "RETRY_PENDING_REJECT_COOLDOWN"
    assert len(orchestrator.repository.defer_calls) == 1
    task_id, retry_not_before, reason_code, prompt_text = (
        orchestrator.repository.defer_calls[0]
    )
    assert task_id == 21
    assert reason_code == "PDD_REJECT_COOLDOWN"
    assert "可继续处理驳回" in prompt_text
    due_at = datetime.fromisoformat(retry_not_before)
    assert started_at + timedelta(minutes=29, seconds=55) <= due_at
    assert due_at <= datetime.now() + timedelta(minutes=30, seconds=5)
    assert orchestrator.repository.task_statuses[21][0] is TaskState.RETRY_PENDING
    assert all(
        state is not TaskState.COMPLETED
        for _, state, _ in orchestrator.repository.status_history
    )
    assert orchestrator.calls == [
        "aftersale_detail",
        "wdt",
        "route",
        "safety",
        "dispatch",
        "reject",
    ]


def test_verified_negotiation_submit_cooldown_is_retry_pending():
    class StageRepository(FakeRepository):
        def __init__(self):
            super().__init__()
            self.defer_context = None

        def get_task(self, task_id):
            return {
                "id": task_id,
                "facts": {
                    "shop_id": "shop_02",
                    "order_no": "ORDER-STAGE",
                    "aftersale_no": "AS-STAGE",
                },
            }

        def defer_task_retry(
            self,
            task_id,
            retry_not_before,
            reason_code,
            prompt_text,
            retry_context,
        ):
            self.defer_context = dict(retry_context)
            return {
                "task_id": task_id,
                "retry_not_before": retry_not_before,
                "reason_code": reason_code,
                **retry_context,
            }

    class StageOrchestrator(FakeOrchestrator):
        def __init__(self):
            super().__init__()
            self.repository = StageRepository()

    orchestrator = StageOrchestrator()
    controller = RuntimeController(orchestrator)
    steps = [{"step": "AFTERSALE_ACTION", "status": "started"}]
    action_result = {
        "ok": True,
        "completed": False,
        "deferred": True,
        "retryable": True,
        "reason_code": "PDD_REJECT_NEGOTIATION_COOLDOWN",
        "retry_after_seconds": 1800,
        "prompt_text": (
            "请耐心等待消费者回复，并协商解决方案；"
            "待倒计时结束，或消费者回复后，您可继续处理驳回"
        ),
        "defer_kind": "POST_SUBMIT_VERIFIED",
        "stage_committed": True,
        "negotiation_round_submitted": True,
        "write_outcome_verified": True,
        "write_outcome_unknown": False,
        "business_write_performed": True,
        "submit_attempted": True,
        "resume_action": REJECT_REFUND,
        "reject_surface": "KEEP_REJECT",
    }

    result = controller._handle_aftersale_reject_deferred(
        31,
        REJECT_REFUND,
        action_result,
        steps,
        {"closed": 1, "list_ready": True},
    )

    assert result is not None
    assert result["status"] == "RETRY_PENDING_REJECT_COOLDOWN"
    assert orchestrator.repository.defer_context is not None
    assert orchestrator.repository.defer_context["business_write_performed"] is True
    assert orchestrator.repository.defer_context["order_no"] == "ORDER-STAGE"
    assert steps[-1]["submit_attempted"] is True


@pytest.mark.parametrize(
    "unsafe_flag",
    ["submit_attempted", "business_write_performed"],
)
def test_reject_cooldown_after_any_possible_write_is_held_for_review(unsafe_flag):
    class UnsafeCooldownRepository(FakeRepository):
        def __init__(self):
            super().__init__()
            self.defer_calls = []

        def defer_task_retry(self, *args):
            self.defer_calls.append(args)

    class UnsafeCooldownOrchestrator(FakeOrchestrator):
        def __init__(self):
            super().__init__()
            self.repository = UnsafeCooldownRepository()

        def collect_aftersale_detail(self, task_id, *, keep_detail_open=True):
            return {
                "decision": {"action": REJECT_REFUND, "reason": "物流轨迹超过三条"},
                "facts": {"refund_type": "退款", "logistics_trajectory_count": 9},
            }

        def check_wdt_by_components(self, task_id):
            return {
                "order_status": "已发货",
                "warehouse": "示例供应链代发总仓",
                "logistics_company": "韵达快递",
                "requires_interception": True,
            }

        def prepare_in_transit_routing(self, task_id):
            return {
                "ok": True,
                "route_type": "carrier_group",
                "group": {"platform": "企业微信", "group_name": "测试群"},
            }

        def validate_aftersale_shipped_refund_safety(
            self, task_id, *, live, action
        ):
            assert live is True
            assert action == REJECT_REFUND
            return {"action": action, "refund_amount": 18.8}

        def dispatch_in_transit_interception(self, task_id, routing):
            return {"ok": True, "send": {"sent_echo": True}}

        def complete_aftersale_shipped_refund_after_interception(
            self, task_id, *, action, live=False
        ):
            action_result = {
                "ok": True,
                "completed": False,
                "deferred": True,
                "retryable": True,
                "reason_code": "PDD_REJECT_COOLDOWN",
                "retry_after_seconds": 1800,
                "prompt_text": (
                    "请耐心等待消费者回复，并协商解决方案；待倒计时结束，"
                    "或消费者回复后，您可继续处理驳回"
                ),
                "business_write_performed": False,
                "submit_attempted": False,
            }
            action_result[unsafe_flag] = True
            return {
                **action_result,
                "detail_cleanup": {"closed": 1, "list_ready": True},
            }

    orchestrator = UnsafeCooldownOrchestrator()
    result = RuntimeController(orchestrator)._process_aftersale_item(
        {"task_id": 22, "workflow_key": "aftersale_shipped_refund"},
        production=True,
    )

    assert result["status"] == "NEED_REVIEW_UNSAFE_RETRY_STATE"
    assert orchestrator.repository.task_statuses[22][0] is TaskState.NEED_REVIEW
    assert orchestrator.repository.defer_calls == []


def test_agree_return_after_refund_runs_interception_chain_without_online_register():
    class ReturnAfterRefundOrchestrator(FakeOrchestrator):
        def __init__(self):
            super().__init__()
            self.safety_actions = []
            self.completion_actions = []

        def collect_aftersale_detail(self, task_id, *, keep_detail_open=True):
            self.calls.append("aftersale_detail")
            return {
                "decision": {
                    "action": AGREE_RETURN_AFTER_REFUND,
                    "reason": "超过三条且页面提供同意快递退回后退款",
                },
                "facts": {"refund_type": "退款", "logistics_trajectory_count": 9},
            }

        def check_wdt_by_components(self, task_id):
            self.calls.append("wdt")
            return {
                "order_status": "已发货",
                "warehouse": "示例供应链代发总仓",
                "logistics_company": "韵达快递",
                "requires_interception": True,
            }

        def prepare_in_transit_routing(self, task_id):
            self.calls.append("route")
            return {
                "ok": True,
                "route_type": "carrier_group",
                "group": {"platform": "企业微信", "group_name": "测试群"},
            }

        def dispatch_in_transit_interception(self, task_id, routing):
            self.calls.append("dispatch")
            return {"ok": True, "send": {"sent_echo": True}}

        def validate_aftersale_shipped_refund_safety(
            self, task_id, *, live, action
        ):
            self.calls.append("safety")
            self.safety_actions.append(action)
            assert live is True
            return {"refund_amount": 18.8}

        def complete_aftersale_shipped_refund_after_interception(
            self, task_id, *, live=False, action
        ):
            self.calls.append("return_without_register")
            self.completion_actions.append(action)
            assert live is True
            assert self.calls.index("dispatch") < self.calls.index(
                "return_without_register"
            )
            return {
                "ok": True,
                "business_write_performed": True,
                "result": {"ok": True, "business_write_performed": True},
                "detail_cleanup": {"closed": 1, "list_ready": True},
            }

    orchestrator = ReturnAfterRefundOrchestrator()
    result = RuntimeController(orchestrator)._process_aftersale_item(
        {"task_id": 23, "workflow_key": "aftersale_shipped_refund"},
        production=True,
    )

    assert result["status"] == "COMPLETED"
    assert orchestrator.calls == [
        "aftersale_detail",
        "wdt",
        "route",
        "safety",
        "dispatch",
        "return_without_register",
    ]
    assert orchestrator.safety_actions == [AGREE_RETURN_AFTER_REFUND]
    assert orchestrator.completion_actions == [AGREE_RETURN_AFTER_REFUND]
    assert orchestrator.repository.task_statuses[23][0] is TaskState.COMPLETED


def test_dingtalk_registration_pending_never_marks_aftersale_completed():
    class RegistrationPendingOrchestrator(FakeOrchestrator):
        def __init__(self):
            super().__init__()
            self.pdd_action_calls = 0

        def collect_aftersale_detail(self, task_id, *, keep_detail_open=True):
            self.calls.append("aftersale_detail")
            return {
                "decision": {
                    "action": AGREE_REFUND,
                    "reason": "三条以内",
                },
                "facts": {"refund_type": "退款", "logistics_trajectory_count": 3},
            }

        def check_wdt_by_components(self, task_id):
            self.calls.append("wdt")
            return {
                "order_status": "已发货",
                "warehouse": "示例供应链代发总仓",
                "logistics_company": "韵达快递",
                "requires_interception": True,
            }

        def prepare_in_transit_routing(self, task_id):
            self.calls.append("route")
            return {
                "ok": True,
                "route_type": "carrier_group",
                "group": {"platform": "企业微信", "group_name": "测试群"},
            }

        def dispatch_in_transit_interception(self, task_id, routing):
            self.calls.append("dispatch")
            return {
                "ok": True,
                "send": {"sent_echo": "YT-PENDING+拦截", "evidence": ["sent.png"]},
            }

        def validate_aftersale_shipped_refund_safety(self, task_id, *, live):
            self.calls.append("safety")
            return {"refund_amount": 18.8, "action": AGREE_REFUND}

        def complete_aftersale_shipped_refund_after_interception(
            self, task_id, *, live=False
        ):
            self.calls.append("register_pending")
            # Mirrors the real wrapper's fail-closed result before it calls the
            # PDD action worker.
            return {
                "ok": False,
                "needs_review": True,
                "registration_pending": True,
                "reason_code": "DINGTALK_INTERCEPTION_REGISTER_PENDING",
                "error": "在线表格窗口未就绪",
                "business_write_performed": False,
                "submit_attempted": False,
                "interception_register": {"id": 7},
                "interception_register_dingtalk": {"ok": False},
            }

    orchestrator = RegistrationPendingOrchestrator()
    result = RuntimeController(orchestrator)._process_aftersale_item(
        {"task_id": 25, "workflow_key": "aftersale_shipped_refund"},
        production=True,
    )

    assert result["status"] == "NEED_REVIEW_ACTION_OUTCOME"
    assert orchestrator.repository.task_statuses[25][0] is TaskState.NEED_REVIEW
    assert orchestrator.pdd_action_calls == 0
    assert "register_pending" in orchestrator.calls
    assert result["steps"][-1]["status"] == "needs_review"


def test_reject_refund_runs_wdt_routing_and_interception_before_reject():
    class RejectOrchestrator(FakeOrchestrator):
        def collect_aftersale_detail(self, task_id, *, keep_detail_open=True):
            self.calls.append("aftersale_detail")
            return {
                "decision": {"action": REJECT_REFUND, "reason": "物流轨迹超过三条"},
                "facts": {"refund_type": "退款", "logistics_trajectory_count": 12},
            }

        def check_wdt_by_components(self, task_id):
            self.calls.append("wdt")
            return {
                "order_status": "已发货",
                "warehouse": "示例供应链代发总仓",
                "logistics_company": "韵达快递",
                "requires_interception": True,
            }

        def prepare_in_transit_routing(self, task_id):
            self.calls.append("route")
            return {
                "ok": True,
                "route_type": "carrier_group",
                "group": {"platform": "企业微信", "group_name": "测试群"},
            }

        def dispatch_in_transit_interception(self, task_id, routing):
            self.calls.append("dispatch")
            return {
                "ok": True,
                "send": {"sent_echo": "YT-REJECT+拦截", "evidence": ["sent.png"]},
            }

        def validate_aftersale_shipped_refund_safety(
            self, task_id, *, live, action
        ):
            self.calls.append("safety")
            assert action == REJECT_REFUND
            assert live is True
            return {"action": action, "refund_amount": 18.8}

        def complete_aftersale_shipped_refund_after_interception(
            self, task_id, *, action, live=False
        ):
            self.calls.append("reject")
            assert action == REJECT_REFUND
            assert live is True
            return {
                "ok": True,
                "completed": True,
                "business_write_performed": True,
                "detail_cleanup": {"closed": 1, "list_ready": True},
            }

    orchestrator = RejectOrchestrator()
    result = RuntimeController(orchestrator)._process_aftersale_item(
        {"task_id": 24, "workflow_key": "aftersale_shipped_refund"},
        production=True,
    )

    assert result["status"] == "COMPLETED"
    assert orchestrator.calls == [
        "aftersale_detail",
        "wdt",
        "route",
        "safety",
        "dispatch",
        "reject",
    ]
    assert orchestrator.repository.task_statuses[24][0] is TaskState.COMPLETED
    assert "无需在线登记" in orchestrator.repository.task_statuses[24][1]


def test_reject_outcome_unknown_after_interception_never_completes_or_retries():
    class OutcomeRepository(FakeRepository):
        def __init__(self):
            super().__init__()
            self.status_history = []
            self.defer_calls = []

        def mark_task_status(self, task_id, target, reason):
            self.status_history.append((int(task_id), target, reason))
            super().mark_task_status(task_id, target, reason)

        def defer_task_retry(self, *args):
            self.defer_calls.append(args)

    class UnknownRejectOrchestrator(FakeOrchestrator):
        def __init__(self):
            super().__init__()
            self.repository = OutcomeRepository()

        def collect_aftersale_detail(self, task_id, *, keep_detail_open=True):
            self.calls.append("aftersale_detail")
            return {
                "decision": {"action": REJECT_REFUND, "reason": "物流轨迹超过三条"},
                "facts": {"refund_type": "退款", "logistics_trajectory_count": 12},
            }

        def check_wdt_by_components(self, task_id):
            self.calls.append("wdt")
            return {
                "order_status": "已发货",
                "warehouse": "示例供应链代发总仓",
                "logistics_company": "韵达快递",
                "requires_interception": True,
            }

        def prepare_in_transit_routing(self, task_id):
            self.calls.append("route")
            return {
                "ok": True,
                "route_type": "carrier_group",
                "group": {"platform": "企业微信", "group_name": "测试群"},
            }

        def validate_aftersale_shipped_refund_safety(
            self, task_id, *, live, action
        ):
            self.calls.append("safety")
            assert action == REJECT_REFUND
            return {"action": action, "refund_amount": 18.8}

        def dispatch_in_transit_interception(self, task_id, routing):
            self.calls.append("dispatch")
            return {
                "ok": True,
                "send": {"sent_echo": "YT-UNKNOWN+拦截", "evidence": ["sent.png"]},
            }

        def complete_aftersale_shipped_refund_after_interception(
            self, task_id, *, action, live=False
        ):
            self.calls.append("reject")
            return {
                "ok": False,
                "write_outcome_unknown": True,
                "needs_review": True,
                "business_write_performed": True,
                "submit_attempted": True,
                "reason_code": "PDD_REJECT_OUTCOME_UNKNOWN",
                "error": "驳回提交后的页面终态无法确认",
                "detail_cleanup": {"closed": 1, "list_ready": True},
            }

    orchestrator = UnknownRejectOrchestrator()
    result = RuntimeController(orchestrator)._process_aftersale_item(
        {"task_id": 26, "workflow_key": "aftersale_shipped_refund"},
        production=True,
    )

    assert result["status"] == "NEED_REVIEW_ACTION_OUTCOME"
    assert orchestrator.calls == [
        "aftersale_detail",
        "wdt",
        "route",
        "safety",
        "dispatch",
        "reject",
    ]
    assert orchestrator.repository.task_statuses[26][0] is TaskState.NEED_REVIEW
    assert orchestrator.repository.defer_calls == []
    assert all(
        state is not TaskState.COMPLETED
        for _, state, _ in orchestrator.repository.status_history
    )


def test_current_queue_discovery_excludes_stale_preserved_aftersales():
    orchestrator = FakeOrchestrator()
    orchestrator.repository.settings["live_sync_snapshot"] = {
        "run_id": "sync-1",
        "in_progress": False,
    }
    orchestrator.list_prioritized_tasks = lambda view="all": [
        {
            "id": 1,
            "source_stale": True,
            "facts": {"work_order_title": AFTERSALE_RETURN_REFUND},
        },
        {
            "id": 2,
            "facts": {
                "work_order_title": AFTERSALE_RETURN_REFUND,
                "source_evidence": {"source_stale": "true"},
            },
        },
        {
            "id": 3,
            "facts": {"work_order_title": AFTERSALE_RETURN_REFUND},
        },
    ]
    controller = RuntimeController(orchestrator)

    discovery = controller._current_queue_discovery()

    assert discovery["results"][0]["task_ids"] == [3]
    assert [task["id"] for task in discovery["_tasks"]] == [3]


def test_partial_aftersale_scan_is_reported_while_work_order_queue_can_continue():
    class PartialAftersaleOrchestrator(FakeOrchestrator):
        def discover_live_readonly(self):
            return {
                "results": [
                    {
                        "shop_id": "shop-1",
                        "shop_name": "测试店铺",
                        "ok": True,
                        "task_ids": [],
                        "aftersale_error": "待处理即将逾期采集不完整（识别8/页面总数12）",
                    }
                ],
                "sync": {"run_id": "sync-1"},
                "_tasks": [],
            }

    controller = RuntimeController(PartialAftersaleOrchestrator())
    controller.start("dry_run", ["shipped_no_trace"], continuous=False)
    for _ in range(100):
        status = controller.status()
        if status["state"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.01)

    assert status["state"] == "COMPLETED"
    assert "售后待处理扫描失败、已保留上轮数据" in status["phase"]
    assert "售后待处理扫描失败、已保留上轮数据" in status["last_error"]
    assert "识别8/页面总数12" in status["last_error"]


def test_aftersale_without_extend_receipt_is_sent_to_manual_review():
    orchestrator = FakeOrchestrator()
    orchestrator.collect_aftersale_detail = lambda task_id, keep_detail_open=True: {
        "decision": {"action": NEED_REVIEW, "reason": "退货退款详情没有延长收货操作"},
        "facts": {"refund_type": "退货退款", "has_extend_receipt": False},
    }
    orchestrator.close_aftersale_detail = lambda task_id: {
        "closed": 1,
        "list_ready": True,
        "handled": True,
    }

    result = RuntimeController(orchestrator)._process_aftersale_item(
        {"task_id": 8, "order_no": "R-8", "shop_id": "shop-1"},
        production=True,
    )

    assert result["status"] == "NEED_REVIEW_NO_EXTEND_RECEIPT"
    assert orchestrator.repository.task_statuses[8][0] is TaskState.NEED_REVIEW


def test_aftersale_without_extend_receipt_keeps_inline_detail_cleanup():
    orchestrator = FakeOrchestrator()
    close_calls = []
    orchestrator.collect_aftersale_detail = lambda task_id, keep_detail_open=True: {
        "decision": {"action": NEED_REVIEW, "reason": "售后详情没有延长收货操作"},
        "facts": {"refund_type": "退货退款", "has_extend_receipt": False},
        "detail_cleanup": {"closed": 1, "list_ready": True, "handled": True},
        "detail_retained": False,
    }
    orchestrator.close_aftersale_detail = lambda task_id: close_calls.append(task_id)

    result = RuntimeController(orchestrator)._process_aftersale_item(
        {"task_id": 9, "order_no": "R-9", "shop_id": "shop-1"},
        production=True,
    )

    assert result["status"] == "NEED_REVIEW_NO_EXTEND_RECEIPT"
    assert close_calls == []
    assert orchestrator.repository.task_statuses[9][0] is TaskState.NEED_REVIEW


def test_shipped_no_trace_non_eligible_status_is_skipped():
    controller = RuntimeController(IneligibleOrchestrator())
    controller.start("dry_run", ["shipped_no_trace"])
    for _ in range(100):
        status = controller.status()
        if status["state"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.01)
    assert status["state"] == "COMPLETED"
    assert status["counts"] == {"total": 1, "completed": 0, "failed": 0, "skipped": 1}
    assert status["results"][0]["status"] == "SKIPPED_NOT_ELIGIBLE"
    assert status["results"][0]["error"] == "旺店通订单状态不符合退款条件：已完成"
    assert status["results"][0]["steps"][-1] == {
        "step": "WDT_CHECK",
        "status": "skipped",
        "reason": "旺店通订单状态不符合退款条件：已完成",
    }
    assert controller.repository.task_statuses[1][0] == TaskState.COMPLETED


def test_missing_order_is_skipped_and_does_not_retry_or_fail():
    controller = RuntimeController(NotFoundOrchestrator())
    controller.start("dry_run", ["shipped_no_trace"])
    for _ in range(100):
        status = controller.status()
        if status["state"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.01)
    assert status["state"] == "COMPLETED"
    assert status["counts"] == {"total": 1, "completed": 0, "failed": 0, "skipped": 1}
    assert status["results"][0]["status"] == "SKIPPED_NOT_FOUND"


def test_manual_task_state_changed_during_loop_skips_queued_item():
    orchestrator = FakeOrchestrator()
    orchestrator.repository.get_task = lambda task_id: {
        "id": task_id,
        "state": TaskState.COMPLETED,
    }
    controller = RuntimeController(orchestrator)
    controller.start("dry_run", ["shipped_no_trace"])
    for _ in range(100):
        status = controller.status()
        if status["state"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.01)
    assert status["state"] == "COMPLETED"
    assert status["counts"] == {"total": 1, "completed": 0, "failed": 0, "skipped": 1}
    assert status["results"][0]["status"] == "SKIPPED_MANUAL_STATUS"
    assert status["results"][0]["steps"] == [
        {"step": "MANUAL_STATUS_CHECK", "status": "skipped", "state": "COMPLETED"}
    ]
    assert orchestrator.calls == ["discover"]


def test_in_transit_ineffective_outcome_submit_skips_without_retrying_interception():
    orchestrator = FakeOrchestrator()
    status_updates = []
    orchestrator.repository.mark_task_status = lambda task_id, state, reason: status_updates.append(
        (task_id, state, reason)
    )
    orchestrator.prepare_in_transit_routing = lambda task_id: {
        "ok": True,
        "route_type": "DIRECT_CARRIER",
        "group": {"group_name": "测试群"},
        "message": "TRACK-1+拦截",
    }
    orchestrator.dispatch_in_transit_interception = lambda task_id, routing: {"send": {"sent": True}}

    controller = RuntimeController(orchestrator)
    controller._finish_in_transit_pdd_after_interception = lambda task_id, steps: {
        "submission_ineffective": True,
        "skip_reason": "已协商一致并提交后表单未变化",
        "detail_cleanup": {"closed": 1, "list_ready": True},
    }

    result = controller._process_in_transit_item(
        {
            "task_id": 1,
            "shop_id": "shop_01",
            "shop_name": "测试店",
            "order_no": "ORDER-1",
            "workflow_key": "in_transit_no_reason",
        },
        production=True,
    )

    assert result["status"] == "SKIPPED_SUBMISSION_INEFFECTIVE"
    assert status_updates[-1][1] is TaskState.NEED_REVIEW
    assert "不会自动重试" in status_updates[-1][2]


def test_in_transit_non_junfu_warehouse_skips_before_interception_dispatch():
    orchestrator = FakeOrchestrator()
    status_updates = []
    dispatches = []
    orchestrator.repository.mark_task_status = lambda task_id, state, reason: status_updates.append(
        (task_id, state, reason)
    )
    orchestrator.prepare_in_transit_routing = lambda task_id: {
        "ok": False,
        "manual_review": True,
        "reason_code": "UNSUPPORTED_WDT_WAREHOUSE",
        "reason": "旺店通仓库“其他仓”不在在途自动化白名单：示例自营仓",
        "wdt_warehouse": "其他仓",
    }
    orchestrator.dispatch_in_transit_interception = lambda *args: dispatches.append(args)

    result = RuntimeController(orchestrator)._process_in_transit_item(
        {
            "task_id": 1,
            "shop_id": "shop_01",
            "shop_name": "测试店",
            "order_no": "ORDER-1",
            "workflow_key": "in_transit_no_reason",
        },
        production=True,
    )

    assert result["status"] == "SKIPPED_UNSUPPORTED_WAREHOUSE"
    assert result["steps"][-1]["step"] == "WDT_WAREHOUSE_GATE"
    assert status_updates[-1][1] is TaskState.NEED_REVIEW
    assert dispatches == []


def test_in_transit_unmapped_jst_product_goes_to_manual_review_not_failed():
    orchestrator = FakeOrchestrator()
    status_updates = []
    dispatches = []
    orchestrator.repository.mark_task_status = lambda task_id, state, reason: status_updates.append(
        (task_id, state, reason)
    )
    orchestrator.prepare_in_transit_routing = lambda task_id: {
        "ok": False,
        "manual_review": True,
        "reason_code": "WDT_PRODUCT_NAME_UNMAPPED",
        "reason": "示例供应链代发总仓货品名称未配置拦截群",
        "wdt_warehouse": "示例供应链代发总仓",
    }
    orchestrator.dispatch_in_transit_interception = lambda *args: dispatches.append(args)

    result = RuntimeController(orchestrator)._process_in_transit_item(
        {
            "task_id": 1,
            "shop_id": "shop_01",
            "shop_name": "测试店",
            "order_no": "ORDER-1",
            "workflow_key": "in_transit_no_reason",
        },
        production=True,
    )

    assert result["status"] == "NEED_REVIEW_ROUTE_UNMAPPED"
    assert result["steps"][-1]["step"] == "ROUTE_MANUAL_REVIEW"
    assert result["steps"][-1]["reason_code"] == "WDT_PRODUCT_NAME_UNMAPPED"
    assert status_updates[-1][1] is TaskState.NEED_REVIEW
    assert dispatches == []


def test_in_transit_dispatch_error_is_held_for_review_without_automatic_resend():
    class DispatchFailureOrchestrator(FakeOrchestrator):
        def __init__(self):
            super().__init__()
            self.config.live_actions_enabled = True
            self.config.execution_mode = "auto"
            self.config.raw = {"safety": {"global_failure_circuit_breaker": 3}}
            self.repository.settings["refund_uia_regression"] = {"confirmed": True}
            self.status_updates = []
            self.dispatch_calls = 0
            self.repository.mark_task_status = lambda task_id, state, reason: self.status_updates.append(
                (task_id, state, reason)
            )

        def in_transit_dispatch_readiness(self):
            return {"ready": True, "reasons": []}

        def list_prioritized_tasks(self):
            return [
                {
                    "id": 1,
                    "queue_priority": 50,
                    "facts": {
                        "work_order_title": "在途无理由退款处理",
                        "shop_name": "测试店",
                        "order_no": "ORDER-1",
                        "source_evidence": {"shop_scan_order": 0, "page_no": 1, "page_index": 0},
                    },
                }
            ]

        def prepare_in_transit_routing(self, _task_id):
            return {
                "ok": True,
                "route_type": "DIRECT_CARRIER",
                "group": {"group_name": "测试群"},
                "message": "TRACK-1+拦截",
            }

        def dispatch_in_transit_interception(self, _task_id, _routing):
            self.dispatch_calls += 1
            raise RuntimeError("发送按钮点击后窗口失焦")

    orchestrator = DispatchFailureOrchestrator()
    controller = RuntimeController(orchestrator)
    controller.start("production", ["in_transit_no_reason"], continuous=False)
    for _ in range(100):
        status = controller.status()
        if status["state"] in {"COMPLETED", "FAILED"}:
            break
        time.sleep(0.01)

    assert status["state"] == "COMPLETED"
    assert status["counts"] == {"total": 1, "completed": 0, "failed": 0, "skipped": 1}
    assert status["results"][0]["status"] == "NEED_REVIEW_EXTERNAL_WRITE"
    assert orchestrator.dispatch_calls == 1
    assert orchestrator.status_updates[-1][1] is TaskState.NEED_REVIEW


def test_executing_task_is_not_requeued_after_process_restart():
    orchestrator = FakeOrchestrator()
    orchestrator.list_prioritized_tasks = lambda: [
        {
            "id": 1,
            "state": "EXECUTING",
            "queue_priority": 100,
            "facts": {
                "work_order_title": SHIPPED_NO_TRACE,
                "shop_name": "娴嬭瘯搴?",
                "order_no": "ORDER-1",
            },
        }
    ]
    controller = RuntimeController(orchestrator)
    queue = controller._build_queue(
        {"results": [{"ok": True, "task_ids": [1]}]}, ["shipped_no_trace"]
    )
    assert queue == []


def test_running_state_is_marked_for_safe_recovery_on_new_process():
    orchestrator = FakeOrchestrator()
    orchestrator.repository.settings["runtime_controller_status"] = {
        "state": "RUNNING",
        "run_id": "run-before-crash",
        "mode": "production",
        "continuous": True,
        "workflow_keys": ["shipped_no_trace"],
    }
    controller = RuntimeController(orchestrator)
    status = controller.status()
    assert status["state"] == "INTERRUPTED"
    assert status["recovery_pending"] is True
    assert status["recovery_previous_state"] == "RUNNING"
