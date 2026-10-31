import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from dataclasses import replace
from pathlib import Path

from pdd_rpa.components import validate_component
from pdd_rpa.aftersale_workbench import (
    AFTERSALE_SHIPPED_REFUND,
    AGREE_REFUND,
    AGREE_RETURN_AFTER_REFUND,
    REJECT_REFUND,
)
from pdd_rpa.config import load_config
from pdd_rpa.connectors.base import ConnectorResult
from pdd_rpa.db import Repository
from pdd_rpa.in_transit_routing import RoutingConfigConflict
from pdd_rpa.models import TaskState, WorkOrderFacts
from pdd_rpa.orchestrator import Orchestrator
from pdd_rpa.qianniu_aftersale import QIANNIU_UNRECEIVED_REFUND


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        config = load_config()
        config = replace(config, database_path=Path(self.temp_dir.name) / "test.db")
        self.repo = Repository(config.database_path)
        self.orchestrator = Orchestrator(config, self.repo)
        self.orchestrator.initialize()
        rules = json.loads((config.project_root / "config" / "rules.json").read_text(encoding="utf-8"))
        self.repo.seed_rules(rules)

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_live_task(self) -> int:
        return self.repo.upsert_task(
            WorkOrderFacts(
                shop_id="shop_01",
                shop_name="测试店铺",
                edge_profile="Profile",
                work_order_no="REAL-WO-001",
                order_no="REAL-ORDER-001",
                work_order_title="在途无理由退款处理",
                refund_type="仅退款",
                refund_reason="不想要了",
                pdd_order_status="已发货",
                carrier="中通",
                tracking_no="REAL-ZTO-001",
                source_evidence={"collection_mode": "edge_uia"},
            )
        )

    def test_qianniu_pre_shipment_waiting_pickup_refunds_without_group_or_registration(self):
        raw = dict(self.orchestrator.config.raw)
        raw["qianniu_shop_browser"] = {
            **dict(raw.get("qianniu_shop_browser") or {}),
            "workflow_enabled": True,
            "production_enabled": True,
        }
        self.orchestrator.config = replace(self.orchestrator.config, raw=raw)
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                shop_id="qianniu_01",
                shop_name="示例天猫旗舰店",
                edge_profile="Default",
                work_order_no="405119198641780713",
                order_no="5127711625084043334",
                work_order_title=QIANNIU_UNRECEIVED_REFUND,
                refund_amount=19.9,
                source_evidence={
                    "source_system": "qianniu_aftersale",
                    "aftersale_no": "405119198641780713",
                },
            )
        )
        calls = []
        self.orchestrator.collect_qianniu_task = lambda *_args, **_kwargs: {
            "ok": True,
            "detail": {
                "available_actions": ["同意退款", "拦截快递"],
                "logistics_text": "您的包裹正在等待揽收，请耐心等待",
            },
            "wdt": {"order_status": "已审核", "warehouse": "示例自营仓"},
        }
        self.orchestrator.prepare_qianniu_interception = lambda _task_id: self.fail(
            "未揽收退款不应准备群拦截路由"
        )
        self.orchestrator.dispatch_in_transit_interception = (
            lambda *_args, **_kwargs: self.fail("未揽收退款不应发送群拦截")
        )
        self.orchestrator._upsert_routed_interception_register = (
            lambda **_kwargs: self.fail("未揽收退款不应登记钉钉")
        )

        def worker(**kwargs):
            if kwargs["operation"] == "revalidate":
                return {"ok": True, "processable": True, "state": {}}
            self.assertEqual(kwargs["operation"], "execute_agree_refund")
            self.assertTrue(kwargs["live"])
            calls.append("agree_refund_confirm")
            return {"ok": True, "business_write_performed": True}

        self.orchestrator._run_qianniu_worker = worker
        self.orchestrator._close_qianniu_task_detail = (
            lambda *_args, **_kwargs: {"ok": True}
        )

        result = self.orchestrator.process_qianniu_task(task_id, live=True)

        self.assertEqual(calls, ["agree_refund_confirm"])
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(
            result["decision"]["reason_code"], "PRE_SHIPMENT_WAITING_PICKUP"
        )
        self.assertEqual(self.repo.get_task(task_id)["state"], TaskState.COMPLETED)

    def test_qianniu_missing_unique_tracking_is_retried_on_next_scan(self):
        raw = dict(self.orchestrator.config.raw)
        raw["qianniu_shop_browser"] = {
            **dict(raw.get("qianniu_shop_browser") or {}),
            "workflow_enabled": True,
            "production_enabled": True,
        }
        self.orchestrator.config = replace(self.orchestrator.config, raw=raw)
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                "qianniu_01",
                "示例天猫旗舰店",
                "Default",
                "405449822671293405",
                "5127709501019017748",
                work_order_title=QIANNIU_UNRECEIVED_REFUND,
                source_evidence={
                    "source_system": "qianniu_aftersale",
                    "aftersale_no": "405449822671293405",
                    "automation_eligible": True,
                },
            )
        )

        def missing_tracking(*_args, **_kwargs):
            raise RuntimeError("千牛详情未读取到唯一有效物流单号")

        self.orchestrator.collect_qianniu_task = missing_tracking
        self.orchestrator._close_qianniu_task_detail = (
            lambda *_args, **_kwargs: {"ok": True}
        )

        result = self.orchestrator.process_qianniu_task(task_id, live=True)

        self.assertEqual(result["status"], "RETRY_PENDING")
        self.assertEqual(self.repo.get_task(task_id)["state"], TaskState.FAILED)

    def test_qianniu_externally_handled_record_is_completed_without_wdt(self):
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                "qianniu_01",
                "示例天猫旗舰店",
                "Default",
                "405583488957934659",
                "5127395222042033935",
                work_order_title=QIANNIU_UNRECEIVED_REFUND,
                source_evidence={
                    "source_system": "qianniu_aftersale",
                    "aftersale_no": "405583488957934659",
                },
            )
        )
        self.orchestrator._run_qianniu_worker = lambda **_kwargs: {
            "ok": True,
            "externally_handled": True,
            "reason": "精确查询后已不再满足退款待处理且含拦截快递",
            "opened": {"record": {"aftersale_no": "405583488957934659"}},
        }

        result = self.orchestrator.collect_qianniu_task(task_id)

        self.assertTrue(result["externally_handled"])
        self.assertEqual(self.repo.get_task(task_id)["state"], TaskState.COMPLETED)

    def test_qianniu_external_handling_before_group_prevents_dispatch(self):
        raw = dict(self.orchestrator.config.raw)
        raw["qianniu_shop_browser"] = {
            **dict(raw.get("qianniu_shop_browser") or {}),
            "workflow_enabled": True,
            "production_enabled": True,
        }
        self.orchestrator.config = replace(self.orchestrator.config, raw=raw)
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                "qianniu_01",
                "示例天猫旗舰店",
                "Default",
                "405583488957934659",
                "5127395222042033935",
                work_order_title=QIANNIU_UNRECEIVED_REFUND,
                source_evidence={
                    "source_system": "qianniu_aftersale",
                    "aftersale_no": "405583488957934659",
                },
            )
        )
        self.orchestrator.collect_qianniu_task = lambda *_args, **_kwargs: {
            "ok": True,
            "detail": {"logistics_text": "已揽收"},
            "wdt": {"order_status": "已发货", "warehouse": "示例自营仓"},
        }
        self.orchestrator.prepare_qianniu_interception = lambda _task_id: {
            "ok": True,
            "group": {"group_name": "示例韵达沟通群"},
            "message": "40000000000000+拦截",
        }
        self.orchestrator.dispatch_in_transit_interception = (
            lambda *_args, **_kwargs: self.fail("人工已处理时禁止继续发群")
        )

        def worker(**kwargs):
            self.assertEqual(kwargs["operation"], "revalidate")
            return {
                "ok": True,
                "processable": False,
                "externally_handled": True,
                "reason": "拦截快递已由人工处理",
                "state": {"aftersale_status": "退款成功"},
            }

        self.orchestrator._run_qianniu_worker = worker
        self.orchestrator._close_qianniu_task_detail = (
            lambda *_args, **_kwargs: {"ok": True}
        )

        result = self.orchestrator.process_qianniu_task(task_id, live=True)

        self.assertEqual(result["status"], "SKIPPED_EXTERNALLY_HANDLED")
        self.assertEqual(result["stage"], "before_group_dispatch")
        self.assertEqual(self.repo.get_task(task_id)["state"], TaskState.COMPLETED)

    def test_qianniu_over_five_sends_group_then_intercepts_without_registration(self):
        raw = dict(self.orchestrator.config.raw)
        raw["qianniu_shop_browser"] = {
            **dict(raw.get("qianniu_shop_browser") or {}),
            "workflow_enabled": True,
            "production_enabled": True,
        }
        self.orchestrator.config = replace(self.orchestrator.config, raw=raw)
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                shop_id="qianniu_01",
                shop_name="示例天猫旗舰店",
                edge_profile="Default",
                work_order_no="405119198641780713",
                order_no="5127711625084043334",
                work_order_title=QIANNIU_UNRECEIVED_REFUND,
                refund_amount=19.9,
                carrier="韵达快递",
                tracking_no="40000000000000",
                wdt_order_status="已完成",
                wdt_warehouse="示例自营仓",
                wdt_logistics_company="韵达一淘1",
                source_evidence={
                    "source_system": "qianniu_aftersale",
                    "aftersale_no": "405119198641780713",
                },
            )
        )
        calls = []
        collected = {
            "ok": True,
            "detail": {"available_actions": ["同意退款", "拦截快递"]},
            "wdt": {"order_status": "已完成", "warehouse": "示例自营仓"},
        }
        self.orchestrator.collect_qianniu_task = lambda *_args, **_kwargs: collected
        self.orchestrator.prepare_qianniu_interception = lambda _task_id: {
            "ok": True,
            "group": {"group_name": "示例韵达沟通群"},
            "message": "40000000000000+拦截",
        }

        def dispatch(*_args, **_kwargs):
            calls.append("group_message")
            return {"ok": True}

        def platform(**kwargs):
            if kwargs["operation"] == "revalidate":
                return {"ok": True, "processable": True, "state": {}}
            if kwargs["operation"] == "read_trajectory":
                calls.append("hover_count")
                return {
                    "trajectory": {
                        "logistics_trajectory_count": 16,
                        "logistics_trajectory_read_complete": True,
                    },
                    "evidence": ["trajectory.png"],
                }
            assert kwargs["operation"] == "execute_intercept" and kwargs["live"] is True
            calls.append("tmall_intercept_confirm")
            return {"ok": True, "business_write_performed": True}

        self.orchestrator.dispatch_in_transit_interception = dispatch
        self.orchestrator._upsert_routed_interception_register = lambda **_kwargs: self.fail(
            "轨迹超过5条不应登记钉钉"
        )
        self.orchestrator._run_qianniu_worker = platform
        self.orchestrator._close_qianniu_task_detail = (
            lambda *_args, **_kwargs: {"ok": True}
        )

        result = self.orchestrator.process_qianniu_task(task_id, live=True)

        self.assertEqual(
            calls,
            [
                "group_message",
                "hover_count",
                "tmall_intercept_confirm",
            ],
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertTrue(result["manual_refund_followup"])
        self.assertTrue(result["dingtalk_registration_skipped"])
        self.assertEqual(self.repo.get_task(task_id)["state"], TaskState.COMPLETED)

    def test_qianniu_five_or_fewer_sends_group_refunds_then_registers(self):
        raw = dict(self.orchestrator.config.raw)
        raw["qianniu_shop_browser"] = {
            **dict(raw.get("qianniu_shop_browser") or {}),
            "workflow_enabled": True,
            "production_enabled": True,
        }
        self.orchestrator.config = replace(self.orchestrator.config, raw=raw)
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                shop_id="qianniu_01",
                shop_name="示例天猫旗舰店",
                edge_profile="Default",
                work_order_no="405119198641780713",
                order_no="5127711625084043334",
                work_order_title=QIANNIU_UNRECEIVED_REFUND,
                refund_amount=19.9,
                carrier="韵达快递",
                tracking_no="40000000000000",
                wdt_warehouse="示例自营仓",
                source_evidence={
                    "source_system": "qianniu_aftersale",
                    "aftersale_no": "405119198641780713",
                },
            )
        )
        calls = []
        self.orchestrator.collect_qianniu_task = lambda *_args, **_kwargs: {
            "ok": True,
            "detail": {"available_actions": ["同意退款", "拦截快递"]},
            "wdt": {"order_status": "已发货", "warehouse": "示例自营仓"},
        }
        self.orchestrator.prepare_qianniu_interception = lambda _task_id: {
            "ok": True,
            "group": {"group_name": "示例韵达沟通群"},
            "message": "40000000000000+拦截",
        }
        self.orchestrator.dispatch_in_transit_interception = lambda *_args, **_kwargs: (
            calls.append("group_message") or {"ok": True}
        )

        def worker(**kwargs):
            if kwargs["operation"] == "revalidate":
                return {"ok": True, "processable": True, "state": {}}
            if kwargs["operation"] == "read_trajectory":
                calls.append("hover_count")
                return {
                    "trajectory": {"logistics_trajectory_count": 5},
                    "evidence": ["trajectory.png"],
                }
            assert kwargs["operation"] == "execute_agree_refund"
            calls.append("agree_refund_confirm")
            return {"ok": True, "business_write_performed": True}

        self.orchestrator._run_qianniu_worker = worker
        self.orchestrator._close_qianniu_task_detail = (
            lambda *_args, **_kwargs: {"ok": True}
        )
        self.orchestrator._upsert_routed_interception_register = lambda **_kwargs: (
            calls.append("dingtalk_register") or {"id": 7}
        )
        self.orchestrator.sync_dingtalk_interception_register = lambda _id: (
            calls.append("dingtalk_synced") or {"ok": True}
        )

        result = self.orchestrator.process_qianniu_task(task_id, live=True)

        self.assertEqual(
            calls,
            [
                "group_message",
                "hover_count",
                "agree_refund_confirm",
                "dingtalk_register",
                "dingtalk_synced",
            ],
        )
        self.assertEqual(result["status"], "COMPLETED")

    def create_shipped_refund_action_task(self, action: str) -> int:
        shop = self.orchestrator.config.shops[0]
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                shop_id=shop.id,
                shop_name=shop.name,
                edge_profile=shop.edge_profile,
                work_order_no=f"AFTERSALE:{action}",
                order_no=f"ORDER:{action}",
                work_order_title=AFTERSALE_SHIPPED_REFUND,
                refund_type="退款",
                refund_amount=28.8,
                pdd_order_status="已发货",
                carrier="圆通速递",
                tracking_no=f"TRACKING:{action}",
                wdt_warehouse="示例自营仓",
                source_evidence={
                    "source_system": "aftersale_workbench",
                    "queue_scope": "pending_soon_overdue",
                    "interception_message": {
                        "sent_echo": True,
                        "screenshots": ["sent.png"],
                        "group_name": "示例自营仓拦截群",
                    },
                },
            )
        )
        facts = dict(self.repo.get_task(task_id)["facts"])
        facts.update(
            {
                "aftersale_status": "待商家处理",
                "list_shipping_status": "已发货",
                "logistics_read_complete": True,
                "logistics_trajectory_count": 3 if action == AGREE_REFUND else 9,
                "action_controls_read_complete": action != AGREE_REFUND,
                "has_agree_return_after_refund": (
                    action == AGREE_RETURN_AFTER_REFUND
                ),
                "has_reject_refund": action == REJECT_REFUND,
                "aftersale_detail_url": "https://example.invalid/aftersale/detail",
            }
        )
        self.repo.update_facts(task_id, facts)
        return task_id

    def test_live_flow_defaults_to_review(self):
        task = self.orchestrator.enrich_and_evaluate(self.create_live_task())
        self.assertEqual(task["state"], TaskState.NEED_REVIEW)
        self.assertFalse(task["decision"]["matched"])

    def test_preview_never_sends(self):
        task_id = self.create_live_task()
        result = self.orchestrator.preview_intercept(task_id, "中通拦截群")
        self.assertFalse(result["sent"])

    def test_shipped_no_trace_wdt_check_does_not_reference_retired_aftersale_branch(self):
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                shop_id="shop_01",
                shop_name="测试店铺",
                edge_profile="Profile",
                work_order_no="REAL-WO-SHIPPED",
                order_no="REAL-ORDER-SHIPPED",
                work_order_title="已发货无轨迹退款处理",
                tracking_no="REAL-WAYBILL-SHIPPED",
                source_evidence={"collection_mode": "edge_uia"},
            )
        )
        worker_result = {
            "workflow_id": "WF-WDT-SHIPPED-NO-TRACE-CHECK",
            "trace": [],
            "context": {
                "wdt_order_status": "待客审",
                "wdt_eligible": True,
            },
        }
        with patch(
            "pdd_rpa.orchestrator.run_isolated_wdt_workflow",
            return_value=worker_result,
        ):
            result = self.orchestrator.check_wdt_by_components(task_id)

        self.assertEqual(result["order_status"], "待客审")
        self.assertTrue(result["eligible"])

    def test_aftersale_shipped_refund_uses_status_independent_wdt_lookup(self):
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                shop_id="shop_01",
                shop_name="测试店铺",
                edge_profile="Profile",
                work_order_no="AFTERSALE:REFUND:AS-001",
                order_no="REAL-ORDER-AFTERSALE",
                work_order_title=AFTERSALE_SHIPPED_REFUND,
                pdd_order_status="已发货",
                carrier="韵达快递",
                tracking_no="REAL-WAYBILL-AFTERSALE",
                source_evidence={"source_system": "aftersale_workbench"},
            )
        )
        worker_result = {
            "workflow_id": "WF-WDT-IN-TRANSIT-LOOKUP",
            "trace": [],
            "context": {
                "wdt_result_count": 1,
                "wdt_order_id": "JY001",
                "wdt_platform": "拼多多",
                "wdt_warehouse": "示例自营仓",
                "wdt_logistics_company": "韵达一拼多多2",
                "wdt_eligible": True,
                # The real sample is 已完成 in WDT. This branch deliberately
                # uses the status-independent exact-row assertion.
                "wdt_order_status": "",
            },
        }
        with patch(
            "pdd_rpa.orchestrator.run_isolated_wdt_workflow",
            return_value=worker_result,
        ) as worker:
            result = self.orchestrator.check_wdt_by_components(task_id)

        self.assertEqual(
            worker.call_args.kwargs["workflow_id"], "WF-WDT-IN-TRANSIT-LOOKUP"
        )
        self.assertTrue(result["eligible"])
        self.assertTrue(result["requires_interception"])
        self.assertEqual(result["warehouse"], "示例自营仓")

    def test_aftersale_wdt_redecides_two_traces_for_jushuitan(self):
        task_id = self.create_shipped_refund_action_task(AGREE_REFUND)
        facts = dict(self.repo.get_task(task_id)["facts"])
        facts.update(
            {
                "logistics_trajectory_count": 2,
                "action_controls_read_complete": True,
                "has_agree_return_after_refund": True,
                "has_reject_refund": True,
                "wdt_warehouse": "",
            }
        )
        self.repo.update_facts(task_id, facts)
        worker_result = {
            "workflow_id": "WF-WDT-IN-TRANSIT-LOOKUP",
            "trace": [],
            "context": {
                "wdt_result_count": 1,
                "wdt_order_id": "JY-JST-001",
                "wdt_platform": "拼多多",
                "wdt_warehouse": "示例供应链代发总仓",
                "wdt_logistics_company": "韵达一拼多多2",
                "wdt_eligible": True,
                "wdt_order_status": "",
            },
        }

        with patch(
            "pdd_rpa.orchestrator.run_isolated_wdt_workflow",
            return_value=worker_result,
        ):
            result = self.orchestrator.check_wdt_by_components(task_id)

        self.assertEqual(result["max_agree_refund_trace_count"], 1)
        self.assertEqual(
            result["aftersale_decision"]["action"],
            AGREE_RETURN_AFTER_REFUND,
        )
        persisted = self.repo.get_task(task_id)["facts"]
        self.assertEqual(persisted["aftersale_agree_refund_trace_limit"], 1)
        self.assertEqual(
            persisted["aftersale_decision"]["action"],
            AGREE_RETURN_AFTER_REFUND,
        )

    def test_yto_old_service_label_migrates_to_verified_chat_header(self):
        routing = self.orchestrator.get_in_transit_config()
        routing["version"] = 3
        routing["schema_version"] = 3
        yto = next(item for item in routing["carrier_groups"] if item["platform"] == "yto")
        yto["group_name"] = "\u5706\u901a\u4e13\u5c5e\u5ba2\u670d"
        self.repo.set_json_setting("in_transit_routing_config", routing)

        self.orchestrator._upgrade_in_transit_routing_defaults()

        migrated = self.orchestrator.get_in_transit_config()
        yto = next(item for item in migrated["carrier_groups"] if item["platform"] == "yto")
        self.assertEqual(yto["group_name"], "示例客户服务群")
        self.assertEqual(migrated["version"], 13)
        self.assertEqual(migrated["schema_version"], 13)
        self.assertEqual(
            migrated["supported_warehouses"],
            ["示例自营仓", "示例供应链代发总仓"],
        )
        self.assertEqual(len(migrated["product_groups"]), 8)
        tianxing = next(
            item for item in migrated["product_groups"]
            if item["keyword"] == "示例品类 F"
        )
        self.assertEqual(tianxing["platform"], "qq")
        self.assertEqual(tianxing["group_name"], "示例供应商 F 分销群")
        qiwenxuan = next(
            item for item in migrated["product_groups"]
            if item["keyword"] == "示例品类 G"
        )
        self.assertEqual(qiwenxuan["platform"], "wecom")
        self.assertEqual(
            qiwenxuan["group_name"],
            "【分销】示例供应商 G 对接群",
        )
        hanzhijian = next(
            item for item in migrated["product_groups"]
            if item["keyword"] == "示例品类 H"
        )
        self.assertEqual(hanzhijian["platform"], "wecom")
        self.assertEqual(hanzhijian["group_name"], "分销1-示例团队&示例供应商 H 对接群")
        self.assertTrue(
            all(
                item["warehouse"] == "示例供应链代发总仓"
                for item in migrated["product_groups"]
            )
        )

    def test_qiwenxuan_old_target_is_replaced_without_duplicate_route(self):
        routing = self.orchestrator.get_in_transit_config()
        routing["version"] = 10
        routing["schema_version"] = 10
        qiwenxuan = next(
            item for item in routing["product_groups"]
            if item["keyword"] == "示例品类 G"
        )
        qiwenxuan["group_name"] = "分销1-示例团队&示例供应商 H 对接群"
        self.repo.set_json_setting("in_transit_routing_config", routing)

        self.orchestrator._upgrade_in_transit_routing_defaults()

        migrated = self.orchestrator.get_in_transit_config()
        matches = [
            item for item in migrated["product_groups"]
            if item["keyword"] == "示例品类 G"
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["platform"], "wecom")
        self.assertEqual(
            matches[0]["group_name"],
            "【分销】示例供应商 G 对接群",
        )
        self.assertEqual(migrated["version"], 13)

    def test_hanzhijian_route_is_appended_once(self):
        routing = self.orchestrator.get_in_transit_config()
        routing["version"] = 11
        routing["schema_version"] = 11
        routing["product_groups"] = [
            item for item in routing["product_groups"]
            if item["keyword"] != "示例品类 H"
        ]
        self.repo.set_json_setting("in_transit_routing_config", routing)

        self.orchestrator._upgrade_in_transit_routing_defaults()
        self.orchestrator._upgrade_in_transit_routing_defaults()

        migrated = self.orchestrator.get_in_transit_config()
        matches = [
            item for item in migrated["product_groups"]
            if item["keyword"] == "示例品类 H"
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["platform"], "wecom")
        self.assertEqual(matches[0]["group_name"], "分销1-示例团队&示例供应商 H 对接群")
        self.assertEqual(migrated["version"], 13)

    def test_routing_update_rejects_a_stale_page_snapshot(self):
        first = self.orchestrator.get_in_transit_config()
        expected_version = first["version"]
        updated = dict(first)
        updated["message_template"] = "{tracking_no}+拦截-测试"

        saved = self.orchestrator.update_in_transit_config(
            updated,
            expected_version=expected_version,
        )
        self.assertEqual(saved["version"], expected_version + 1)

        with self.assertRaises(RoutingConfigConflict):
            self.orchestrator.update_in_transit_config(
                first,
                expected_version=expected_version,
            )

    def test_captured_logistics_component_gets_a_separate_warehouse_reader(self):
        self.repo.save_component(
            validate_component(
                {
                    "component_id": "CMP-WDT-LOGISTICS-COMPANY",
                    "component_type": "ReadDesktopText",
                    "name": "读取旺店通物流公司",
                    "application": "旺店通ERP旗舰版",
                    "locator": {
                        "window": {"title": "旺店通ERP旗舰版", "class_name": "CoolWindow"},
                        "class_name": "CoolWindow",
                        "relative_point": {"x": 1800, "y": 338},
                    },
                    "config": {"output_variable": "wdt_logistics_company"},
                }
            )
        )
        self.orchestrator._ensure_wdt_warehouse_component()
        self.orchestrator._upgrade_wdt_in_transit_workflow()

        component = self.repo.get_component("CMP-WDT-WAREHOUSE")
        self.assertIsNotNone(component)
        self.assertEqual(component["config"]["output_variable"], "wdt_warehouse")

        workflow = self.repo.get_workflow("WF-WDT-IN-TRANSIT-LOOKUP")
        component_ids = [node["component_id"] for node in workflow["nodes"]]
        self.assertIn("CMP-WDT-WAREHOUSE", component_ids)
        self.assertIn("CMP-WDT_ORIGINAL_ORDER", component_ids)
        self.assertIn("CMP-WDT_WAYBILL", component_ids)
        self.assertLess(
            component_ids.index("CMP-WDT_WAYBILL"),
            component_ids.index("CMP-WDT-WAREHOUSE"),
        )
        self.assertLess(
            component_ids.index("CMP-WDT-WAREHOUSE"),
            component_ids.index("CMP-WDT-LOGISTICS-COMPANY"),
        )

    def test_jst_product_nodes_are_guarded_by_warehouse(self):
        workflow = self.repo.get_workflow("WF-WDT-IN-TRANSIT-LOOKUP")
        by_id = {node["node_id"]: node for node in workflow["nodes"]}
        self.assertEqual(
            by_id["goods_tab"]["when"],
            {"wdt_warehouse": "示例供应链代发总仓"},
        )
        self.assertEqual(
            by_id["product_names"]["when"],
            {"wdt_warehouse": "示例供应链代发总仓"},
        )
        self.assertIsNotNone(self.repo.get_component("CMP-WDT-GOODS-LIST-TAB"))
        self.assertIsNotNone(self.repo.get_component("CMP-WDT-PRODUCT-NAMES"))

    def test_review_task_can_be_reevaluated_after_rule_enabled(self):
        task_id = self.create_live_task()
        first = self.orchestrator.enrich_and_evaluate(task_id)
        self.assertEqual(first["state"], TaskState.NEED_REVIEW)
        self.repo.set_rule_enabled("R-INTRANSIT-001", True)
        second = self.orchestrator.enrich_and_evaluate(task_id)
        self.assertEqual(second["state"], TaskState.WAIT_APPROVAL)

    def test_live_snapshot_replaces_scanned_shop_and_keeps_unscanned_shop(self):
        selected = self.orchestrator.config.shops[:2]
        baseline = {
            "run_id": "old-run",
            "display_task_ids": [101, 202],
            "display_shops": [
                {"shop_id": selected[0].id, "shop_name": selected[0].name, "task_ids": [101]},
                {"shop_id": selected[1].id, "shop_name": selected[1].name, "task_ids": [202]},
            ],
        }
        snapshot = self.orchestrator._live_sync_snapshot(
            run_id="new-run",
            started_at="2026-08-22T12:00:00",
            requested_shop_id=None,
            selected=selected,
            results=[
                {
                    "shop_id": selected[0].id,
                    "shop_name": selected[0].name,
                    "ok": True,
                    "task_ids": [303],
                }
            ],
            next_shop_index=1,
            in_progress=True,
            baseline_snapshot=baseline,
        )

        self.assertEqual(snapshot["task_ids"], [303])
        self.assertEqual(snapshot["display_task_ids"], [303, 202])
        self.assertEqual(snapshot["display_shops"][0]["source"], "current")
        self.assertEqual(snapshot["display_shops"][1]["source"], "previous")

    def test_live_snapshot_keeps_previous_aftersale_tasks_when_subscan_fails(self):
        shop = self.orchestrator.config.shops[0]
        previous_work_order_id = self.repo.upsert_task(
            WorkOrderFacts(
                shop_id=shop.id,
                shop_name=shop.name,
                edge_profile=shop.edge_profile,
                work_order_no="PREVIOUS-WORK-ORDER",
                order_no="PREVIOUS-ORDER",
                work_order_title="已发货无轨迹退款处理",
                source_evidence={"collection_mode": "edge_uia"},
            )
        )
        previous_aftersale_id = self.repo.upsert_task(
            WorkOrderFacts(
                shop_id=shop.id,
                shop_name=shop.name,
                edge_profile=shop.edge_profile,
                work_order_no="AFTERSALE:PREVIOUS",
                order_no="PREVIOUS-AFTERSALE-ORDER",
                work_order_title="售后工作台：退货退款",
                refund_type="退货退款",
                source_evidence={
                    "collection_mode": "edge_uia",
                    "source_system": "aftersale_workbench",
                    "queue_scope": "pending_soon_overdue",
                },
            )
        )
        current_work_order_id = self.repo.upsert_task(
            WorkOrderFacts(
                shop_id=shop.id,
                shop_name=shop.name,
                edge_profile=shop.edge_profile,
                work_order_no="CURRENT-WORK-ORDER",
                order_no="CURRENT-ORDER",
                work_order_title="已发货无轨迹退款处理",
                source_evidence={"collection_mode": "edge_uia"},
            )
        )
        baseline = {
            "run_id": "old-run",
            "display_task_ids": [previous_work_order_id, previous_aftersale_id],
            "display_shops": [
                {
                    "shop_id": shop.id,
                    "shop_name": shop.name,
                    "task_ids": [previous_work_order_id, previous_aftersale_id],
                }
            ],
        }

        snapshot = self.orchestrator._live_sync_snapshot(
            run_id="partial-run",
            started_at="2026-08-24T16:00:00",
            requested_shop_id=None,
            selected=[shop],
            results=[
                {
                    "shop_id": shop.id,
                    "shop_name": shop.name,
                    "ok": True,
                    "task_ids": [current_work_order_id],
                    "aftersale_error": "售后工作台待处理即将逾期分页栏未稳定",
                }
            ],
            next_shop_index=1,
            in_progress=False,
            baseline_snapshot=baseline,
        )

        self.assertEqual(snapshot["task_ids"], [current_work_order_id])
        self.assertEqual(
            snapshot["display_task_ids"],
            [current_work_order_id, previous_aftersale_id],
        )
        self.assertNotIn(previous_work_order_id, snapshot["display_task_ids"])
        self.assertFalse(snapshot["complete"])
        shop_snapshot = snapshot["display_shops"][0]
        self.assertEqual(shop_snapshot["source"], "current_partial")
        self.assertTrue(shop_snapshot["aftersale_stale"])
        self.assertEqual(
            shop_snapshot["aftersale_sync_error"],
            "售后工作台待处理即将逾期分页栏未稳定",
        )
        self.assertEqual(
            shop_snapshot["stale_aftersale_task_ids"],
            [previous_aftersale_id],
        )

        self.repo.set_json_setting("live_sync_snapshot", snapshot)
        pending = {
            int(task["id"]): task
            for task in self.orchestrator.list_prioritized_tasks(view="pending")
        }
        self.assertFalse(pending[current_work_order_id]["source_stale"])
        self.assertTrue(pending[previous_aftersale_id]["source_stale"])
        self.assertEqual(pending[previous_aftersale_id]["task_kind"], "aftersale")
        self.assertEqual(
            pending[previous_aftersale_id]["queue_scope"],
            "pending_soon_overdue",
        )
        self.assertEqual(
            pending[previous_aftersale_id]["source_sync_error"],
            "售后工作台待处理即将逾期分页栏未稳定",
        )

    def test_live_snapshot_keeps_previous_work_orders_when_work_order_subscan_fails(self):
        shop = self.orchestrator.config.shops[0]
        previous_work_order_id = self.repo.upsert_task(
            WorkOrderFacts(
                shop_id=shop.id,
                shop_name=shop.name,
                edge_profile=shop.edge_profile,
                work_order_no="PREVIOUS-WORK-ORDER-PARTIAL",
                order_no="PREVIOUS-WORK-ORDER-PARTIAL",
                work_order_title="已发货无轨迹退款处理",
                source_evidence={"collection_mode": "edge_uia"},
            )
        )
        current_aftersale_id = self.repo.upsert_task(
            WorkOrderFacts(
                shop_id=shop.id,
                shop_name=shop.name,
                edge_profile=shop.edge_profile,
                work_order_no="AFTERSALE:CURRENT-PARTIAL",
                order_no="CURRENT-AFTERSALE-PARTIAL",
                work_order_title="售后工作台：已发货-退款",
                refund_type="退款",
                source_evidence={
                    "collection_mode": "edge_uia",
                    "source_system": "aftersale_workbench",
                    "queue_scope": "pending_soon_overdue",
                },
            )
        )
        baseline = {
            "run_id": "old-run",
            "display_task_ids": [previous_work_order_id],
            "display_shops": [
                {
                    "shop_id": shop.id,
                    "shop_name": shop.name,
                    "task_ids": [previous_work_order_id],
                }
            ],
        }

        snapshot = self.orchestrator._live_sync_snapshot(
            run_id="partial-work-order-run",
            started_at="2026-08-26T14:45:00",
            requested_shop_id=None,
            selected=[shop],
            results=[
                {
                    "shop_id": shop.id,
                    "shop_name": shop.name,
                    "ok": True,
                    "task_ids": [current_aftersale_id],
                    "work_order_error": "工单列表恢复失败",
                }
            ],
            next_shop_index=1,
            in_progress=False,
            baseline_snapshot=baseline,
        )

        self.assertEqual(snapshot["task_ids"], [current_aftersale_id])
        self.assertEqual(
            snapshot["display_task_ids"],
            [current_aftersale_id, previous_work_order_id],
        )
        self.assertFalse(snapshot["complete"])
        shop_snapshot = snapshot["display_shops"][0]
        self.assertEqual(shop_snapshot["source"], "current_partial")
        self.assertTrue(shop_snapshot["work_order_stale"])
        self.assertEqual(
            shop_snapshot["work_order_sync_error"], "工单列表恢复失败"
        )
        self.assertEqual(
            shop_snapshot["stale_work_order_task_ids"], [previous_work_order_id]
        )

        self.repo.set_json_setting("live_sync_snapshot", snapshot)
        pending = {
            int(task["id"]): task
            for task in self.orchestrator.list_prioritized_tasks(view="pending")
        }
        self.assertFalse(pending[current_aftersale_id]["source_stale"])
        self.assertTrue(pending[previous_work_order_id]["source_stale"])
        self.assertEqual(pending[previous_work_order_id]["task_kind"], "work_order")
        self.assertEqual(
            pending[previous_work_order_id]["source_sync_error"],
            "工单列表恢复失败",
        )

    def test_live_scan_retries_partial_work_order_source_before_accepting_aftersales(self):
        shop = self.orchestrator.config.shops[0]
        runtime = dict(self.orchestrator.config.raw.get("runtime") or {})
        runtime.update(
            {
                # One retry after the initial attempt.
                "scan_retry_per_shop": 1,
                "scan_retry_delay_seconds": 0.2,
            }
        )
        self.orchestrator.config = replace(
            self.orchestrator.config,
            shops=(shop,),
            raw={**self.orchestrator.config.raw, "runtime": runtime},
        )
        partial_aftersale = {
            "shop_id": shop.id,
            "shop_name": shop.name,
            "edge_profile": shop.edge_profile,
            "work_order_no": "AFTERSALE:PARTIAL-001",
            "order_no": "PARTIAL-AFTERSALE-ORDER",
            "work_order_title": "售后工作台：已发货-退款",
            "refund_type": "退款",
            "source_evidence": {
                "source_system": "aftersale_workbench",
                "queue_scope": "pending_soon_overdue",
            },
        }
        recovered_work_order = {
            "shop_id": shop.id,
            "shop_name": shop.name,
            "edge_profile": shop.edge_profile,
            "work_order_no": "CURRENT-WORK-ORDER-001",
            "order_no": "CURRENT-WORK-ORDER-001",
            "work_order_title": "已发货无轨迹退款处理",
            "source_evidence": {"collection_mode": "edge_uia"},
        }
        connector = MagicMock()
        connector.name = "fake_pdd_readonly"
        connector.discover.side_effect = [
            ConnectorResult(
                True,
                {
                    "items": [partial_aftersale],
                    "work_order_error": "待处理筛选控件不唯一：0",
                    "work_order_total": 0,
                    "aftersale_total": 1,
                },
            ),
            ConnectorResult(
                True,
                {
                    "items": [recovered_work_order],
                    "work_order_total": 1,
                    "aftersale_total": 0,
                },
            ),
        ]
        self.orchestrator.pdd_readonly = connector

        with patch("pdd_rpa.orchestrator.time.sleep"), patch.object(
            self.orchestrator,
            "_qianniu_cycle_config",
            return_value=MagicMock(workflow_enabled=False),
        ):
            discovered = self.orchestrator.discover_live_readonly()

        self.assertEqual(connector.discover.call_count, 2)
        result = discovered["results"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["work_order_error"], "")
        self.assertEqual(result["work_order_total"], 1)
        self.assertEqual(result["aftersale_total"], 0)
        self.assertEqual(len(result["task_ids"]), 1)
        task = self.repo.get_task(result["task_ids"][0])
        self.assertEqual(task["facts"]["work_order_no"], "CURRENT-WORK-ORDER-001")

    def test_collect_aftersale_detail_indexes_aftersale_evidence_as_readonly(self):
        shop = self.orchestrator.config.shops[0]
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                shop_id=shop.id,
                shop_name=shop.name,
                edge_profile=shop.edge_profile,
                work_order_no="AFTERSALE:DETAIL-001",
                order_no="AFTERSALE-ORDER-001",
                work_order_title="售后工作台：退货退款",
                refund_type="退货退款",
                source_evidence={
                    "collection_mode": "edge_uia",
                    "source_system": "aftersale_workbench",
                    "queue_scope": "pending_soon_overdue",
                    "aftersale_card": {"aftersale_no": "DETAIL-001"},
                },
            )
        )
        evidence_path = str(Path(self.temp_dir.name) / "aftersale-detail.png")
        worker_payload = {
            "facts": {
                "order_no": "AFTERSALE-ORDER-001",
                "refund_type": "退货退款",
                "shipping_status": "已发货",
                "refund_amount": 25.8,
                "has_extend_receipt": False,
                "business_write_performed": False,
            },
            "decision": {"action": "NEED_REVIEW", "reason": "测试只读详情"},
            "detail_url": "https://mms.pinduoduo.com/aftersales-ssr/detail?id=1",
            "evidence": [evidence_path],
            "detail_cleanup": {"handled": True, "list_ready": True},
            "detail_retained": False,
            "business_write_performed": False,
        }

        with patch(
            "pdd_rpa.orchestrator.run_isolated_aftersale",
            return_value=worker_payload,
        ) as worker:
            result = self.orchestrator.collect_aftersale_detail(
                task_id,
                keep_detail_open=False,
            )

        self.assertFalse(result["business_write_performed"])
        self.assertFalse(result["detail_retained"])
        self.assertEqual(
            [(item["evidence_type"], item["file_path"]) for item in result["task"]["evidence"]],
            [("pdd_aftersale_detail", evidence_path)],
        )
        worker.assert_called_once()
        self.assertFalse(worker.call_args.kwargs["keep_detail_open"])
        self.assertEqual(worker.call_args.kwargs["aftersale_no"], "DETAIL-001")

    def test_collect_aftersale_detail_rejects_work_order_task(self):
        with self.assertRaisesRegex(RuntimeError, "不是售后工作台任务"):
            self.orchestrator.collect_aftersale_detail(self.create_live_task())

    def test_interception_register_amount_and_target_follow_warehouse(self):
        task_id = self.create_shipped_refund_action_task(AGREE_REFUND)
        facts = dict(self.repo.get_task(task_id)["facts"])
        junfu = self.orchestrator._upsert_routed_interception_register(
            task_id=task_id,
            facts=facts,
            refund_reason=AFTERSALE_SHIPPED_REFUND,
        )
        self.assertEqual(junfu["dingtalk_target_key"], "junfu")
        self.assertEqual(junfu["amount"], 33.8)
        self.assertEqual(junfu["warehouse"], "示例自营仓")

        facts["wdt_warehouse"] = "示例供应链代发总仓"
        facts["source_evidence"]["interception_message"][
            "group_name"
        ] = "示例供应商 A"
        siku = self.orchestrator._upsert_routed_interception_register(
            task_id=task_id,
            facts=facts,
            refund_reason=AFTERSALE_SHIPPED_REFUND,
        )
        self.assertEqual(siku["dingtalk_target_key"], "siku_distribution")
        self.assertEqual(siku["amount"], 28.8)
        self.assertEqual(siku["group_name"], "示例供应商 A")

    def test_unknown_warehouse_fails_closed_before_register_creation(self):
        task_id = self.create_shipped_refund_action_task(AGREE_REFUND)
        facts = dict(self.repo.get_task(task_id)["facts"])
        facts["wdt_warehouse"] = "未知仓库"
        with self.assertRaisesRegex(RuntimeError, "未匹配唯一钉钉登记表"):
            self.orchestrator._upsert_routed_interception_register(
                task_id=task_id,
                facts=facts,
                refund_reason=AFTERSALE_SHIPPED_REFUND,
            )
        self.assertEqual(self.repo.list_interception_registers(), [])

    def test_sync_uses_persisted_siku_target_configuration(self):
        self.orchestrator.config.raw["dingtalk_interception_register"]["enabled"] = True
        task_id = self.create_shipped_refund_action_task(AGREE_REFUND)
        facts = dict(self.repo.get_task(task_id)["facts"])
        facts["wdt_warehouse"] = "示例供应链代发总仓"
        facts["source_evidence"]["interception_message"][
            "group_name"
        ] = "示例供应商 A"
        register = self.orchestrator._upsert_routed_interception_register(
            task_id=task_id,
            facts=facts,
            refund_reason=AFTERSALE_SHIPPED_REFUND,
        )
        with patch(
            "pdd_rpa.orchestrator.run_isolated_dingtalk_interception_register",
            return_value={
                "ok": True,
                "result": {
                    "remote_row": 108,
                    "duplicate_tracking": False,
                },
            },
        ) as worker:
            result = self.orchestrator.sync_dingtalk_interception_register(
                int(register["id"])
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["target_key"], "siku_distribution")
        kwargs = worker.call_args.kwargs
        self.assertEqual(kwargs["record"]["amount"], 28.8)
        self.assertEqual(kwargs["record"]["group_name"], "示例供应商 A")
        self.assertEqual(
            kwargs["dingtalk_config"]["sheet_name"], "2026登记"
        )
        self.assertEqual(
            kwargs["dingtalk_config"]["layout"], "siku_distribution"
        )
        self.assertEqual(
            kwargs["dingtalk_config"]["url"],
            "https://example.com/dingtalk-doc-placeholder",
        )

    def test_open_shop_windows_opens_both_dingtalk_targets_read_only(self):
        self.orchestrator.config.raw["dingtalk_interception_register"]["enabled"] = True
        self.orchestrator.config.raw["yto_service_chat"]["enabled"] = True
        self.orchestrator.config.raw["qianniu_shop_browser"]["workflow_enabled"] = True
        opened_targets = []

        def make_dingtalk_writer(config, _evidence_dir):
            opened_targets.append((config.target_key, config.sheet_name))
            writer = MagicMock()
            writer.open_window.return_value = {
                "ok": True,
                "action": "reuse",
                "opened": False,
                "verified": True,
                "profile": config.edge_profile,
                "url": config.url,
                "write_performed": False,
            }
            return writer

        yto_writer = MagicMock()
        yto_writer.open_window.return_value = {
            "ok": True,
            "action": "reuse",
            "opened": False,
            "verified": True,
            "write_performed": False,
        }
        qianniu_writer = MagicMock()
        qianniu_writer.open_window.return_value = {
            "ok": True,
            "action": "reuse",
            "opened": False,
            "verified": True,
            "write_performed": False,
        }
        with patch.object(
            self.orchestrator.edge_launcher, "open_shops", return_value=[]
        ), patch(
            "pdd_rpa.orchestrator.QianniuEdgeWindow",
            return_value=qianniu_writer,
        ), patch(
            "pdd_rpa.orchestrator.YtoServiceChatWriter",
            return_value=yto_writer,
        ), patch(
            "pdd_rpa.orchestrator.DingTalkInterceptionRegisterWriter",
            side_effect=make_dingtalk_writer,
        ):
            result = self.orchestrator.open_shop_windows()

        self.assertTrue(result["ok"])
        self.assertEqual(
            opened_targets,
            [("junfu", "2026"), ("siku_distribution", "2026登记")],
        )
        dingtalk_results = [
            item for item in result["results"] if item["kind"] == "dingtalk"
        ]
        self.assertEqual(len(dingtalk_results), 2)
        self.assertTrue(all(not item["write_performed"] for item in dingtalk_results))

    def test_return_after_refund_uses_interception_completion_with_exact_action(self):
        task_id = self.create_shipped_refund_action_task(
            AGREE_RETURN_AFTER_REFUND
        )
        facts = dict(self.repo.get_task(task_id)["facts"])
        facts["logistics_trajectory_count"] = 4
        self.repo.update_facts(task_id, facts)
        completed_payload = {
            "ok": True,
            "result": {
                "ok": True,
                "action": AGREE_RETURN_AFTER_REFUND,
                "business_write_performed": True,
            },
            "business_write_performed": True,
            "detail_cleanup": {"handled": True, "closed": 1},
        }
        with patch.object(
            self.orchestrator,
            "validate_aftersale_shipped_refund_safety",
            return_value={"action": AGREE_REFUND},
        ) as safety, patch.object(
            self.orchestrator,
            "complete_aftersale_action",
            return_value=completed_payload,
        ) as complete, patch.object(
            self.orchestrator,
            "sync_dingtalk_interception_register",
            return_value={"ok": True},
        ) as sync:
            result = (
                self.orchestrator.complete_aftersale_shipped_refund_after_interception(
                    task_id,
                    live=True,
                )
            )

        self.assertTrue(result["ok"])
        safety.assert_called_once_with(
            task_id,
            action=AGREE_RETURN_AFTER_REFUND,
            live=True,
        )
        self.assertEqual(complete.call_args.args[:2], (task_id, AGREE_RETURN_AFTER_REFUND))
        persisted = self.repo.get_task(task_id)["facts"]
        self.assertEqual(persisted["final_action"], AGREE_RETURN_AFTER_REFUND)
        self.assertTrue(persisted["refund_after_return_agreed"])
        self.assertNotIn("refund_agreed", persisted)
        sync.assert_not_called()
        self.assertNotIn("interception_register", result)
        self.assertNotIn("interception_register_dingtalk", result)
        self.assertEqual(self.repo.list_interception_registers(), [])

    def test_dingtalk_registration_failure_blocks_aftersale_pdd_action(self):
        task_id = self.create_shipped_refund_action_task(
            AGREE_REFUND
        )
        with patch.object(
            self.orchestrator,
            "validate_aftersale_shipped_refund_safety",
            return_value={"action": AGREE_REFUND},
        ), patch.object(
            self.orchestrator,
            "sync_dingtalk_interception_register",
            side_effect=RuntimeError("在线表格 worker 异常退出"),
        ) as sync, patch.object(
            self.orchestrator,
            "complete_aftersale_action",
        ) as complete:
            result = (
                self.orchestrator.complete_aftersale_shipped_refund_after_interception(
                    task_id,
                    live=True,
                )
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["needs_review"])
        self.assertTrue(result["registration_pending"])
        self.assertEqual(
            result["reason_code"],
            "DINGTALK_INTERCEPTION_REGISTER_PENDING",
        )
        self.assertFalse(result["business_write_performed"])
        self.assertFalse(result["submit_attempted"])
        self.assertEqual(
            result["interception_register_dingtalk"]["reason_code"],
            "DINGTALK_INTERCEPTION_SYNC_EXCEPTION",
        )
        complete.assert_not_called()
        sync.assert_called_once()
        registers = self.repo.list_interception_registers()
        self.assertEqual(len(registers), 1)
        self.assertEqual(registers[0]["task_id"], task_id)
        self.assertNotIn("final_action", self.repo.get_task(task_id)["facts"])

    def test_registration_retry_reuses_local_row_and_only_then_calls_pdd(self):
        task_id = self.create_shipped_refund_action_task(
            AGREE_REFUND
        )
        sync_register_ids = []

        def sync(register_id):
            sync_register_ids.append(register_id)
            if len(sync_register_ids) == 1:
                return {"ok": False, "error": "第一次同步失败"}
            return {"ok": True, "already_synced": False, "remote_row": 88}

        completed_payload = {
            "ok": True,
            "result": {
                "ok": True,
                "action": AGREE_REFUND,
                "business_write_performed": True,
            },
            "business_write_performed": True,
        }
        with patch.object(
            self.orchestrator,
            "validate_aftersale_shipped_refund_safety",
            return_value={"action": AGREE_RETURN_AFTER_REFUND},
        ), patch.object(
            self.orchestrator,
            "sync_dingtalk_interception_register",
            side_effect=sync,
        ), patch.object(
            self.orchestrator,
            "complete_aftersale_action",
            return_value=completed_payload,
        ) as complete:
            first = (
                self.orchestrator.complete_aftersale_shipped_refund_after_interception(
                    task_id,
                    live=True,
                )
            )
            self.assertTrue(first["registration_pending"])
            complete.assert_not_called()

            second = (
                self.orchestrator.complete_aftersale_shipped_refund_after_interception(
                    task_id,
                    live=True,
                )
            )

        self.assertTrue(second["ok"])
        complete.assert_called_once_with(
            task_id,
            AGREE_REFUND,
            live=True,
            cancel_event=None,
        )
        self.assertEqual(len(self.repo.list_interception_registers()), 1)
        self.assertEqual(len(sync_register_ids), 2)
        self.assertEqual(sync_register_ids[0], sync_register_ids[1])
        self.assertEqual(
            second["interception_register"]["id"],
            sync_register_ids[0],
        )
        self.assertTrue(second["interception_register_dingtalk"]["ok"])

    def test_stale_running_dingtalk_sync_fails_closed_without_remote_replay(self):
        self.orchestrator.config.raw["dingtalk_interception_register"]["enabled"] = True
        task_id = self.create_shipped_refund_action_task(
            AGREE_REFUND
        )
        facts = self.repo.get_task(task_id)["facts"]
        register = self.repo.upsert_interception_register(
            task_id=task_id,
            record_date="2026-08-25",
            carrier=str(facts["carrier"]),
            tracking_no=str(facts["tracking_no"]),
            order_no=str(facts["order_no"]),
            refund_reason=AFTERSALE_SHIPPED_REFUND,
            amount=33.8,
        )
        started = self.repo.begin_dingtalk_interception_sync(int(register["id"]))
        self.assertEqual(started["sync"]["status"], "running")

        with patch(
            "pdd_rpa.orchestrator.run_isolated_dingtalk_interception_register",
        ) as worker:
            result = self.orchestrator.sync_dingtalk_interception_register(
                int(register["id"])
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["needs_review"])
        self.assertTrue(result["write_outcome_unknown"])
        self.assertEqual(
            result["reason_code"],
            "DINGTALK_INTERCEPTION_WRITE_OUTCOME_UNKNOWN",
        )
        worker.assert_not_called()
        self.assertEqual(
            self.repo.get_dingtalk_interception_sync(int(register["id"]))["status"],
            "running",
        )

    def test_trace_branch_action_cannot_be_overridden_to_bypass_registration_rule(self):
        direct_refund_task = self.create_shipped_refund_action_task(AGREE_REFUND)
        return_after_refund_task = self.create_shipped_refund_action_task(
            AGREE_RETURN_AFTER_REFUND
        )
        return_facts = dict(self.repo.get_task(return_after_refund_task)["facts"])
        return_facts["logistics_trajectory_count"] = 4
        self.repo.update_facts(return_after_refund_task, return_facts)

        with patch.object(
            self.orchestrator,
            "validate_aftersale_shipped_refund_safety",
        ) as safety, patch.object(
            self.orchestrator,
            "sync_dingtalk_interception_register",
        ) as sync, patch.object(
            self.orchestrator,
            "complete_aftersale_action",
        ) as complete:
            with self.assertRaisesRegex(RuntimeError, "动作与最新详情决策不一致"):
                self.orchestrator.complete_aftersale_shipped_refund_after_interception(
                    direct_refund_task,
                    action=AGREE_RETURN_AFTER_REFUND,
                    live=True,
                )
            with self.assertRaisesRegex(RuntimeError, "动作与最新详情决策不一致"):
                self.orchestrator.complete_aftersale_shipped_refund_after_interception(
                    return_after_refund_task,
                    action=AGREE_REFUND,
                    live=True,
                )

        safety.assert_not_called()
        sync.assert_not_called()
        complete.assert_not_called()
        self.assertEqual(self.repo.list_interception_registers(), [])

    def test_reject_refund_uses_interception_completion_without_dingtalk_register(self):
        task_id = self.create_shipped_refund_action_task(REJECT_REFUND)
        with patch.object(
            self.orchestrator,
            "validate_aftersale_shipped_refund_safety",
            return_value={"action": REJECT_REFUND},
        ) as safety, patch.object(
            self.orchestrator,
            "complete_aftersale_action",
            return_value={
                "ok": True,
                "result": {
                    "ok": True,
                    "action": REJECT_REFUND,
                    "business_write_performed": True,
                },
                "business_write_performed": True,
                "detail_cleanup": {"handled": True, "closed": 1},
            },
        ) as complete, patch.object(
            self.orchestrator,
            "sync_dingtalk_interception_register",
        ) as sync:
            result = self.orchestrator.complete_aftersale_shipped_refund_after_interception(
                task_id,
                live=True,
            )

        self.assertTrue(result["ok"])
        safety.assert_called_once_with(
            task_id,
            action=REJECT_REFUND,
            live=True,
        )
        complete.assert_called_once_with(
            task_id,
            REJECT_REFUND,
            live=True,
            cancel_event=None,
        )
        sync.assert_not_called()
        self.assertEqual(self.repo.list_interception_registers(), [])
        persisted = self.repo.get_task(task_id)["facts"]
        self.assertEqual(persisted["final_action"], REJECT_REFUND)
        self.assertTrue(persisted["refund_rejected"])
        self.assertNotIn("refund_after_return_agreed", persisted)

    def test_reject_interception_completion_preserves_outer_cooldown_state(self):
        task_id = self.create_shipped_refund_action_task(REJECT_REFUND)
        with patch.object(
            self.orchestrator,
            "validate_aftersale_shipped_refund_safety",
            return_value={"action": REJECT_REFUND},
        ), patch.object(
            self.orchestrator,
            "complete_aftersale_action",
            return_value={
                "ok": True,
                "result": {
                    "ok": True,
                    "retryable": True,
                    "reason_code": "PDD_REJECT_COOLDOWN",
                    "retry_after_seconds": 1800,
                    "prompt_text": "待倒计时结束后可继续处理驳回",
                    "submit_attempted": False,
                },
                "deferred": True,
                "business_write_performed": False,
                "detail_cleanup": {"handled": True, "closed": 1},
            },
        ), patch.object(
            self.orchestrator,
            "sync_dingtalk_interception_register",
        ) as sync:
            result = self.orchestrator.complete_aftersale_shipped_refund_after_interception(
                task_id,
                live=True,
            )

        self.assertTrue(result["deferred"])
        self.assertTrue(result["retryable"])
        self.assertFalse(result["business_write_performed"])
        self.assertEqual(result["reason_code"], "PDD_REJECT_COOLDOWN")
        sync.assert_not_called()
        persisted = self.repo.get_task(task_id)["facts"]
        self.assertNotIn("final_action", persisted)
        self.assertNotIn("refund_rejected", persisted)

    def test_reject_refund_generic_action_does_not_dispatch_or_register(self):
        task_id = self.create_shipped_refund_action_task(REJECT_REFUND)
        worker_payload = {
            "ok": True,
            "result": {
                "ok": True,
                "action": REJECT_REFUND,
                "business_write_performed": True,
            },
            "business_write_performed": True,
            "evidence": [],
            "detail_cleanup": {"handled": True, "closed": 1},
        }
        with patch(
            "pdd_rpa.orchestrator.run_isolated_aftersale",
            return_value=worker_payload,
        ), patch(
            "pdd_rpa.orchestrator.assert_action_allowed",
        ), patch.object(
            self.orchestrator,
            "dispatch_in_transit_interception",
        ) as dispatch, patch.object(
            self.orchestrator,
            "sync_dingtalk_interception_register",
        ) as sync:
            result = self.orchestrator.complete_aftersale_action(
                task_id,
                REJECT_REFUND,
                live=True,
            )

        self.assertTrue(result["ok"])
        dispatch.assert_not_called()
        sync.assert_not_called()
        self.assertEqual(self.repo.list_interception_registers(), [])
        self.assertEqual(
            self.repo.get_task(task_id)["facts"]["aftersale_final_action"],
            REJECT_REFUND,
        )

    def test_deferred_aftersale_action_is_not_a_final_success(self):
        task_id = self.create_shipped_refund_action_task(REJECT_REFUND)
        worker_payload = {
            "ok": True,
            "result": {
                "ok": True,
                "deferred": True,
                "reason_code": "PDD_REJECT_COOLDOWN",
                "retry_after_seconds": 1800,
                "business_write_performed": False,
            },
            "business_write_performed": False,
            "evidence": [],
        }
        with patch(
            "pdd_rpa.orchestrator.run_isolated_aftersale",
            return_value=worker_payload,
        ):
            result = self.orchestrator.complete_aftersale_action(
                task_id,
                REJECT_REFUND,
                live=False,
            )

        self.assertTrue(result["deferred"])
        facts = self.repo.get_task(task_id)["facts"]
        self.assertNotIn("aftersale_final_action", facts)
        self.assertEqual(facts["aftersale_last_action_attempt"]["status"], "deferred")
        final_logs = [
            item
            for item in self.repo.list_logs(task_id=task_id)
            if item["action_type"] == "AFTERSALE_FINAL_ACTION"
        ]
        self.assertEqual([item["status"] for item in final_logs], ["deferred"])

    def test_unknown_write_outcome_is_not_a_final_success(self):
        task_id = self.create_shipped_refund_action_task(REJECT_REFUND)
        worker_payload = {
            "ok": True,
            "result": {
                "ok": True,
                "write_outcome_unknown": True,
                "business_write_performed": True,
            },
            "business_write_performed": True,
            "evidence": [],
        }
        with patch(
            "pdd_rpa.orchestrator.run_isolated_aftersale",
            return_value=worker_payload,
        ):
            result = self.orchestrator.complete_aftersale_action(
                task_id,
                REJECT_REFUND,
                live=False,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["write_outcome_unknown"])
        facts = self.repo.get_task(task_id)["facts"]
        self.assertNotIn("aftersale_final_action", facts)
        self.assertEqual(facts["aftersale_last_action_attempt"]["status"], "uncertain")
        final_logs = [
            item
            for item in self.repo.list_logs(task_id=task_id)
            if item["action_type"] == "AFTERSALE_FINAL_ACTION"
        ]
        self.assertEqual([item["status"] for item in final_logs], ["uncertain"])

    def test_pending_view_is_not_limited_to_200_live_tasks(self):
        task_ids = []
        for index in range(205):
            task_ids.append(
                self.repo.upsert_task(
                    WorkOrderFacts(
                        shop_id="shop_01",
                        shop_name="测试店铺",
                        edge_profile="Profile",
                        work_order_no=f"LIVE-WO-{index:03d}",
                        order_no=f"LIVE-ORDER-{index:03d}",
                        work_order_title="已发货无轨迹退款处理",
                        source_evidence={"collection_mode": "edge_uia"},
                    )
                )
            )
        self.repo.set_json_setting(
            "live_sync_snapshot",
            {"run_id": "large-run", "task_ids": task_ids, "display_task_ids": task_ids},
        )

        tasks = self.orchestrator.list_prioritized_tasks(view="pending")

        self.assertEqual(len(tasks), 205)
        self.assertTrue(all(task["live_pending"] for task in tasks))


if __name__ == "__main__":
    unittest.main()
