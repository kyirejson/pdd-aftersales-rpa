import gc
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pdd_rpa.aftersale_workbench import AFTERSALE_RETURN_REFUND
from pdd_rpa.db import Repository
from pdd_rpa.models import Shop, TaskState, WorkOrderFacts


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp_dir.name) / "test.db")
        self.repo.initialize()
        self.repo.seed_shops((Shop("shop_01", "测试店铺", "Profile"),))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_business_key_is_idempotent(self):
        facts = WorkOrderFacts(
            shop_id="shop_01",
            shop_name="测试店铺",
            edge_profile="Profile",
            work_order_no="WO-1",
            order_no="ORDER-1",
        )
        first = self.repo.upsert_task(facts)
        second = self.repo.upsert_task(facts)
        self.assertEqual(first, second)
        self.assertEqual(len(self.repo.list_tasks()), 1)

    def test_seed_shops_disables_entries_removed_from_authoritative_config(self):
        self.repo.seed_shops(
            (
                Shop("shop_01", "测试店铺", "Profile"),
                Shop("shop_02", "已移除店铺", "Profile 2"),
            )
        )

        self.repo.seed_shops((Shop("shop_01", "测试店铺", "Profile"),))

        dashboard_shops = self.repo.dashboard()["shops"]
        self.assertEqual([item["id"] for item in dashboard_shops], ["shop_01"])
        with self.repo.connect() as conn:
            removed = conn.execute(
                "SELECT enabled FROM shops WHERE id='shop_02'"
            ).fetchone()
        self.assertEqual(int(removed["enabled"]), 0)

    def test_list_rediscovery_does_not_clear_detail_and_wdt_enrichment(self):
        facts = WorkOrderFacts(
            "shop_01", "测试店铺", "Profile", "WO-R", "ORDER-R",
            tracking_no="YT4068262410", carrier="圆通快递", refund_amount=28.8,
            wdt_result_count=1, wdt_order_status="待客审", wdt_eligible=True,
            screenshots=["detail.png"], source_evidence={"detail_url": "https://example/detail"},
        )
        task_id = self.repo.upsert_task(facts)
        self.repo.upsert_task(
            WorkOrderFacts(
                "shop_01", "测试店铺", "Profile", "WO-R", "ORDER-R",
                screenshots=["list.png"], source_evidence={"list_url": "https://example/list"},
            )
        )
        stored = self.repo.get_task(task_id)["facts"]
        self.assertEqual(stored["tracking_no"], "YT4068262410")
        self.assertEqual(stored["wdt_order_status"], "待客审")
        self.assertTrue(stored["wdt_eligible"])
        self.assertEqual(stored["screenshots"], ["detail.png", "list.png"])
        self.assertIn("detail_url", stored["source_evidence"])
        self.assertIn("list_url", stored["source_evidence"])

    def test_defer_task_retry_atomically_persists_state_metadata_and_audit(self):
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                "shop_01", "测试店铺", "Profile", "WO-RETRY", "ORDER-RETRY"
            )
        )

        retry = self.repo.defer_task_retry(
            task_id,
            "2026-08-25T12:30:00",
            "PDD_REJECT_COOLDOWN",
            "请耐心等待消费者回复，并协商解决方案",
        )

        self.assertEqual(
            retry,
            {
                "reason_code": "PDD_REJECT_COOLDOWN",
                "prompt_text": "请耐心等待消费者回复，并协商解决方案",
                "retry_not_before": "2026-08-25T12:30:00",
                "attempts": 1,
                "business_write_performed": False,
            },
        )
        reopened = Repository(self.repo.path)
        stored = reopened.get_task(task_id)
        self.assertEqual(stored["state"], TaskState.RETRY_PENDING)
        self.assertIsNone(stored["completed_at"])
        self.assertEqual(
            stored["facts"]["source_evidence"]["aftersale_retry"], retry
        )
        latest = reopened.list_logs(task_id)[0]
        self.assertEqual(latest["action_type"], "TASK_RETRY_DEFERRED")
        self.assertEqual(latest["status"], "success")
        self.assertEqual(latest["details"]["from"], TaskState.DISCOVERED)
        self.assertEqual(latest["details"]["to"], TaskState.RETRY_PENDING)
        self.assertFalse(latest["details"]["business_write_performed"])

    def test_aftersale_retry_survives_rediscovery_and_deep_merges_partial_metadata(self):
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                "shop_01", "测试店铺", "Profile", "WO-RETRY-R", "ORDER-RETRY-R"
            )
        )
        self.repo.defer_task_retry(
            task_id,
            "2026-08-25T12:30:00",
            "PDD_REJECT_COOLDOWN",
            "等待消费者回复",
        )

        self.repo.upsert_task(
            WorkOrderFacts(
                "shop_01",
                "测试店铺",
                "Profile",
                "WO-RETRY-R",
                "ORDER-RETRY-R",
                source_evidence={
                    "collection_mode": "edge_uia",
                    "aftersale_retry": {"scan_observed": True},
                },
            )
        )

        stored = self.repo.get_task(task_id)
        retry = stored["facts"]["source_evidence"]["aftersale_retry"]
        self.assertEqual(stored["state"], TaskState.RETRY_PENDING)
        self.assertEqual(retry["reason_code"], "PDD_REJECT_COOLDOWN")
        self.assertEqual(retry["retry_not_before"], "2026-08-25T12:30:00")
        self.assertEqual(retry["attempts"], 1)
        self.assertTrue(retry["scan_observed"])

        second = self.repo.defer_task_retry(
            task_id,
            "2026-08-25T13:00:00",
            "PDD_REJECT_COOLDOWN",
            "平台仍要求等待消费者回复",
        )
        self.assertEqual(second["attempts"], 2)

    def test_post_submit_retry_persists_verified_negotiation_context(self):
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                "shop_01", "测试店铺", "Profile", "WO-STAGE", "ORDER-STAGE"
            )
        )

        retry = self.repo.defer_task_retry(
            task_id,
            "2026-08-25T12:30:00",
            "PDD_REJECT_NEGOTIATION_COOLDOWN",
            "待倒计时结束后可继续处理驳回",
            {
                "business_write_performed": True,
                "submit_attempted": True,
                "defer_kind": "POST_SUBMIT_VERIFIED",
                "stage_committed": True,
                "negotiation_round_submitted": True,
                "resume_action": "REJECT_REFUND",
                "reject_surface": "KEEP_REJECT",
                "shop_id": "shop_01",
                "order_no": "ORDER-STAGE",
                "aftersale_no": "AS-STAGE",
            },
        )

        self.assertTrue(retry["business_write_performed"])
        self.assertTrue(retry["submit_attempted"])
        self.assertEqual(retry["defer_kind"], "POST_SUBMIT_VERIFIED")
        self.assertEqual(retry["resume_action"], "REJECT_REFUND")
        stored = self.repo.get_task(task_id)
        self.assertEqual(
            stored["facts"]["source_evidence"]["aftersale_retry"], retry
        )

    def test_clear_task_retry_removes_gate_and_rediscovery_does_not_restore_it(self):
        facts = WorkOrderFacts(
            "shop_01", "测试店铺", "Profile", "WO-RETRY-C", "ORDER-RETRY-C"
        )
        task_id = self.repo.upsert_task(facts)
        self.repo.defer_task_retry(
            task_id,
            "2026-08-25T12:30:00",
            "PDD_REJECT_COOLDOWN",
            "等待平台倒计时结束",
        )

        self.assertTrue(self.repo.clear_task_retry(task_id, "售后处理已形成终态"))
        self.assertFalse(self.repo.clear_task_retry(task_id, "重复清除"))
        stored = self.repo.get_task(task_id)
        self.assertNotIn(
            "aftersale_retry", stored["facts"].get("source_evidence", {})
        )
        self.assertEqual(stored["state"], TaskState.RETRY_PENDING)
        latest = self.repo.list_logs(task_id)[0]
        self.assertEqual(latest["action_type"], "TASK_RETRY_CLEARED")
        self.assertEqual(latest["details"]["reason"], "售后处理已形成终态")

        self.repo.upsert_task(facts)
        rediscovered = self.repo.get_task(task_id)
        self.assertNotIn(
            "aftersale_retry",
            rediscovered["facts"].get("source_evidence", {}),
        )

    def test_transition_is_audited(self):
        task_id = self.repo.upsert_task(
            WorkOrderFacts("shop_01", "测试店铺", "Profile", "WO-2", "ORDER-2")
        )
        self.repo.transition(task_id, TaskState.COLLECTING_DATA, "测试")
        self.assertEqual(self.repo.get_task(task_id)["state"], TaskState.COLLECTING_DATA)
        self.assertEqual(self.repo.list_logs(task_id)[0]["action_type"], "STATE_TRANSITION")

    def test_manual_status_update_allows_operator_reset_and_is_audited(self):
        task_id = self.repo.upsert_task(
            WorkOrderFacts("shop_01", "测试店铺", "Profile", "WO-M", "ORDER-M")
        )
        self.repo.mark_task_status(task_id, TaskState.FAILED, "自动执行失败")

        updated = self.repo.set_task_status_manual(
            task_id, TaskState.DISCOVERED, "控制台人工重置为待处理"
        )

        self.assertEqual(updated["state"], TaskState.DISCOVERED)
        latest = self.repo.list_logs(task_id)[0]
        self.assertEqual(latest["action_type"], "MANUAL_STATE_UPDATE")
        self.assertTrue(latest["details"]["manual"])
        self.assertFalse(latest["details"]["verified_outcome"])
        self.assertEqual(latest["details"]["from"], TaskState.FAILED)
        self.assertEqual(latest["details"]["to"], TaskState.DISCOVERED)

    def test_rediscovered_transient_not_found_task_returns_to_discovered(self):
        facts = WorkOrderFacts(
            "shop_01", "测试店铺", "Profile", "WO-REDISCOVER", "ORDER-REDISCOVER"
        )
        task_id = self.repo.upsert_task(facts)
        self.repo.mark_task_status(
            task_id,
            TaskState.REJECTED,
            "订单号在当前待处理列表中不存在，跳过",
        )

        self.repo.upsert_task(facts)

        self.assertEqual(self.repo.get_task(task_id)["state"], TaskState.DISCOVERED)
        latest = self.repo.list_logs(task_id)[0]
        self.assertEqual(latest["action_type"], "STATE_STATUS_UPDATE")
        self.assertEqual(latest["details"]["from"], TaskState.REJECTED)
        self.assertEqual(latest["details"]["to"], TaskState.DISCOVERED)
        self.assertEqual(
            latest["details"]["recovery_trigger"], "rediscovered_current_pending"
        )

    def test_rediscovery_does_not_reset_other_rejected_tasks(self):
        facts = WorkOrderFacts(
            "shop_01", "测试店铺", "Profile", "WO-REJECTED", "ORDER-REJECTED"
        )
        task_id = self.repo.upsert_task(facts)
        self.repo.mark_task_status(task_id, TaskState.REJECTED, "旺店通订单状态不符合退款条件")

        self.repo.upsert_task(facts)

        self.assertEqual(self.repo.get_task(task_id)["state"], TaskState.REJECTED)

    def test_rediscovery_does_not_reset_completed_tasks(self):
        facts = WorkOrderFacts(
            "shop_01", "测试店铺", "Profile", "WO-COMPLETED", "ORDER-COMPLETED"
        )
        task_id = self.repo.upsert_task(facts)
        self.repo.mark_task_status(task_id, TaskState.COMPLETED, "退款已核验完成")

        self.repo.upsert_task(facts)

        self.assertEqual(self.repo.get_task(task_id)["state"], TaskState.COMPLETED)

    def test_failed_qianniu_read_is_reactivated_when_still_pending_next_scan(self):
        facts = WorkOrderFacts(
            "qianniu_01",
            "示例天猫旗舰店",
            "Default",
            "405449822671293405",
            "5127709501019017748",
            source_evidence={
                "source_system": "qianniu_aftersale",
                "automation_eligible": True,
            },
        )
        task_id = self.repo.upsert_task(facts)
        self.repo.mark_task_status(
            task_id, TaskState.FAILED, "详情页未读取到唯一有效物流单号"
        )

        self.repo.upsert_task(facts)

        stored = self.repo.get_task(task_id)
        self.assertEqual(stored["state"], TaskState.DISCOVERED)
        latest = self.repo.list_logs(task_id)[0]
        self.assertEqual(
            latest["details"]["recovery_trigger"],
            "rediscovered_qianniu_pending",
        )

    def test_current_return_refund_rediscovery_reactivates_completed_task(self):
        facts = WorkOrderFacts(
            "shop_01",
            "测试店铺",
            "Profile",
            "AFTERSALE:ORDER-RETURN-REDISCOVERED",
            "ORDER-RETURN-REDISCOVERED",
            work_order_title=AFTERSALE_RETURN_REFUND,
            refund_type="退货退款",
            source_evidence={
                "source_system": "aftersale_workbench",
                "queue_scope": "pending_soon_overdue",
                "filter_label": "待处理即将逾期",
            },
        )
        task_id = self.repo.upsert_task(facts)
        self.repo.mark_task_status(task_id, TaskState.COMPLETED, "本轮已延长收货")

        rediscovered_id = self.repo.upsert_task(facts)

        self.assertEqual(rediscovered_id, task_id)
        stored = self.repo.get_task(task_id)
        self.assertEqual(stored["state"], TaskState.DISCOVERED)
        self.assertIsNone(stored["completed_at"])
        latest = self.repo.list_logs(task_id)[0]
        self.assertEqual(latest["action_type"], "STATE_STATUS_UPDATE")
        self.assertEqual(latest["details"]["from"], TaskState.COMPLETED)
        self.assertEqual(latest["details"]["to"], TaskState.DISCOVERED)
        self.assertEqual(
            latest["details"]["recovery_trigger"],
            "rediscovered_return_refund_pending",
        )

    def test_completed_return_refund_outside_current_pending_scope_stays_completed(self):
        facts = WorkOrderFacts(
            "shop_01",
            "测试店铺",
            "Profile",
            "AFTERSALE:ORDER-RETURN-HISTORY",
            "ORDER-RETURN-HISTORY",
            work_order_title=AFTERSALE_RETURN_REFUND,
            refund_type="退货退款",
            source_evidence={
                "source_system": "aftersale_workbench",
                "queue_scope": "history",
                "filter_label": "全部售后",
            },
        )
        task_id = self.repo.upsert_task(facts)
        self.repo.mark_task_status(task_id, TaskState.COMPLETED, "历史任务已完成")

        self.repo.upsert_task(facts)

        self.assertEqual(self.repo.get_task(task_id)["state"], TaskState.COMPLETED)

    def test_rediscovery_requires_fresh_pending_scope_not_preserved_old_metadata(self):
        current = WorkOrderFacts(
            "shop_01",
            "测试店铺",
            "Profile",
            "AFTERSALE:ORDER-RETURN-NOT-CURRENT",
            "ORDER-RETURN-NOT-CURRENT",
            work_order_title=AFTERSALE_RETURN_REFUND,
            refund_type="退货退款",
            source_evidence={
                "source_system": "aftersale_workbench",
                "queue_scope": "pending_soon_overdue",
                "filter_label": "待处理即将逾期",
            },
        )
        task_id = self.repo.upsert_task(current)
        self.repo.mark_task_status(task_id, TaskState.COMPLETED, "本轮已延长收货")

        self.repo.upsert_task(
            WorkOrderFacts(
                "shop_01",
                "测试店铺",
                "Profile",
                "AFTERSALE:ORDER-RETURN-NOT-CURRENT",
                "ORDER-RETURN-NOT-CURRENT",
                work_order_title=AFTERSALE_RETURN_REFUND,
                refund_type="退货退款",
                source_evidence={},
            )
        )

        self.assertEqual(self.repo.get_task(task_id)["state"], TaskState.COMPLETED)

    def test_rule_toggle_survives_definition_reseed(self):
        rule = {"rule_id": "R1", "name": "规则", "enabled": False, "priority": 1}
        self.repo.seed_rules([rule])
        self.repo.set_rule_enabled("R1", True)
        self.repo.seed_rules([rule])
        self.assertTrue(self.repo.get_rule_enabled("R1"))

    def test_dashboard_counts_only_live_tasks_as_sources(self):
        internal = WorkOrderFacts("shop_01", "测试店铺", "Profile", "WO-I", "ORDER-I")
        live = WorkOrderFacts(
            "shop_01",
            "测试店铺",
            "Profile",
            "WO-L",
            "ORDER-L",
            work_order_title="在途无理由退款处理",
            source_evidence={"collection_mode": "edge_readonly"},
        )
        self.repo.upsert_task(internal)
        self.repo.upsert_task(live)
        dashboard = self.repo.dashboard()
        self.assertEqual(dashboard["sources"], {"live": 1})
        self.assertEqual(dashboard["targets"]["in_transit_no_reason"], 1)

    def test_interception_register_is_idempotent(self):
        task_id = self.repo.upsert_task(
            WorkOrderFacts("shop_01", "测试店铺", "Profile", "WO-RG", "ORDER-RG")
        )
        first = self.repo.upsert_interception_register(
            task_id=task_id,
            record_date="2026-08-07",
            carrier="中通快递",
            tracking_no="40000000000000",
            order_no="ORDER-RG",
            refund_reason="主观原因不想要",
            amount=28.8,
        )
        second = self.repo.upsert_interception_register(
            task_id=task_id,
            record_date="2026-08-07",
            carrier="中通快递",
            tracking_no="40000000000000",
            order_no="ORDER-RG",
            refund_reason="主观原因不想要",
            amount=28.8,
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["tracking_no"], "40000000000000")
        self.assertEqual(len(self.repo.list_interception_registers()), 1)

    def test_initialize_backfills_interception_evidence_from_legacy_dispatch_log(self):
        task_id = self.repo.upsert_task(
            WorkOrderFacts("shop_01", "测试店铺", "Profile", "WO-EV", "ORDER-EV")
        )
        screenshot = str(Path(self.temp_dir.name) / "interception.png")
        self.repo.log_action(
            task_id,
            "legacy-dispatch",
            "INTERCEPT_MESSAGE_DISPATCHED",
            "success",
            {"evidence": {"evidence": [screenshot]}},
        )

        self.repo.initialize()

        evidence = self.repo.list_task_evidence(task_id)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["evidence_type"], "interception_group")
        self.assertEqual(evidence[0]["file_path"], screenshot)

    def test_initialize_labels_qianniu_screenshot_as_qianniu_evidence(self):
        screenshot = str(Path(self.temp_dir.name) / "qianniu_detail.png")
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                "qianniu_01",
                "示例天猫旗舰店",
                "Default",
                "405594252005293405",
                "5127709501019017748",
                screenshots=[screenshot],
                source_evidence={"source_system": "qianniu_aftersale"},
            )
        )
        self.repo.add_task_evidence(
            task_id, "pdd_work_order_detail", screenshot
        )

        self.repo.initialize()

        evidence = self.repo.list_task_evidence(task_id)
        self.assertEqual(
            [(item["evidence_type"], item["file_path"]) for item in evidence],
            [("qianniu_aftersale_detail", screenshot)],
        )

    def test_initialize_removes_shop_scan_snapshots_from_task_evidence_only(self):
        detail = str(Path(self.temp_dir.name) / "detail_shop_01_ORDER-EV_uia.png")
        scan = str(Path(self.temp_dir.name) / "pdd_shop_01_20260820_101530_uia.png")
        message = str(Path(self.temp_dir.name) / "wechat_ORDER-EV.png")
        task_id = self.repo.upsert_task(
            WorkOrderFacts(
                "shop_01",
                "测试店铺",
                "Profile",
                "WO-SCAN",
                "ORDER-SCAN",
                screenshots=[scan, detail, message],
                source_evidence={
                    "scan_screenshot": scan,
                    "interception_message": {"screenshots": [message]},
                },
            )
        )
        self.repo.add_task_evidence(task_id, "pdd_work_order_detail", scan)
        self.repo.add_task_evidence(task_id, "pdd_work_order_detail", detail)
        self.repo.add_task_evidence(task_id, "interception_group", message)

        self.repo.initialize()

        stored = self.repo.get_task(task_id)["facts"]
        self.assertEqual(stored["screenshots"], [detail, message])
        self.assertEqual(stored["source_evidence"]["scan_screenshot"], scan)
        evidence = self.repo.list_task_evidence(task_id)
        self.assertEqual(
            {(item["evidence_type"], item["file_path"]) for item in evidence},
            {
                ("pdd_work_order_detail", detail),
                ("interception_group", message),
            },
        )

    def test_dingtalk_register_sync_ledger_retries_without_reserving_a_row(self):
        first_task = self.repo.upsert_task(
            WorkOrderFacts("shop_01", "测试店铺", "Profile", "WO-DT-1", "ORDER-DT-1")
        )
        first_register = self.repo.upsert_interception_register(
            task_id=first_task,
            record_date="2026-08-12",
            carrier="中通快递",
            tracking_no="40000000000000",
            order_no="ORDER-DT-1",
            refund_reason="主观原因不想要",
            amount=14.88,
        )
        first_attempt = self.repo.begin_dingtalk_interception_sync(first_register["id"])
        self.assertFalse(first_attempt["already_synced"])
        self.assertEqual(first_attempt["sync"]["status"], "running")
        self.assertNotIn("remote_row", first_attempt["record"])

        failed = self.repo.mark_dingtalk_interception_sync_failure(
            first_register["id"], "window locked"
        )
        self.assertEqual(failed["status"], "pending")
        retry = self.repo.begin_dingtalk_interception_sync(first_register["id"])
        self.assertEqual(retry["sync"]["attempts"], 2)
        success = self.repo.mark_dingtalk_interception_sync_success(
            first_register["id"], remote_row=23876, duplicate_tracking=False
        )
        self.assertEqual(success["remote_row"], 23876)
        self.assertEqual(success["status"], "succeeded")
        already = self.repo.begin_dingtalk_interception_sync(first_register["id"])
        self.assertTrue(already["already_synced"])
        self.assertEqual(self.repo.list_pending_dingtalk_interception_registers(), [])
        displayed = self.repo.list_interception_registers()[0]
        self.assertEqual(displayed["dingtalk_sync_status"], "succeeded")
        self.assertEqual(displayed["dingtalk_remote_row"], 23876)

        second_task = self.repo.upsert_task(
            WorkOrderFacts("shop_01", "测试店铺", "Profile", "WO-DT-2", "ORDER-DT-2")
        )
        second_register = self.repo.upsert_interception_register(
            task_id=second_task,
            record_date="2026-08-12",
            carrier="中通快递",
            tracking_no="40000000000000",
            order_no="ORDER-DT-2",
            refund_reason="主观原因不想要",
            amount=15.88,
        )
        duplicate = self.repo.begin_dingtalk_interception_sync(second_register["id"])
        self.assertTrue(duplicate["local_duplicate_tracking"])

        third_task = self.repo.upsert_task(
            WorkOrderFacts("shop_01", "测试店铺", "Profile", "WO-DT-3", "ORDER-DT-3")
        )
        third_register = self.repo.upsert_interception_register(
            task_id=third_task,
            record_date="2026-08-12",
            carrier="中通快递",
            tracking_no="40000000000000",
            order_no="ORDER-DT-3",
            refund_reason="主观原因不想要",
            amount=15.88,
            warehouse="示例供应链代发总仓",
            group_name="示例供应商 A",
            dingtalk_target_key="siku_distribution",
        )
        cross_target = self.repo.begin_dingtalk_interception_sync(
            third_register["id"]
        )
        self.assertFalse(cross_target["local_duplicate_tracking"])

    def test_route_migration_redirects_only_pending_siku_and_restores_raw_amount(self):
        pending_task = self.repo.upsert_task(
            WorkOrderFacts(
                "shop_01",
                "测试店铺",
                "Profile",
                "WO-ROUTE-PENDING",
                "ORDER-ROUTE-PENDING",
                refund_amount=23.3,
                wdt_warehouse="示例供应链代发总仓",
                source_evidence={
                    "interception_message": {"group_name": "示例供应商 A"}
                },
            )
        )
        pending = self.repo.upsert_interception_register(
            task_id=pending_task,
            record_date="2026-08-25",
            carrier="圆通",
            tracking_no="YT-PENDING",
            order_no="ORDER-ROUTE-PENDING",
            refund_reason="退款",
            amount=28.3,
        )
        succeeded_task = self.repo.upsert_task(
            WorkOrderFacts(
                "shop_01",
                "测试店铺",
                "Profile",
                "WO-ROUTE-DONE",
                "ORDER-ROUTE-DONE",
                refund_amount=30,
                wdt_warehouse="示例供应链代发总仓",
                source_evidence={
                    "interception_message": {"group_name": "旧群"}
                },
            )
        )
        succeeded = self.repo.upsert_interception_register(
            task_id=succeeded_task,
            record_date="2026-08-24",
            carrier="圆通",
            tracking_no="YT-DONE",
            order_no="ORDER-ROUTE-DONE",
            refund_reason="退款",
            amount=35,
        )
        self.repo.begin_dingtalk_interception_sync(succeeded["id"])
        self.repo.mark_dingtalk_interception_sync_success(
            succeeded["id"], remote_row=99, duplicate_tracking=False
        )
        with self.repo.connect() as conn:
            conn.execute(
                "DELETE FROM system_settings "
                "WHERE key='interception_register_warehouse_routes_v1'"
            )

        self.repo.initialize()

        rows = {
            item["id"]: item for item in self.repo.list_interception_registers()
        }
        migrated_pending = rows[pending["id"]]
        self.assertEqual(
            migrated_pending["dingtalk_target_key"], "siku_distribution"
        )
        self.assertEqual(migrated_pending["warehouse"], "示例供应链代发总仓")
        self.assertEqual(migrated_pending["group_name"], "示例供应商 A")
        self.assertEqual(migrated_pending["amount"], 23.3)
        migrated_succeeded = rows[succeeded["id"]]
        self.assertEqual(migrated_succeeded["dingtalk_target_key"], "junfu")
        self.assertEqual(migrated_succeeded["amount"], 35)

    def test_interception_register_upgrade_backfills_tracking_and_adjusts_once(self):
        legacy_path = Path(self.temp_dir.name) / "legacy_register.db"
        with sqlite3.connect(legacy_path) as conn:
            conn.executescript(
                """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY,
                    business_key TEXT,
                    shop_id TEXT,
                    order_no TEXT,
                    work_order_no TEXT,
                    state TEXT,
                    risk_level TEXT,
                    matched_rule_id TEXT,
                    decision_json TEXT,
                    facts_json TEXT,
                    version INTEGER,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE interception_registers (
                    id INTEGER PRIMARY KEY,
                    task_id INTEGER UNIQUE,
                    record_date TEXT NOT NULL,
                    carrier TEXT NOT NULL,
                    order_no TEXT NOT NULL,
                    refund_reason TEXT NOT NULL,
                    amount REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO tasks(id, facts_json) VALUES (?, ?)",
                (7, json.dumps({"tracking_no": "40000000000000"})),
            )
            conn.execute(
                """
                INSERT INTO interception_registers(
                    id, task_id, record_date, carrier, order_no, refund_reason,
                    amount, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (1, 7, "2026-08-11", "中通快递", "ORDER-7", "主观原因不想要", 9.88, "t", "t"),
            )
        conn.close()
        legacy = Repository(legacy_path)
        legacy.initialize()
        migrated = legacy.list_interception_registers()[0]
        self.assertEqual(migrated["tracking_no"], "40000000000000")
        self.assertEqual(migrated["amount"], 14.88)
        legacy.initialize()
        self.assertEqual(legacy.list_interception_registers()[0]["amount"], 14.88)
        del legacy
        gc.collect()

    def test_inflight_refund_is_moved_to_manual_review_after_restart(self):
        task_id = self.repo.upsert_task(
            WorkOrderFacts("shop_01", "娴嬭瘯搴楅摵", "Profile", "WO-EXEC", "ORDER-EXEC")
        )
        self.repo.mark_task_status(task_id, TaskState.EXECUTING, "test")
        changed = self.repo.mark_inflight_tasks_for_review()
        self.assertEqual(changed, 1)
        self.assertEqual(self.repo.get_task(task_id)["state"], TaskState.NEED_REVIEW)


if __name__ == "__main__":
    unittest.main()
