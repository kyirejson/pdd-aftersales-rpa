from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields
import re
import threading
import time
from datetime import datetime
import uuid
from typing import Any

from .config import AppConfig
from .aftersale_workbench import (
    AFTERSALE_RETURN_REFUND,
    AFTERSALE_SHIPPED_REFUND,
    AGREE_RECALL_AFTER_REFUND,
    AGREE_REFUND,
    AGREE_RETURN_AFTER_REFUND,
    REJECT_REFUND,
    decide_aftersale,
    shipped_refund_trace_limit,
)
from .aftersale_worker_client import AftersaleWorkerError, run_isolated_aftersale
from .connectors import (
    DesktopMessageConnector,
    MockJstConnector,
    MockLogisticsConnector,
    PddEdgeUiaConnector,
)
from .db import Repository
from .detail_collector import DetailCollectionResult, PddDetailCollector
from .earlier_shipping_workflow import EARLIER_SHIPPING_TITLE
from .desktop_workflow import DesktopWorkflowRunner
from .edge_launcher import EdgeShopLauncher
from .models import TaskState, WorkOrderFacts
from .components import validate_component, validate_workflow
from .in_transit_routing import (
    DEFAULT_CONFIG,
    RoutingConfigConflict,
    SETTING_KEY as IN_TRANSIT_SETTING_KEY,
    decide_interception,
    validate_config as validate_in_transit_config,
)
from .in_transit_workflows import definitions as in_transit_workflow_definitions
from .rule_engine import evaluate_rules
from .refund_workflow import RefundComponentWorkflow
from .in_transit_workflow import InTransitComponentWorkflow
from .group_dispatch import GroupDispatchService
from .safety import SafetyContext, SafetyViolation, assert_action_allowed, validate_shop_context
from .workflow_priority import (
    DEFAULT_CONFIG as WORKFLOW_PRIORITY_DEFAULT,
    SETTING_KEY as WORKFLOW_PRIORITY_SETTING_KEY,
    SHIPPED_NO_TRACE,
    prioritize_tasks,
    validate_config as validate_workflow_priority_config,
)
from .wdt_worker_client import WdtWorkerError, run_isolated_wdt_workflow
from .pdd_detail_worker_client import (
    PddDetailWorkerError,
    run_isolated_pdd_close,
    run_isolated_pdd_detail,
)
from .pdd_refund_worker_client import (
    PddRefundWorkerError,
    run_isolated_pdd_refund_stage,
)
from .edge_uia import PddAlreadyProcessedError, PddOrderNotFoundError
from .pdd_login_components import component_definitions as pdd_login_component_definitions
from .pdd_login_components import workflow_definition as pdd_login_workflow_definition
from .dingtalk_interception_worker_client import (
    DingTalkInterceptionWorkerError,
    run_isolated_dingtalk_interception_register,
)
from .dingtalk_interception_register import (
    DINGTALK_TARGET_JUNFU,
    DINGTALK_TARGET_SIKU_DISTRIBUTION,
    DingTalkInterceptionRegisterConfig,
    DingTalkInterceptionRegisterError,
    DingTalkInterceptionRegisterWriter,
    dingtalk_target_key_for_warehouse,
    iter_dingtalk_target_configs,
    resolve_dingtalk_target_config,
)
from .message_worker_client import MessageWorkerError, run_isolated_message_send
from .qianniu_edge import QianniuEdgeConfig, QianniuEdgeError, QianniuEdgeWindow
from .qianniu_aftersale import (
    AGREE_REFUND as QIANNIU_AGREE_REFUND,
    INTERCEPT_EXPRESS as QIANNIU_INTERCEPT,
    QIANNIU_UNRECEIVED_REFUND,
    decide_qianniu_unreceived_refund,
)
from .qianniu_cycle import QianniuCycleConfig
from .qianniu_routing import decide_qianniu_interception
from .qianniu_worker_client import QianniuWorkerError, run_isolated_qianniu
from .qianniu_wdt_worker_client import (
    QianniuWdtWorkerError,
    run_isolated_qianniu_wdt,
)
from .yto_service_chat import YtoServiceChatConfig, YtoServiceChatError, YtoServiceChatWriter


LIVE_SYNC_SETTING_KEY = "live_sync_snapshot"
IN_TRANSIT_NO_REASON_TITLE = "在途无理由退款处理"
# The task table is a current shop-backend work-order view, not the narrow
# refund execution queue. Only completed/terminal safety-marked work orders
# leave this view; type-specific routing remains inside RuntimeController.
NON_PENDING_STATES = {
    TaskState.COMPLETED,
    TaskState.REJECTED,
}
DISPLAY_STATE_LABELS = {
    "DISCOVERED": "待处理",
    # Internal execution phases are intentionally collapsed into the four
    # operator-facing task states.  The raw state remains in the audit log.
    "COLLECTING_DATA": "待处理",
    "WAIT_RULE_MATCH": "待处理",
    "NEED_REVIEW": "转人工",
    "READY": "待处理",
    "WAIT_APPROVAL": "转人工",
    "EXECUTING": "待处理",
    "VERIFYING": "待处理",
    "COMPLETED": "已处理",
    # REJECTED is retained internally as a terminal safety marker, but must
    # never be exposed as a user-facing “已跳过” status.
    "REJECTED": "失败",
    "FAILED": "失败",
    "PAUSED": "转人工",
    "DATA_CONFLICT": "转人工",
    "RETRY_PENDING": "等待平台冷却",
}


class Orchestrator:
    def __init__(self, config: AppConfig, repository: Repository):
        self.config = config
        self.repository = repository
        self.pdd_readonly = PddEdgeUiaConnector(
            config.raw.get("pdd_readonly", {}), config.evidence_dir
        )
        self.jst = MockJstConnector()
        self.logistics = MockLogisticsConnector()
        # Carrier interception is a real external write.  The connector stays
        # unavailable for sends until every desktop control has been captured
        # and the explicit message switch is enabled.
        self.messages = DesktopMessageConnector(repository, config.evidence_dir)
        self.edge_launcher = EdgeShopLauncher(config.raw.get("pdd_readonly", {}))
        self.detail_collector = PddDetailCollector(
            config.raw.get("pdd_readonly", {}), config.shops, config.evidence_dir
        )
        self.desktop_workflows = DesktopWorkflowRunner(repository, config.evidence_dir)
        self.refund_workflow = RefundComponentWorkflow(
            repository, self.detail_collector, config.raw.get("pdd_readonly", {})
        )
        self.in_transit_workflow = InTransitComponentWorkflow(
            repository,
            config.raw.get("pdd_readonly", {}),
            browser=self.refund_workflow.browser,
        )
        self.group_dispatch = GroupDispatchService(repository)
        self._in_transit_config_lock = threading.RLock()
        self._dingtalk_interception_sync_lock = threading.RLock()

    def _qianniu_raw_config(self) -> dict[str, Any]:
        return dict(self.config.raw.get("qianniu_shop_browser") or {})

    def qianniu_shop_summary(self) -> dict[str, Any]:
        """Console-facing summary of the staged twelfth Tmall shop."""
        raw = self._qianniu_raw_config()
        cycle = self._qianniu_cycle_config()
        return {
            "id": str(raw.get("id") or "qianniu_01"),
            "name": str(raw.get("shop_name") or "示例天猫旗舰店"),
            "edge_profile": str(raw.get("edge_profile") or "Default"),
            "platform": "tmall",
            "enabled": bool(raw.get("enabled")),
            "workflow_enabled": cycle.workflow_enabled,
            "production_enabled": cycle.production_enabled,
            "scan_position": cycle.scan_position,
        }

    def _qianniu_cycle_config(self) -> QianniuCycleConfig:
        config = QianniuCycleConfig.from_raw(self._qianniu_raw_config())
        if config.workflow_enabled or config.production_enabled:
            config.validate(len([shop for shop in self.config.shops if shop.enabled]))
        return config

    def _run_qianniu_worker(
        self,
        *,
        operation: str,
        order_no: str = "",
        aftersale_no: str = "",
        live: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        runtime = dict(self.config.raw.get("runtime") or {})
        return run_isolated_qianniu(
            project_root=self.config.project_root,
            evidence_dir=self.config.evidence_dir,
            config=self._qianniu_raw_config(),
            operation=operation,
            order_no=order_no,
            aftersale_no=aftersale_no,
            live=live,
            timeout_seconds=float(runtime.get("qianniu_worker_timeout_seconds", 180)),
            cancel_event=cancel_event,
        )

    def initialize(self) -> None:
        self.repository.initialize()
        self.repository.purge_demo_tasks()
        self.repository.mark_refunded_tasks_completed()
        self.repository.mark_inflight_tasks_for_review()
        self.repository.seed_shops(self.config.shops)
        if not self.repository.get_json_setting(IN_TRANSIT_SETTING_KEY):
            self.repository.set_json_setting(IN_TRANSIT_SETTING_KEY, DEFAULT_CONFIG)
        self.messages.ensure_config()
        self.messages.sync_yto_settings(self.config.raw.get("yto_service_chat"))
        self._upgrade_in_transit_routing_defaults()
        if not self.repository.get_json_setting(WORKFLOW_PRIORITY_SETTING_KEY):
            self.repository.set_json_setting(WORKFLOW_PRIORITY_SETTING_KEY, WORKFLOW_PRIORITY_DEFAULT)
        for workflow in in_transit_workflow_definitions():
            if not self.repository.get_workflow(workflow["workflow_id"]):
                self.repository.save_workflow(workflow)
        # Keep the login recovery blocks visible in the designer and versioned
        # in the same repository as the other fixed UIA components.  This is
        # metadata only; no login click is performed during initialization.
        for definition in pdd_login_component_definitions():
            if not self.repository.get_component(definition["component_id"]):
                self.repository.save_component(validate_component(definition))
        self._ensure_in_transit_wdt_assert_component()
        login_workflow = pdd_login_workflow_definition()
        if not self.repository.get_workflow(login_workflow["workflow_id"]):
            self.repository.save_workflow(validate_workflow(login_workflow))
        self._upgrade_wdt_unique_result_guard()
        self._upgrade_wdt_waybill_input_component()
        self._upgrade_wdt_search_component()
        self._upgrade_wdt_wait_result_component()
        self._upgrade_wdt_result_count_component()
        self._upgrade_wdt_logistics_company_component()
        self._ensure_wdt_warehouse_component()
        self._ensure_wdt_goods_list_tab_component()
        self._ensure_wdt_product_names_component()
        self._upgrade_wdt_shipped_interception_branch()
        self._upgrade_wdt_in_transit_workflow()

    def _upgrade_in_transit_routing_defaults(self) -> None:
        """Migrate the pre-routing empty mapping to the configured channels."""
        current = self.repository.get_json_setting(IN_TRANSIT_SETTING_KEY, {})
        if not current:
            self.repository.set_json_setting(IN_TRANSIT_SETTING_KEY, DEFAULT_CONFIG)
            return
        revision = int(current.get("version", 1) or 1)
        # Older revisions used one field for both schema migrations and every
        # operator save.  Cap that legacy value at v12 so a frequently edited
        # v12 config cannot accidentally skip the v13 per-route-warehouse
        # migration merely because its display revision is already higher.
        version = int(current.get("schema_version") or min(revision, 12))
        current_platforms = set(current.get("supported_platforms") or [])
        required_platforms = set(DEFAULT_CONFIG.get("supported_platforms") or [])
        default_version = int(DEFAULT_CONFIG.get("schema_version", 13))
        product_routes_have_warehouses = all(
            isinstance(item, dict) and str(item.get("warehouse") or "").strip()
            for item in (current.get("product_groups") or [])
        )
        if (
            version >= default_version
            and required_platforms.issubset(current_platforms)
            and product_routes_have_warehouses
        ):
            return
        seed = dict(current)
        seed["supported_platforms"] = sorted(current_platforms | required_platforms)
        migrated = validate_in_transit_config(seed)
        for key in ("carrier_groups", "location_whitelists"):
            if not current.get(key):
                migrated[key] = DEFAULT_CONFIG[key]
        # Version 3 changes the refund-location rule from a loose “contains
        # 无锡” test to an exact PDD bracket-city whitelist.  Only replace the
        # old built-in single-city default; preserve an operator's explicit
        # custom whitelist.
        if version < 3:
            old_locations = current.get("location_whitelists") or []
            if old_locations and all(
                set(str(item).strip() for item in (entry.get("keywords") or [])) <= {"无锡", "示例城市甲市"}
                for entry in old_locations
            ):
                migrated["location_whitelists"] = DEFAULT_CONFIG["location_whitelists"]
        if version < 4:
            old_yto_names = {"\u5706\u901a\u4e13\u5c5e\u5ba2\u670d", "\u4e13\u5c5e\u5ba2\u670d"}
            for mapping in migrated.get("carrier_groups") or []:
                if (
                    str(mapping.get("platform") or "").strip().lower() == "yto"
                    and str(mapping.get("group_name") or "").strip() in old_yto_names
                ):
                    mapping["group_name"] = DEFAULT_CONFIG["carrier_groups"][0]["group_name"]
        if version < 5:
            # Version 5 applies the user-confirmed rule to the stock city
            # whitelist: a bracketed 【示例城市 A】/【示例城市 B】 is sufficient; do not
            # require the wording “快件已发往”.  Only alter the untouched
            # built-in mapping and retain intentionally customised rules.
            default_keywords = {"示例城市甲市", "示例城市乙市"}
            for entry in migrated.get("location_whitelists") or []:
                keywords = {str(item).strip() for item in (entry.get("keywords") or [])}
                if (
                    keywords == default_keywords
                    and str(entry.get("required_phrase") or "").strip() == "快件已发往"
                ):
                    entry.pop("required_phrase", None)
        if version < 7:
            warehouses = [
                re.sub(r"\s+", "", str(item or ""))
                for item in (current.get("supported_warehouses") or [])
            ]
            for warehouse in DEFAULT_CONFIG.get("supported_warehouses") or []:
                normalized = re.sub(r"\s+", "", str(warehouse or ""))
                if normalized and normalized not in warehouses:
                    warehouses.append(normalized)
            migrated["supported_warehouses"] = warehouses
            if not current.get("product_groups"):
                migrated["product_groups"] = deepcopy(DEFAULT_CONFIG["product_groups"])
            migrated["jst_product_warehouse"] = str(
                current.get("jst_product_warehouse")
                or DEFAULT_CONFIG["jst_product_warehouse"]
            )
        if version < 8:
            # Add the newly configured 示例供应链 product route without
            # overwriting existing operator mappings.  Product mappings are
            # matched by keyword + group so a previous custom group remains
            # untouched while the new default is appended once.
            existing_product_keys = {
                (
                    str(item.get("keyword") or "").strip(),
                    str(item.get("group_name") or "").strip(),
                )
                for item in (migrated.get("product_groups") or [])
                if isinstance(item, dict)
            }
            for default_mapping in DEFAULT_CONFIG.get("product_groups") or []:
                key = (
                    str(default_mapping.get("keyword") or "").strip(),
                    str(default_mapping.get("group_name") or "").strip(),
                )
                if key[0] and key not in existing_product_keys:
                    migrated.setdefault("product_groups", []).append(deepcopy(default_mapping))
                    existing_product_keys.add(key)
        if version < 9:
            # Keep the existing 示例供应链 product-routing category and append
            # only the operator-confirmed 示例品类 F QQ group mapping.
            existing_product_keys = {
                (
                    str(item.get("keyword") or "").strip(),
                    str(item.get("group_name") or "").strip(),
                )
                for item in (migrated.get("product_groups") or [])
                if isinstance(item, dict)
            }
            for default_mapping in DEFAULT_CONFIG.get("product_groups") or []:
                key = (
                    str(default_mapping.get("keyword") or "").strip(),
                    str(default_mapping.get("group_name") or "").strip(),
                )
                if key[0] and key not in existing_product_keys:
                    migrated.setdefault("product_groups", []).append(deepcopy(default_mapping))
                    existing_product_keys.add(key)
        if version < 10:
            # Add the operator-confirmed 示例品类 G route to the existing
            # 示例供应链 product category without replacing custom mappings.
            existing_product_keys = {
                (
                    str(item.get("keyword") or "").strip(),
                    str(item.get("group_name") or "").strip(),
                )
                for item in (migrated.get("product_groups") or [])
                if isinstance(item, dict)
            }
            for default_mapping in DEFAULT_CONFIG.get("product_groups") or []:
                key = (
                    str(default_mapping.get("keyword") or "").strip(),
                    str(default_mapping.get("group_name") or "").strip(),
                )
                if key[0] and key not in existing_product_keys:
                    migrated.setdefault("product_groups", []).append(deepcopy(default_mapping))
                    existing_product_keys.add(key)
        if version < 11:
            # The operator corrected the 示例品类 G destination after the first
            # migration.  Replace every older mapping for this exact keyword
            # with one canonical target so routing cannot become ambiguous.
            qiwenxuan_default = next(
                (
                    deepcopy(item)
                    for item in DEFAULT_CONFIG.get("product_groups") or []
                    if str(item.get("keyword") or "").strip() == "示例品类 G"
                ),
                None,
            )
            if qiwenxuan_default is not None:
                retained = [
                    item
                    for item in (migrated.get("product_groups") or [])
                    if str(item.get("keyword") or "").strip() != "示例品类 G"
                ]
                retained.append(qiwenxuan_default)
                migrated["product_groups"] = retained
        if version < 12:
            # Append the operator-confirmed 示例品类 H route once while preserving
            # the independently configured 示例品类 G destination.
            existing_product_keys = {
                (
                    str(item.get("keyword") or "").strip(),
                    str(item.get("group_name") or "").strip(),
                )
                for item in (migrated.get("product_groups") or [])
                if isinstance(item, dict)
            }
            for default_mapping in DEFAULT_CONFIG.get("product_groups") or []:
                key = (
                    str(default_mapping.get("keyword") or "").strip(),
                    str(default_mapping.get("group_name") or "").strip(),
                )
                if key[0] == "示例品类 H" and key not in existing_product_keys:
                    migrated.setdefault("product_groups", []).append(
                        deepcopy(default_mapping)
                    )
                    existing_product_keys.add(key)
        migrated["supported_platforms"] = sorted(current_platforms | required_platforms)
        fixed_reject = dict(current.get("fixed_reject") or {})
        if not fixed_reject.get("option_text"):
            fixed_reject["option_text"] = DEFAULT_CONFIG["fixed_reject"]["option_text"]
        migrated["fixed_reject"] = fixed_reject
        migrated["refund_reason_option_text"] = str(
            current.get("refund_reason_option_text") or DEFAULT_CONFIG["refund_reason_option_text"]
        )
        migrated["schema_version"] = default_version
        migrated["version"] = max(revision, default_version)
        self.repository.set_json_setting(IN_TRANSIT_SETTING_KEY, migrated)

    def _upgrade_wdt_in_transit_workflow(self) -> None:
        """Add the same unique-result guard used by shipped-no-trace lookup."""
        workflow = self.repository.get_workflow("WF-WDT-IN-TRANSIT-LOOKUP")
        if not workflow:
            return
        # The query input and result are deliberately separate.  A unique
        # row must return the same waybill before its other fields can be
        # trusted for routing.
        nodes = [
            {"node_id": "activate", "component_id": "CMP-WDT-ACTIVATE", "component_type": "ActivateDesktopWindow", "name": "激活旺店通"},
            {"node_id": "management", "component_id": "CMP-WDT_ORDER_MANAGEMENT", "component_type": "ClickDesktopElement", "name": "打开订单管理"},
            {"node_id": "input", "component_id": "CMP-WDT_WAYBILL_INPUT", "component_type": "InputDesktopText", "name": "输入物流单号"},
            {"node_id": "search", "component_id": "CMP-WDT_SEARCH", "component_type": "ClickDesktopElement", "name": "搜索"},
            {"node_id": "wait", "component_id": "CMP-WDT-WAIT-RESULT", "component_type": "WaitDesktopState", "name": "等待查询结果"},
            {"node_id": "result_count", "component_id": "CMP-WDT-RESULT-COUNT", "component_type": "ReadDesktopGridRow", "name": "识别查询结果行数"},
            {"node_id": "waybill", "component_id": "CMP-WDT_WAYBILL", "component_type": "ReadDesktopText", "name": "回读结果物流单号"},
            {"node_id": "order", "component_id": "CMP-WDT_ORDER_ID", "component_type": "ReadDesktopText", "name": "读取旺店通订单"},
            {"node_id": "platform", "component_id": "CMP-WDT_PLATFORM", "component_type": "ReadDesktopText", "name": "读取平台"},
            {"node_id": "original", "component_id": "CMP-WDT_ORIGINAL_ORDER", "component_type": "ReadDesktopText", "name": "读取原始订单"},
            {"node_id": "warehouse", "component_id": "CMP-WDT-WAREHOUSE", "component_type": "ReadDesktopText", "name": "读取旺店通仓库"},
            {"node_id": "company", "component_id": "CMP-WDT-LOGISTICS-COMPANY", "component_type": "ReadDesktopText", "name": "读取物流公司"},
            {
                "node_id": "goods_tab",
                "component_id": "CMP-WDT-GOODS-LIST-TAB",
                "component_type": "ClickDesktopElement",
                "name": "打开货品列表",
                "when": {"wdt_warehouse": "示例供应链代发总仓"},
            },
            {
                "node_id": "product_names",
                "component_id": "CMP-WDT-PRODUCT-NAMES",
                "component_type": "ReadDesktopText",
                "name": "读取货品名称",
                "when": {"wdt_warehouse": "示例供应链代发总仓"},
            },
            {"node_id": "assert", "component_id": "CMP-WDT-ASSERT-IN-TRANSIT", "component_type": "AssertDesktopQueryResult", "name": "校验唯一订单及平台"},
        ]
        new_ids = [node["node_id"] for node in nodes]
        if [node.get("node_id") for node in (workflow.get("nodes") or [])] == new_ids and int(workflow.get("version", 1)) >= 7:
            return
        workflow["nodes"] = nodes
        workflow["edges"] = [
            {"from": new_ids[i], "to": new_ids[i + 1]}
            for i in range(len(new_ids) - 1)
        ]
        workflow["description"] = "输入物流单号，查询唯一结果后从结果行回读并核对物流单号、原始订单、平台、仓库和物流公司；示例供应链代发总仓额外读取货品名称后路由。"
        workflow["version"] = max(int(workflow.get("version", 1)), 7)
        self.repository.save_workflow(validate_workflow(workflow))

    def _ensure_in_transit_wdt_assert_component(self) -> None:
        component = self.repository.get_component("CMP-WDT-ASSERT-IN-TRANSIT")
        if not component:
            base = self.repository.get_component("CMP-WDT-ASSERT-REFUNDABLE")
            if not base:
                return
            component = dict(base)
            component["component_id"] = "CMP-WDT-ASSERT-IN-TRANSIT"
            component["name"] = "校验唯一订单及平台"
        config = dict(component.get("config") or {})
        if (
            config.get("skip_status_check") is True
            and config.get("allow_exact_tracking_fallback") is True
            and config.get("expected_platform") == "拼多多"
            and config.get("require_original_order_match") is True
        ):
            return
        config.update({
            # 在途分支需要以平台和物流公司决定拦截路由，必须保留该校验。
            "expected_platform": "拼多多",
            "skip_status_check": True,
            "allow_exact_tracking_fallback": True,
            "require_original_order_match": True,
        })
        component["config"] = config
        component["version"] = max(int(component.get("version", 1)), 3)
        self.repository.save_component(validate_component(component))

    def _upgrade_wdt_unique_result_guard(self) -> None:
        workflow = self.repository.get_workflow("WF-WDT-SHIPPED-NO-TRACE-CHECK")
        result_count = self.repository.get_component("CMP-WDT-RESULT-COUNT")
        if not workflow or not result_count:
            return
        # The production decision only needs a unique row, the queried waybill
        # and the order status.  Reading order id/platform/original order and
        # counting the same painted row twice added UIA work without changing
        # the decision.  The platform requirement must be removed together
        # with its reader; leaving it on the assertion makes every successful
        # query fail with ``平台与预期不一致：空``.
        assertion = self.repository.get_component("CMP-WDT-ASSERT-REFUNDABLE")
        if assertion:
            assertion_config = dict(assertion.get("config") or {})
            if "expected_platform" in assertion_config:
                assertion_config.pop("expected_platform", None)
                assertion["config"] = assertion_config
                assertion["version"] = max(int(assertion.get("version", 1) or 1), 4)
                self.repository.save_component(validate_component(assertion))
        removable_components = {
            "CMP-WDT_ORDER_ID",
            "CMP-WDT_PLATFORM",
            "CMP-WDT_ORIGINAL_ORDER",
        }
        source_nodes = list(workflow.get("nodes") or [])
        original_ids = [node.get("node_id") for node in source_nodes]
        nodes = [
            node
            for node in source_nodes
            if node.get("component_id") not in removable_components
            and node.get("node_id") != "wdt_result_count_confirm"
        ]
        nodes = [node for node in nodes if node.get("component_id") != "CMP-WDT_SECOND_ORDER"]
        # Retain exactly one result-count read before the row fields.
        count_seen = False
        unique_nodes = []
        for node in nodes:
            if node.get("component_id") == "CMP-WDT-RESULT-COUNT":
                if count_seen:
                    continue
                count_seen = True
            unique_nodes.append(node)
        nodes = unique_nodes
        assert_index = next(
            (index for index, node in enumerate(nodes) if node.get("component_id") == "CMP-WDT-ASSERT-REFUNDABLE"),
            len(nodes),
        )
        if not count_seen:
            wait_index = next(
                (index for index, node in enumerate(nodes) if node.get("component_id") == "CMP-WDT-WAIT-RESULT"),
                -1,
            )
            nodes.insert(
                wait_index + 1 if wait_index >= 0 else assert_index,
                {
                    "node_id": "wdt_result_count",
                    "component_id": "CMP-WDT-RESULT-COUNT",
                    "component_type": "ReadDesktopGridRow",
                    "name": "识别查询结果行数",
                },
            )
        new_ids = [node.get("node_id") for node in nodes]
        if original_ids == new_ids and int(workflow.get("version", 1)) >= 8:
            return
        workflow["nodes"] = nodes
        workflow["edges"] = [
            {"from": nodes[index]["node_id"], "to": nodes[index + 1]["node_id"]}
            for index in range(len(nodes) - 1)
        ]
        workflow["description"] = "输入并回读物流单号，查询唯一结果后按订单状态判断是否可退款"
        workflow["version"] = max(int(workflow.get("version", 1)), 8)
        self.repository.save_workflow(validate_workflow(workflow))

    def _upgrade_wdt_shipped_interception_branch(self) -> None:
        """Read routing fields only when a shipped-no-trace order is 已发货."""
        workflow = self.repository.get_workflow("WF-WDT-SHIPPED-NO-TRACE-CHECK")
        assertion = self.repository.get_component("CMP-WDT-ASSERT-REFUNDABLE")
        if not workflow or not assertion:
            return
        assertion_config = dict(assertion.get("config") or {})
        assertion_config["interception_statuses"] = ["已发货"]
        assertion["config"] = assertion_config
        assertion["version"] = max(int(assertion.get("version", 1) or 1), 5)
        self.repository.save_component(validate_component(assertion))

        nodes = [
            {"node_id": "activate", "component_id": "CMP-WDT-ACTIVATE", "component_type": "ActivateDesktopWindow", "name": "激活旺店通"},
            {"node_id": "management", "component_id": "CMP-WDT_ORDER_MANAGEMENT", "component_type": "ClickDesktopElement", "name": "打开订单管理"},
            {"node_id": "input", "component_id": "CMP-WDT_WAYBILL_INPUT", "component_type": "InputDesktopText", "name": "输入物流单号"},
            {"node_id": "search", "component_id": "CMP-WDT_SEARCH", "component_type": "ClickDesktopElement", "name": "搜索"},
            {"node_id": "wait", "component_id": "CMP-WDT-WAIT-RESULT", "component_type": "WaitDesktopState", "name": "等待查询结果"},
            {"node_id": "result_count", "component_id": "CMP-WDT-RESULT-COUNT", "component_type": "ReadDesktopGridRow", "name": "识别查询结果行数"},
            {"node_id": "waybill", "component_id": "CMP-WDT_WAYBILL", "component_type": "ReadDesktopText", "name": "回读结果物流单号"},
            {"node_id": "status", "component_id": "CMP-WDT_ORDER_STATUS", "component_type": "ReadDesktopText", "name": "读取订单状态"},
            {"node_id": "warehouse", "component_id": "CMP-WDT-WAREHOUSE", "component_type": "ReadDesktopText", "name": "已发货时读取仓库", "when": {"wdt_order_status": "已发货"}},
            {"node_id": "company", "component_id": "CMP-WDT-LOGISTICS-COMPANY", "component_type": "ReadDesktopText", "name": "已发货时读取物流公司", "when": {"wdt_order_status": "已发货"}},
            {"node_id": "goods_tab", "component_id": "CMP-WDT-GOODS-LIST-TAB", "component_type": "ClickDesktopElement", "name": "聚水潭打开货品列表", "when": {"wdt_warehouse": "示例供应链代发总仓"}},
            {"node_id": "product_names", "component_id": "CMP-WDT-PRODUCT-NAMES", "component_type": "ReadDesktopText", "name": "聚水潭读取货品名称", "when": {"wdt_warehouse": "示例供应链代发总仓"}},
            {"node_id": "assert", "component_id": "CMP-WDT-ASSERT-REFUNDABLE", "component_type": "AssertDesktopQueryResult", "name": "校验唯一订单及退款或拦截状态"},
        ]
        node_ids = [node["node_id"] for node in nodes]
        if (
            [node.get("node_id") for node in workflow.get("nodes") or []] == node_ids
            and int(workflow.get("version", 1) or 1) >= 10
        ):
            return
        workflow["nodes"] = nodes
        workflow["edges"] = [
            {"from": node_ids[index], "to": node_ids[index + 1]}
            for index in range(len(node_ids) - 1)
        ]
        workflow["description"] = (
            "物流单号查询唯一结果；可退款状态直接退款，已发货时读取仓库、物流公司，"
            "示例供应链再读取货品名称后进入拦截分支。"
        )
        workflow["version"] = 10
        self.repository.save_workflow(validate_workflow(workflow))

    def _upgrade_wdt_search_component(self) -> None:
        """Bind WDT search to a semantic query control, never a clear point."""
        component = self.repository.get_component("CMP-WDT_SEARCH")
        if not component:
            return
        locator = dict(component.get("locator") or {})
        semantic = dict(locator.get("semantic") or {})
        config = dict(component.get("config") or {})
        changed = False
        if semantic.get("names") != ["搜索", "查询", "检索", "执行查询", "搜索订单"]:
            semantic["names"] = ["搜索", "查询", "检索", "执行查询", "搜索订单"]
            changed = True
        if semantic.get("control_types") != ["Button", "SplitButton", "Hyperlink", "Text", "Pane", "Custom"]:
            semantic["control_types"] = ["Button", "SplitButton", "Hyperlink", "Text", "Pane", "Custom"]
            changed = True
        if locator.get("semantic") != semantic:
            locator["semantic"] = semantic
            changed = True
        try:
            after_wait = float(config.get("after_wait_seconds", 0.5))
        except (TypeError, ValueError):
            after_wait = 0.5
        if after_wait < 1.0:
            config["after_wait_seconds"] = 1.0
            changed = True
        if config.get("wait_for_window_ready") is not True:
            config["wait_for_window_ready"] = True
            changed = True
        if config.get("response_timeout_seconds") != 5:
            config["response_timeout_seconds"] = 5
            changed = True
        if config.get("click_strategy") != "win32_point_no_clipboard":
            config["click_strategy"] = "win32_point_no_clipboard"
            changed = True
        if changed:
            component["locator"] = locator
            component["config"] = config
            self.repository.save_component(validate_component(component))

    def _upgrade_wdt_waybill_input_component(self) -> None:
        """Make the captured WDT waybill field explicitly clipboard-free."""
        component = self.repository.get_component("CMP-WDT_WAYBILL_INPUT")
        if not component:
            return
        config = dict(component.get("config") or {})
        desired = {
            "input_strategy": "direct_keys_no_clipboard",
            "focus_wait_seconds": 0.08,
            "key_interval_seconds": 0.012,
            "after_wait_seconds": 0.10,
        }
        changed = False
        for key, value in desired.items():
            if config.get(key) != value:
                config[key] = value
                changed = True
        if changed:
            component["config"] = config
            component["version"] = max(int(component.get("version", 1) or 1), 3)
            self.repository.save_component(validate_component(component))

    def _upgrade_wdt_wait_result_component(self) -> None:
        """Require the freshly queried waybill before downstream WDT reads."""

        component = self.repository.get_component("CMP-WDT-WAIT-RESULT")
        if not component:
            return
        config = dict(component.get("config") or {})
        desired = {
            "seconds": 12,
            "poll_for_grid": True,
            "require_exact_tracking_sync": True,
            "minimum_wait_seconds": 0.5,
            "max_wait_seconds": 12,
            "poll_interval_seconds": 0.35,
            "tracking_cell_relative_point": {
                "x": 1850,
                "y": 307,
                "x_ratio": 1850 / 1920,
                "y_ratio": 307 / 1040,
            },
            "tracking_scan_span_px": 24,
            "tracking_scan_step_px": 8,
            "tracking_cell_click_wait_seconds": 0.05,
            "tracking_cell_copy_wait_seconds": 0.20,
        }
        if all(config.get(key) == value for key, value in desired.items()):
            return
        config.update(desired)
        component["config"] = config
        component["version"] = max(int(component.get("version", 1) or 1), 3)
        self.repository.save_component(validate_component(component))

    def _upgrade_wdt_result_count_component(self) -> None:
        """Recognise both selected and white WDT result rows."""
        component = self.repository.get_component("CMP-WDT-RESULT-COUNT")
        if not component:
            return
        config = dict(component.get("config") or {})
        desired = {
            "row_probe_offsets": [24, 28],
            "min_text_ratio": 0.006,
        }
        changed = False
        for key, value in desired.items():
            if config.get(key) != value:
                config[key] = value
                changed = True
        if changed:
            component["config"] = config
            component["version"] = max(int(component.get("version", 1) or 1), 2)
            self.repository.save_component(validate_component(component))

    def _upgrade_wdt_logistics_company_component(self) -> None:
        """Keep the carrier reader on the verified current WDT column.

        A real unique-result audit on 2026-08-22 placed the carrier centre at
        x=1710, y=307 on the 1920x1040 reference layout.  Persisting this in
        the initializer prevents an old migration rule from restoring x=1800
        after every console restart.
        """
        component = self.repository.get_component("CMP-WDT-LOGISTICS-COMPANY")
        if not component:
            return
        locator = dict(component.get("locator") or {})
        point = dict(locator.get("relative_point") or {})
        config = dict(component.get("config") or {})
        changed = False
        desired_point = {
            "x": 1710,
            "y": 307,
            "x_ratio": 1710 / 1920,
            "y_ratio": 307 / 1040,
        }
        if any(point.get(key) != value for key, value in desired_point.items()):
            point.update(desired_point)
            locator["relative_point"] = point
            changed = True
        desired = {
            "preferred_offset_px": 0,
            "scan_span_px": 24,
            "scan_step_px": 8,
            "cell_click_wait_seconds": 0.03,
            "cell_copy_wait_seconds": 0.05,
        }
        for key, value in desired.items():
            if config.get(key) != value:
                config[key] = value
                changed = True
        if changed:
            component["locator"] = locator
            component["config"] = config
            self.repository.save_component(validate_component(component))

    def _ensure_wdt_warehouse_component(self) -> None:
        """Create a dedicated WDT warehouse reader beside, not in, carrier routing."""
        if self.repository.get_component("CMP-WDT-WAREHOUSE"):
            # A subsequently recaptured warehouse component belongs to the
            # operator.  Never replace it with the bootstrap coordinates.
            return
        source = self.repository.get_component("CMP-WDT-LOGISTICS-COMPANY")
        if not source:
            return
        component = deepcopy(source)
        component["component_id"] = "CMP-WDT-WAREHOUSE"
        component["name"] = "读取旺店通仓库"
        locator = dict(component.get("locator") or {})
        locator["relative_point"] = {
            "x": 1575,
            "y": 307,
            "x_ratio": 1575 / 1920,
            "y_ratio": 307 / 1040,
        }
        component["locator"] = locator
        config = dict(component.get("config") or {})
        config.update(
            {
                "output_variable": "wdt_warehouse",
                "preferred_offset_px": 0,
                "scan_span_px": 24,
                "scan_step_px": 8,
                "cell_click_wait_seconds": 0.03,
                "cell_copy_wait_seconds": 0.05,
            }
        )
        component["config"] = config
        component["version"] = max(int(component.get("version", 1)), 1)
        self.repository.save_component(validate_component(component))

    def _ensure_wdt_goods_list_tab_component(self) -> None:
        if self.repository.get_component("CMP-WDT-GOODS-LIST-TAB"):
            return
        source = self.repository.get_component("CMP-WDT-WAREHOUSE")
        component = deepcopy(source) if source else {
            "application": "旺店通ERP旗舰版",
            "locator": {
                "window": {
                    "title": "旺店通ERP旗舰版",
                    "class_name": "CoolWindow",
                    "reference_size": {"width": 1920, "height": 1040},
                },
                "class_name": "CoolWindow",
                "resolution_fallback": True,
            },
        }
        component.update(
            {
                "component_id": "CMP-WDT-GOODS-LIST-TAB",
                "component_type": "ClickDesktopElement",
                "name": "打开旺店通货品列表",
            }
        )
        locator = dict(component.get("locator") or {})
        locator["relative_point"] = {
            "x": 50,
            "y": 812,
            "x_ratio": 50 / 1920,
            "y_ratio": 812 / 1040,
        }
        locator["semantic"] = {
            "names": ["货品列表"],
            "control_types": ["Button", "TabItem", "Text", "Pane", "Custom"],
        }
        component["locator"] = locator
        component["config"] = {
            "semantic_role": "wdt_goods_list_tab",
            "after_wait_seconds": 0.15,
        }
        component["version"] = 1
        self.repository.save_component(validate_component(component))

    def _ensure_wdt_product_names_component(self) -> None:
        existing = self.repository.get_component("CMP-WDT-PRODUCT-NAMES")
        if existing:
            existing_config = dict(existing.get("config") or {})
            keywords = [
                str(item).strip()
                for item in (existing_config.get("product_keywords") or [])
                if str(item).strip()
            ]
            changed = False
            for keyword in (
                "示例品类 A",
                "示例供应商 B",
                "示例品类 C",
                "示例品类 D",
                "示例品类 E",
                "示例品类 F",
                "示例品类 G",
                "示例品类 H",
            ):
                if keyword not in keywords:
                    keywords.append(keyword)
                    changed = True
            if changed:
                existing_config["product_keywords"] = keywords
                existing["config"] = existing_config
                self.repository.save_component(validate_component(existing))
            return
        source = self.repository.get_component("CMP-WDT-WAREHOUSE")
        component = deepcopy(source) if source else {
            "application": "旺店通ERP旗舰版",
            "locator": {
                "window": {
                    "title": "旺店通ERP旗舰版",
                    "class_name": "CoolWindow",
                    "reference_size": {"width": 1920, "height": 1040},
                },
                "class_name": "CoolWindow",
                "resolution_fallback": True,
            },
        }
        component.update(
            {
                "component_id": "CMP-WDT-PRODUCT-NAMES",
                "component_type": "ReadDesktopText",
                "name": "读取旺店通货品名称",
            }
        )
        locator = dict(component.get("locator") or {})
        locator["relative_point"] = {
            "x": 340,
            "y": 868,
            "x_ratio": 340 / 1920,
            "y_ratio": 868 / 1040,
        }
        component["locator"] = locator
        component["config"] = {
            "semantic_role": "wdt_product_names",
            "output_variable": "wdt_product_names",
            "product_keywords": [
                "示例品类 A",
                "示例供应商 B",
                "示例品类 C",
                "示例品类 D",
                "示例品类 E",
                "示例品类 F",
                "示例品类 G",
                "示例品类 H",
            ],
            "first_row_offset_ratio": 0.055,
            "row_step_ratio": 26 / 1040,
            "max_visible_rows": 12,
            "blank_row_limit": 2,
            "cell_click_wait_seconds": 0.04,
            "cell_copy_wait_seconds": 0.06,
        }
        component["version"] = 1
        self.repository.save_component(validate_component(component))

    def get_in_transit_config(self) -> dict[str, Any]:
        return validate_in_transit_config(
            self.repository.get_json_setting(IN_TRANSIT_SETTING_KEY, DEFAULT_CONFIG)
        )

    def get_message_dispatch_config(self) -> dict[str, Any]:
        return self.messages.get_config()

    def update_message_dispatch_config(self, definition: dict[str, Any]) -> dict[str, Any]:
        return self.messages.update_config(definition)

    def in_transit_dispatch_readiness(self) -> dict[str, Any]:
        """Report whether every configured in-transit channel can really write.

        The four current direct-carrier routes are all enabled.  Production
        is deliberately blocked until each of their desktop send components
        has been captured and verified, so a partially configured chat client
        can never silently fall back to a guessed coordinate.
        """
        routing = self.get_in_transit_config()
        required_platforms = {
            str(item.get("platform") or "").strip().lower()
            for item in routing.get("carrier_groups") or []
            if item.get("enabled", True)
        }
        required_platforms.update(
            str(item.get("platform") or "").strip().lower()
            for item in routing.get("product_groups") or []
            if item.get("enabled", True)
        )
        required_platforms.update(
            str(item.get("platform") or "").strip().lower()
            for item in routing.get("supplier_groups") or []
            if item.get("enabled", True)
        )
        health = self.messages.health().data
        platforms = dict(health.get("platforms") or {})
        reasons: list[str] = []
        for platform in sorted(required_platforms):
            result = dict(platforms.get(platform) or {})
            if not result.get("ready"):
                detail = "；".join(str(item) for item in result.get("reasons") or [])
                reasons.append(f"{platform} 消息组件未就绪{('：' + detail) if detail else ''}")
        return {
            "ready": not reasons,
            "required_platforms": sorted(required_platforms),
            "platforms": platforms,
            "reasons": reasons,
        }

    def preflight_message_group(self, platform: str, group_name: str) -> dict[str, Any]:
        """Validate a captured chat group's identity without sending a message."""

        result = self.messages.preflight_group(platform, group_name)
        if not result.ok:
            raise RuntimeError(result.error or "快递群只读预检失败")
        return {**result.data, "evidence": result.evidence}

    def update_in_transit_config(
        self,
        definition: dict[str, Any],
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        with self._in_transit_config_lock:
            current = self.get_in_transit_config()
            current_version = int(current.get("version", 1))
            if expected_version is not None and int(expected_version) != current_version:
                raise RoutingConfigConflict(
                    f"路由配置已从版本 {expected_version} 更新到 {current_version}，"
                    "本次未覆盖，请重新读取后再保存"
                )
            validated = validate_in_transit_config(definition)
            validated["version"] = current_version + 1
            self.repository.set_json_setting(IN_TRANSIT_SETTING_KEY, validated)
            return validated

    def preview_in_transit_routing(self, inputs: dict[str, Any]) -> dict[str, Any]:
        result = decide_interception(inputs, self.get_in_transit_config())
        if result.get("ok"):
            result["dispatch"] = self.group_dispatch.prepare(
                result["group"], result["tracking_no"], result["message"]
            )
        return result

    def _persist_interception_dispatch_evidence(
        self,
        task_id: int,
        group: dict[str, Any],
        tracking_no: str,
        message: str,
        evidence: dict[str, Any],
    ) -> None:
        """Hydrate task facts/evidence from one already-audited group send."""

        sent_echo = str(evidence.get("sent_echo") or "").strip()
        evidence_paths = [
            str(item).strip()
            for item in evidence.get("evidence") or []
            if str(item).strip()
        ]
        if not sent_echo or not evidence_paths:
            raise RuntimeError("拦截消息缺少发送回读或截图，禁止作为已验证发送复用")
        current_task = self.repository.get_task(task_id)
        if not current_task:
            raise KeyError(f"任务不存在：{task_id}")
        current_facts = dict(current_task.get("facts") or {})
        current_facts["screenshots"] = list(
            dict.fromkeys(
                [*list(current_facts.get("screenshots") or []), *evidence_paths]
            )
        )
        current_facts.setdefault("source_evidence", {})["interception_message"] = {
            "platform": str(group.get("platform") or "").strip(),
            "account_alias": str(group.get("account_alias") or "").strip(),
            "group_name": str(group.get("group_name") or "").strip(),
            "tracking_no": str(tracking_no or "").strip(),
            "message": str(message or ""),
            "sent_echo": sent_echo,
            "screenshots": evidence_paths,
        }
        self.repository.update_facts(task_id, current_facts)
        for path in evidence_paths:
            self.repository.add_task_evidence(task_id, "interception_group", path)

    def dispatch_in_transit_interception(
        self,
        task_id: int,
        routing: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Send a configured carrier interception request and audit it.

        No group-history lookup is performed.  The caller has already bound
        the current task to the exact shop/order/tracking values; this method
        only validates the route and target, then delegates the desktop send.
        """
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        group = routing.get("group") or {}
        message = str(routing.get("message") or "")
        if not routing.get("ok") or not group.get("group_name") or not message:
            raise RuntimeError(f"拦截路由未准备好：{routing}")
        try:
            assert_action_allowed(
                "SEND_INTERCEPT_REQUEST",
                self.config.execution_mode,
                self.config.live_actions_enabled,
                approved=True,
            )
        except SafetyViolation as exc:
            raise RuntimeError(str(exc)) from exc
        tracking_no = str(routing.get("tracking_no") or "").strip()
        prepared = self.group_dispatch.prepare(
            group,
            tracking_no,
            message,
            check_duplicate=False,
        )
        try:
            prior_dispatch = self.group_dispatch.verified_task_dispatch(
                task_id,
                group,
                tracking_no,
                message,
            )
        except RuntimeError as exc:
            raise RuntimeError(str(exc)) from exc
        if prior_dispatch is not None:
            evidence = dict(prior_dispatch["evidence"])
            self._persist_interception_dispatch_evidence(
                task_id,
                group,
                tracking_no,
                message,
                evidence,
            )
            return {
                "ok": True,
                "prepared": {**prepared, "task_dispatch_reused": True},
                "send": evidence,
                "reused": True,
                "action_key": prior_dispatch["action_key"],
            }
        execution_id = f"MESSAGE:{task_id}:{uuid.uuid4().hex[:12]}"
        timeout_seconds = float(
            dict(self.config.raw.get("runtime") or {}).get(
                "message_worker_timeout_seconds", 120
            )
        )
        self.repository.log_action(
            task_id,
            f"{execution_id}:worker_started",
            "MESSAGE_WORKER_LIFECYCLE",
            "started",
            {
                "execution_id": execution_id,
                "platform": group.get("platform"),
                "group_name": group.get("group_name"),
                "isolated": True,
            },
        )
        try:
            result = run_isolated_message_send(
                project_root=self.config.project_root,
                database_path=self.config.database_path,
                evidence_dir=self.config.evidence_dir,
                group=group,
                message=message,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )
        except MessageWorkerError as exc:
            self.repository.log_action(
                task_id,
                f"{execution_id}:worker_failed",
                "MESSAGE_WORKER_LIFECYCLE",
                "failed",
                {
                    "execution_id": execution_id,
                    "platform": group.get("platform"),
                    "group_name": group.get("group_name"),
                    "isolated": True,
                    "returncode": exc.returncode,
                    "native_crash": exc.crashed,
                    "error": str(exc),
                },
            )
            raise RuntimeError(str(exc)) from exc
        if not result.get("ok"):
            error = str(result.get("error") or "拦截消息发送失败")
            self.repository.log_action(
                task_id,
                f"{task['business_key']}:INTERCEPT_SEND:{int(time.time())}",
                "INTERCEPT_MESSAGE_SEND",
                "failed",
                {
                    "route": routing,
                    "prepared": prepared,
                    "connector": str(result.get("connector") or self.messages.name),
                    "error": error,
                    "isolated": True,
                },
            )
            self.repository.log_action(
                task_id,
                f"{execution_id}:worker_failed",
                "MESSAGE_WORKER_LIFECYCLE",
                "failed",
                {"execution_id": execution_id, "isolated": True, "error": error},
            )
            raise RuntimeError(error)
        evidence = {
            "connector": str(result.get("connector") or self.messages.name),
            **dict(result.get("data") or {}),
            "evidence": list(result.get("evidence") or []),
        }
        evidence_paths = [
            str(item).strip()
            for item in evidence.get("evidence") or []
            if str(item).strip()
        ]
        if not str(evidence.get("sent_echo") or "").strip() or not evidence_paths:
            error = "拦截消息发送结果缺少 sent_echo 或截图，禁止记录为成功"
            self.repository.log_action(
                task_id,
                f"{execution_id}:evidence_incomplete",
                "INTERCEPT_MESSAGE_SEND",
                "failed",
                {
                    "execution_id": execution_id,
                    "platform": group.get("platform"),
                    "group_name": group.get("group_name"),
                    "error": error,
                    "isolated": True,
                },
            )
            raise RuntimeError(error)

        # Persist the exact verified dispatch immediately after the worker
        # result.  If the process dies while hydrating task facts, the next run
        # can reconstruct them from this immutable action-log outbox instead of
        # sending the same group message again.
        recorded = self.group_dispatch.record_dispatched(
            task_id,
            group,
            tracking_no,
            message,
            evidence,
        )
        if not recorded:
            prior_dispatch = self.group_dispatch.verified_task_dispatch(
                task_id,
                group,
                tracking_no,
                message,
            )
            if prior_dispatch is None:
                raise RuntimeError("拦截消息审计记录写入冲突，禁止继续后续退款")
            evidence = dict(prior_dispatch["evidence"])
        self._persist_interception_dispatch_evidence(
            task_id,
            group,
            tracking_no,
            message,
            evidence,
        )
        self.repository.log_action(
            task_id,
            f"{execution_id}:worker_success",
            "MESSAGE_WORKER_LIFECYCLE",
            "success",
            {
                "execution_id": execution_id,
                "platform": group.get("platform"),
                "group_name": group.get("group_name"),
                "isolated": True,
            },
        )
        return {
            "ok": True,
            "prepared": {**prepared, "task_dispatch_reused": False},
            "send": evidence,
            "reused": False,
        }

    def prepare_in_transit_routing(self, task_id: int) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = task["facts"]
        if facts.get("work_order_title") not in {
            IN_TRANSIT_NO_REASON_TITLE,
            SHIPPED_NO_TRACE,
            AFTERSALE_SHIPPED_REFUND,
        }:
            raise RuntimeError("当前工单不支持快递拦截路由")
        routing = decide_interception(
            {
                "order_no": facts.get("order_no", ""),
                "wdt_logistics_company": facts.get("wdt_logistics_company", ""),
                "wdt_warehouse": facts.get("wdt_warehouse", ""),
                "carrier": facts.get("carrier", ""),
                "tracking_no": facts.get("tracking_no", ""),
                "latest_logistics_text": facts.get("latest_logistics_text", ""),
                "wdt_product_names": facts.get("wdt_product_names", []),
            },
            self.get_in_transit_config(),
        )
        # Persist the route preview before any external message send.  This
        # makes a paused/manual run auditable and prevents a later retry from
        # losing the exact carrier, group, message, and location decision.
        if routing.get("ok"):
            canonical_warehouse = str(routing.get("wdt_warehouse") or "").strip()
            raw_warehouse = str(routing.get("wdt_warehouse_raw") or "").strip()
            if canonical_warehouse:
                facts["wdt_warehouse"] = canonical_warehouse
            if raw_warehouse and raw_warehouse != canonical_warehouse:
                facts["wdt_warehouse_raw"] = raw_warehouse
            facts.setdefault("source_evidence", {})["interception_route"] = routing
            self.repository.update_facts(task_id, facts)
        self.repository.log_action(
            task_id,
            f"{task['business_key']}:IN_TRANSIT_ROUTE:{int(time.time())}",
            "CLASSIFY_IN_TRANSIT_ROUTE",
            "success" if routing.get("ok") else "failed",
            routing,
        )
        return routing

    def refresh_in_transit_detail(self, task_id: int) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = task["facts"]
        worker = self._run_isolated_pdd_refund_stage(
            task_id,
            facts,
            operation="in_transit_refresh",
        )
        fields = dict(worker.get("result") or {})
        if fields.get("tracking_no"):
            facts["tracking_no"] = fields["tracking_no"]
        if fields.get("carrier"):
            facts["carrier"] = fields["carrier"]
        facts["latest_logistics_text"] = fields.get("latest_logistics_text", "")
        self.repository.update_facts(task_id, facts)
        return {"ok": True, **fields, "task": self.repository.get_task(task_id)}

    def prepare_in_transit_reason(self, task_id: int, *, live: bool = False) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = task["facts"]
        config = self.get_in_transit_config()
        worker = self._run_isolated_pdd_refund_stage(
            task_id,
            facts,
            operation="in_transit_reason",
            reason_text=str(config.get("refund_reason_option_text") or "主观原因不想要"),
            live=live,
        )
        result = dict(worker.get("result") or {})
        # The fallback `处理情况` page can override the route's generic final
        # action.  Persist its observed form and latest-logistics decision
        # before the isolated final-click worker runs.
        facts["in_transit_detail_form"] = str(
            result.get("detail_form") or "reason"
        )
        facts["in_transit_final_action_override"] = str(
            result.get("final_action_override") or ""
        )
        facts["in_transit_refund_reason"] = str(
            result.get("refund_reason")
            or config.get("refund_reason_option_text")
            or "主观原因不想要"
        )
        if isinstance(result.get("decision"), dict):
            facts["in_transit_detail_decision"] = dict(result["decision"])
        else:
            facts.pop("in_transit_detail_decision", None)
        self.repository.update_facts(task_id, facts)
        self.repository.log_action(
            task_id,
            f"{task['business_key']}:IN_TRANSIT_REASON:{int(time.time())}",
            "PDD_IN_TRANSIT_REASON",
            "success",
            result,
        )
        return result

    def finish_in_transit_refund(self, task_id: int, action: str, *, live: bool = False) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = task["facts"]
        requested_action = str(action or "REJECT_REFUND")
        action = str(facts.get("in_transit_final_action_override") or requested_action)
        worker = self._run_isolated_pdd_refund_stage(
            task_id,
            facts,
            operation="in_transit_finish",
            action=action,
            live=live,
            close_detail_after_execute=bool(live),
        )
        result = dict(worker.get("result") or {})
        detail_cleanup = worker.get("detail_cleanup")
        if isinstance(detail_cleanup, dict):
            result["detail_cleanup"] = dict(detail_cleanup)
        return self._persist_in_transit_terminal(
            task_id,
            task,
            facts,
            result,
            requested_action=requested_action,
            effective_action=action,
            live=live,
        )

    def _persist_in_transit_terminal(
        self,
        task_id: int,
        task: dict[str, Any],
        facts: dict[str, Any],
        result: dict[str, Any],
        *,
        requested_action: str,
        effective_action: str,
        live: bool,
    ) -> dict[str, Any]:
        """Persist one terminal decision and its optional interception ledger."""
        result["requested_action"] = requested_action
        result["effective_action"] = effective_action
        submission_ineffective = bool(result.get("submission_ineffective"))
        if result.get("ok") and not submission_ineffective:
            # Keep the final merchant decision in the task facts as well as
            # the immutable action log.  The console uses facts for its task
            # detail view, so a completed in-transit item must not appear to
            # be missing its final action after a restart.
            facts["final_action"] = effective_action
            if effective_action == "AGREE_REFUND":
                facts["refund_agreed"] = True
            elif effective_action == "AGREED_CONSENSUS":
                facts["agreed_consensus"] = True
            elif effective_action == "REJECT_REFUND":
                facts["withdraw_requested"] = True
            self.repository.update_facts(task_id, facts)
        register = None
        if effective_action == "AGREE_REFUND" and live and result.get("ok") and not submission_ineffective:
            config = self.get_in_transit_config()
            register = self._upsert_routed_interception_register(
                task_id=task_id,
                facts=facts,
                refund_reason=str(
                    facts.get("in_transit_refund_reason")
                    or config.get("refund_reason_option_text")
                    or "主观原因不想要"
                ),
            )
            result["interception_register"] = register
            # The local console ledger is durable before this external step.
            # A DingTalk desktop issue must therefore never turn a completed
            # PDD refund into a failed refund; the dedicated sync ledger keeps
            # it pending for a later automatic/manual retry.
            result["interception_register_dingtalk"] = (
                self.sync_dingtalk_interception_register(int(register["id"]))
            )
        self.repository.log_action(
            task_id,
            f"{task['business_key']}:IN_TRANSIT_FINISH:{int(time.time())}",
            effective_action,
            "skipped" if submission_ineffective else "success",
            result,
        )
        return result

    def complete_in_transit_after_interception(
        self, task_id: int, *, live: bool = False
    ) -> dict[str, Any]:
        """Finish the entire PDD post-interception sequence in one UIA worker."""
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = task["facts"]
        config = self.get_in_transit_config()
        worker = self._run_isolated_pdd_refund_stage(
            task_id,
            facts,
            operation="in_transit_complete",
            reason_text=str(config.get("refund_reason_option_text") or "主观原因不想要"),
            routing_config=config,
            live=live,
            close_detail_after_execute=bool(live),
        )

        refreshed = dict(worker.get("refreshed") or {})
        for field in ("tracking_no", "carrier", "latest_logistics_text"):
            if field in refreshed:
                facts[field] = refreshed[field]
        reason = dict(worker.get("reason") or {})
        facts["in_transit_detail_form"] = str(reason.get("detail_form") or "reason")
        facts["in_transit_final_action_override"] = str(
            reason.get("final_action_override") or ""
        )
        facts["in_transit_refund_reason"] = str(
            reason.get("refund_reason")
            or config.get("refund_reason_option_text")
            or "主观原因不想要"
        )
        if isinstance(reason.get("decision"), dict):
            facts["in_transit_detail_decision"] = dict(reason["decision"])
        else:
            facts.pop("in_transit_detail_decision", None)
        self.repository.update_facts(task_id, facts)

        terminal = dict(worker.get("result") or {})
        detail_cleanup = worker.get("detail_cleanup")
        if isinstance(detail_cleanup, dict):
            terminal["detail_cleanup"] = dict(detail_cleanup)
        requested_action = str(worker.get("requested_action") or "REJECT_REFUND")
        effective_action = str(worker.get("effective_action") or requested_action)
        terminal = self._persist_in_transit_terminal(
            task_id,
            task,
            facts,
            terminal,
            requested_action=requested_action,
            effective_action=effective_action,
            live=live,
        )
        return {
            "ok": True,
            "refreshed": refreshed,
            "routing": dict(worker.get("routing") or {}),
            "reason": reason,
            "terminal": terminal,
            "detail_cleanup": dict(detail_cleanup or {}),
            "requested_action": requested_action,
            "effective_action": effective_action,
            "submission_ineffective": bool(terminal.get("submission_ineffective")),
            "skip_reason": str(terminal.get("skip_reason") or ""),
        }

    def complete_shipped_no_trace_after_interception(
        self, task_id: int, *, live: bool = False
    ) -> dict[str, Any]:
        """Submit the shipped-no-trace refund only after verified interception."""
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = task["facts"]
        if facts.get("work_order_title") != SHIPPED_NO_TRACE:
            raise RuntimeError("当前工单不是已发货无轨迹退款处理")
        message_evidence = dict(
            (facts.get("source_evidence") or {}).get("interception_message") or {}
        )
        if live and (
            not message_evidence.get("sent_echo")
            or not message_evidence.get("screenshots")
        ):
            raise RuntimeError("拦截消息尚未取得发送回读与截图，禁止执行退款")

        worker = self._run_isolated_pdd_refund_stage(
            task_id,
            facts,
            operation="shipped_interception_complete",
            live=live,
            close_detail_after_execute=bool(live),
        )
        result = dict(worker.get("result") or {})
        cleanup = dict(worker.get("detail_cleanup") or {})
        if cleanup:
            result["detail_cleanup"] = cleanup
        if result.get("ok") and live:
            facts["final_action"] = "AGREE_REFUND_AFTER_INTERCEPTION"
            facts["refund_agreed"] = True
            self.repository.update_facts(task_id, facts)
            register = self._upsert_routed_interception_register(
                task_id=task_id,
                facts=facts,
                refund_reason=SHIPPED_NO_TRACE,
            )
            result["interception_register"] = register
            result["interception_register_dingtalk"] = (
                self.sync_dingtalk_interception_register(int(register["id"]))
            )
        self.repository.log_action(
            task_id,
            f"{task['business_key']}:SHIPPED_INTERCEPTION_FINISH:{int(time.time())}",
            "SHIPPED_NO_TRACE_REFUND_AFTER_INTERCEPTION",
            "success",
            result,
        )
        return result

    def complete_aftersale_shipped_refund_after_interception(
        self,
        task_id: int,
        *,
        action: str | None = None,
        live: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Finish a shipped-refund action only after verified interception.

        The DingTalk interception register belongs only to the warehouse-aware
        ``AGREE_REFUND`` branch (聚水潭 <=1; 俊富/default <=3). Orders above
        their warehouse threshold use
        ``AGREE_RETURN_AFTER_REFUND``, ``AGREE_RECALL_AFTER_REFUND`` and
        ``REJECT_REFUND`` branches still
        perform the verified group interception, but intentionally do not
        append an online-table row.
        """

        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = dict(task["facts"])
        if facts.get("work_order_title") != AFTERSALE_SHIPPED_REFUND:
            raise RuntimeError("当前售后单不是已发货普通退款")
        decision = decide_aftersale(facts)
        allowed_actions = {
            AGREE_REFUND,
            AGREE_RETURN_AFTER_REFUND,
            AGREE_RECALL_AFTER_REFUND,
            REJECT_REFUND,
        }
        if decision.action not in allowed_actions:
            raise RuntimeError(f"当前售后详情不允许走拦截后处理路径：{decision.reason}")
        selected_action = str(action or decision.action).strip()
        if selected_action != decision.action:
            raise RuntimeError(
                "售后动作与最新详情决策不一致："
                f"请求={selected_action or '空'}，详情={decision.action}"
            )
        safety_result = self.validate_aftersale_shipped_refund_safety(
            task_id,
            action=selected_action,
            live=live,
        )
        message_evidence = dict(
            (facts.get("source_evidence") or {}).get("interception_message") or {}
        )
        if live and (
            not message_evidence.get("sent_echo")
            or not message_evidence.get("screenshots")
        ):
            raise RuntimeError("拦截消息尚未取得发送回读与截图，禁止执行售后动作")

        register: dict[str, Any] | None = None
        register_sync: dict[str, Any] | None = None
        if live and selected_action == AGREE_REFUND:
            # The verified carrier request is itself the event recorded in the
            # direct-refund interception ledger. Persist and synchronize it before
            # the PDD monetary action, so a crash cannot leave a successful
            # direct refund with no local/online registration.  Both layers are
            # idempotent by task/register id and therefore safe to resume after
            # a failure. Actions above the warehouse threshold are deliberately
            # excluded: those branches may not create a DingTalk registration row.
            register = self._upsert_routed_interception_register(
                task_id=task_id,
                facts=facts,
                refund_reason=AFTERSALE_SHIPPED_REFUND,
            )
            try:
                register_sync = self.sync_dingtalk_interception_register(
                    int(register["id"])
                )
            except Exception as exc:
                # No PDD write has started yet.  Convert every online-register
                # exception into an explicit retained/pending outcome so the
                # runtime cannot mistake an exception path for a completed
                # refund and an operator can safely inspect the durable row.
                register_sync = {
                    "ok": False,
                    "needs_review": True,
                    "write_outcome_unknown": True,
                    "reason_code": "DINGTALK_INTERCEPTION_SYNC_EXCEPTION",
                    "error": f"钉钉在线拦截登记异常：{exc}",
                }
            if not bool(register_sync.get("ok")):
                error = str(
                    register_sync.get("error")
                    or register_sync.get("reason")
                    or "钉钉在线拦截登记尚未同步成功"
                )
                pending = {
                    "ok": False,
                    "action": selected_action,
                    "needs_review": True,
                    "registration_pending": True,
                    "reason_code": "DINGTALK_INTERCEPTION_REGISTER_PENDING",
                    "error": error,
                    "business_write_performed": False,
                    "submit_attempted": False,
                    "interception_register": register,
                    "interception_register_dingtalk": register_sync,
                    "safety": safety_result,
                }
                self.repository.log_action(
                    task_id,
                    f"{task['business_key']}:AFTERSALE_REGISTER_PENDING:{int(time.time())}",
                    "AFTERSALE_SHIPPED_REFUND_AFTER_INTERCEPTION",
                    "registration_pending",
                    pending,
                )
                return pending

        completed = self.complete_aftersale_action(
            task_id,
            selected_action,
            live=live,
            cancel_event=cancel_event,
        )
        result = dict(completed.get("result") or {})
        for field in (
            "retryable",
            "reason_code",
            "retry_after_seconds",
            "prompt_text",
            "defer_kind",
            "stage_committed",
            "negotiation_round_submitted",
            "resume_action",
            "reject_surface",
            "write_outcome_verified",
            "submit_attempted",
            "needs_review",
            "error",
            "skip_reason",
        ):
            if field not in result and field in completed:
                result[field] = completed[field]
        cleanup = dict(completed.get("detail_cleanup") or {})
        if cleanup:
            result["detail_cleanup"] = cleanup
        result["safety"] = safety_result
        if register is not None:
            result["interception_register"] = register
        if register_sync is not None:
            result["interception_register_dingtalk"] = register_sync
        deferred = bool(result.get("deferred") or completed.get("deferred"))
        write_outcome_unknown = bool(
            result.get("write_outcome_unknown")
            or completed.get("write_outcome_unknown")
        )
        skipped = bool(result.get("skipped") or completed.get("skipped"))
        business_write_performed = bool(
            result.get("business_write_performed")
            or completed.get("business_write_performed")
        )
        needs_review = bool(
            result.get("needs_review") or completed.get("needs_review")
        )
        result.update(
            {
                "ok": bool(completed.get("ok", True))
                and bool(result.get("ok", True))
                and not write_outcome_unknown
                and not needs_review,
                "deferred": deferred,
                "write_outcome_unknown": write_outcome_unknown,
                "skipped": skipped,
                "business_write_performed": business_write_performed,
                "needs_review": needs_review,
            }
        )
        if skipped or deferred or write_outcome_unknown or needs_review:
            return result
        if result.get("ok") and live:
            current = self.repository.get_task(task_id)
            persisted_facts = dict((current or task).get("facts") or facts)
            persisted_facts["final_action"] = selected_action
            if selected_action == AGREE_REFUND:
                persisted_facts["refund_agreed"] = True
            elif selected_action == AGREE_RETURN_AFTER_REFUND:
                persisted_facts["refund_after_return_agreed"] = True
            elif selected_action == AGREE_RECALL_AFTER_REFUND:
                persisted_facts["refund_after_recall_agreed"] = True
            elif selected_action == REJECT_REFUND:
                persisted_facts["refund_rejected"] = True
            self.repository.update_facts(task_id, persisted_facts)
        self.repository.log_action(
            task_id,
            f"{task['business_key']}:AFTERSALE_SHIPPED_REFUND_FINISH:{int(time.time())}",
            "AFTERSALE_SHIPPED_REFUND_AFTER_INTERCEPTION",
            (
                "deferred"
                if deferred
                else (
                    "uncertain"
                    if write_outcome_unknown
                    else ("success" if result.get("ok") else "failed")
                )
            ),
            {"action": selected_action, **result},
        )
        return result

    def validate_aftersale_shipped_refund_safety(
        self,
        task_id: int,
        *,
        action: str = AGREE_REFUND,
        live: bool,
    ) -> dict[str, Any]:
        """Apply money/write guards before a group send or refund click."""

        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = dict(task["facts"])
        if facts.get("work_order_title") != AFTERSALE_SHIPPED_REFUND:
            raise RuntimeError("当前售后单不是已发货普通退款")
        if action not in {
            AGREE_REFUND,
            AGREE_RETURN_AFTER_REFUND,
            AGREE_RECALL_AFTER_REFUND,
            REJECT_REFUND,
        }:
            raise RuntimeError(f"该售后动作不允许走拦截后完成路径：{action or '空'}")
        if live:
            try:
                assert_action_allowed(
                    action,
                    self.config.execution_mode,
                    self.config.live_actions_enabled,
                    approved=True,
                )
                shop = next(
                    (
                        item
                        for item in self.config.shops
                        if item.enabled and item.id == facts.get("shop_id")
                    ),
                    None,
                )
                if not shop:
                    raise SafetyViolation("任务店铺不在启用的店铺白名单中")
                safety = dict(self.config.raw.get("safety") or {})
                if safety.get("require_exact_shop_name", True) or safety.get(
                    "require_exact_edge_profile", True
                ):
                    validate_shop_context(
                        SafetyContext(
                            configured_shop_name=shop.name,
                            page_shop_name=str(facts.get("shop_name") or ""),
                            task_shop_name=str(facts.get("shop_name") or ""),
                            configured_profile=shop.edge_profile,
                            active_profile=str(facts.get("edge_profile") or ""),
                        )
                    )
            except SafetyViolation as exc:
                raise RuntimeError(str(exc)) from exc
        else:
            safety = dict(self.config.raw.get("safety") or {})

        amount = float(facts.get("refund_amount") or 0)
        max_single = float(safety.get("max_single_refund_amount") or 0)
        money_guard_applied = action != REJECT_REFUND
        if money_guard_applied and max_single > 0 and amount > max_single + 0.001:
            raise RuntimeError(
                f"单笔退款金额 {amount:.2f} 超过安全上限 {max_single:.2f}"
            )
        prior_real_logs = [
            item
            for item in self.repository.list_logs(task_id=task_id, limit=100)
            if item.get("action_type") == "AFTERSALE_FINAL_ACTION"
            and (item.get("details") or {}).get("business_write_performed")
        ]
        staged_negotiations: list[dict[str, Any]] = []
        preconfirmation_attempts: list[dict[str, Any]] = []
        blocking_real_logs: list[dict[str, Any]] = []
        for item in prior_real_logs:
            details = dict(item.get("details") or {})
            result = dict(details.get("result") or {})
            trace_texts = [
                str(step.get("text") or "")
                for step in (result.get("trace") or [])
                if isinstance(step, dict)
            ]
            # Builds before the real-page test treated the first
            # ``提交协商方案`` click as the write.  PDD then presents a second
            # mandatory ``确认，发给消费者`` control; the captured trace proves
            # that control was never clicked, so this is a pre-confirmation
            # attempt and must not block the corrected final step.
            preconfirmation_only = (
                action == REJECT_REFUND
                and str(item.get("status") or "") == "uncertain"
                and str(details.get("action") or "") == REJECT_REFUND
                and str(result.get("reason_code") or "")
                == "PDD_REJECT_NEGOTIATION_OUTCOME_UNKNOWN"
                and "提交协商方案" in trace_texts
                and "确认，发给消费者" not in trace_texts
                and "确认，发送给消费者" not in trace_texts
            )
            if preconfirmation_only:
                preconfirmation_attempts.append(item)
                continue
            known_stage = (
                action == REJECT_REFUND
                and str(item.get("status") or "") == "deferred"
                and str(details.get("action") or "") == REJECT_REFUND
                and str(result.get("reason_code") or "")
                == "PDD_REJECT_NEGOTIATION_COOLDOWN"
                and str(result.get("defer_kind") or "") == "POST_SUBMIT_VERIFIED"
                and bool(result.get("stage_committed"))
                and bool(result.get("negotiation_round_submitted"))
                and not bool(result.get("write_outcome_unknown"))
                and bool(result.get("business_write_performed"))
                and bool(result.get("submit_attempted"))
            )
            (staged_negotiations if known_stage else blocking_real_logs).append(item)
        retry = dict(
            (facts.get("source_evidence") or {}).get("aftersale_retry") or {}
        )
        staged_retry_matches = (
            not staged_negotiations
            or (
                str(retry.get("reason_code") or "")
                == "PDD_REJECT_NEGOTIATION_COOLDOWN"
                and str(retry.get("resume_action") or "") == REJECT_REFUND
                and str(retry.get("defer_kind") or "") == "POST_SUBMIT_VERIFIED"
                and bool(retry.get("stage_committed"))
                and bool(retry.get("business_write_performed"))
                and str(retry.get("shop_id") or "") == str(facts.get("shop_id") or "")
                and str(retry.get("order_no") or "") == str(facts.get("order_no") or "")
                and str(retry.get("aftersale_no") or "")
                == str(facts.get("aftersale_no") or "")
            )
        )
        if blocking_real_logs or not staged_retry_matches or len(staged_negotiations) > 2:
            raise RuntimeError("该售后单已有真实业务写入记录，拒绝重复执行")
        return {
            "action": action,
            "execution_mode": str(self.config.execution_mode),
            "live_actions_enabled": self.config.live_actions_enabled,
            "refund_amount": amount,
            "money_guard_applied": money_guard_applied,
            "max_single_refund_amount": max_single,
            "verified_negotiation_rounds": len(staged_negotiations),
            "corrected_preconfirmation_attempts": len(preconfirmation_attempts),
        }

    def _upsert_routed_interception_register(
        self,
        *,
        task_id: int,
        facts: dict[str, Any],
        refund_reason: str,
    ) -> dict[str, Any]:
        """Persist the exact warehouse/group/table decision before any retry.

        Routing is based on the warehouse returned by WDT, while the group
        name comes from the already-confirmed interception send evidence.  A
        pending row therefore cannot drift to another document after restart
        or after a later configuration/facts refresh.
        """

        root_config = dict(
            self.config.raw.get("dingtalk_interception_register") or {}
        )
        warehouse = str(facts.get("wdt_warehouse") or "").strip()
        target_key = dingtalk_target_key_for_warehouse(root_config, warehouse)
        if not target_key:
            raise RuntimeError(
                f"旺店通仓库“{warehouse or '空'}”未匹配唯一钉钉登记表，已停止登记"
            )
        target_config = resolve_dingtalk_target_config(root_config, target_key)
        message_evidence = dict(
            (facts.get("source_evidence") or {}).get("interception_message") or {}
        )
        group_name = str(message_evidence.get("group_name") or "").strip()
        if target_key == DINGTALK_TARGET_SIKU_DISTRIBUTION and not group_name:
            raise RuntimeError("示例代发分销登记缺少已发送拦截群名称，已停止登记")
        default_add = 5 if target_key == DINGTALK_TARGET_JUNFU else 0
        try:
            amount_add = float(target_config.get("amount_add", default_add))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"钉钉登记目标“{target_key}”金额规则无效，已停止登记"
            ) from exc
        amount = round(float(facts.get("refund_amount") or 0) + amount_add, 2)
        return self.repository.upsert_interception_register(
            task_id=task_id,
            record_date=datetime.now().date().isoformat(),
            carrier=str(
                facts.get("carrier") or facts.get("wdt_logistics_company") or ""
            ),
            # The tracking number is captured from the PDD detail page; it is
            # unrelated to the WDT warehouse field used for routing.
            tracking_no=str(facts.get("tracking_no") or ""),
            order_no=str(facts.get("order_no") or ""),
            refund_reason=str(refund_reason or ""),
            amount=amount,
            warehouse=warehouse,
            group_name=group_name,
            dingtalk_target_key=target_key,
        )

    def sync_dingtalk_interception_register(self, register_id: int) -> dict[str, Any]:
        """Append one durable local register row to the shared DingTalk sheet."""
        with self._dingtalk_interception_sync_lock:
            return self._sync_dingtalk_interception_register_locked(register_id)

    def _sync_dingtalk_interception_register_locked(
        self, register_id: int
    ) -> dict[str, Any]:
        """Run one serialized sheet append without replaying an unknown write."""
        root_dingtalk_config = dict(
            self.config.raw.get("dingtalk_interception_register") or {}
        )
        if not bool(root_dingtalk_config.get("enabled", False)):
            return {"ok": False, "skipped": True, "reason": "DingTalk 同步未启用"}

        existing_sync = self.repository.get_dingtalk_interception_sync(
            int(register_id)
        )
        if existing_sync and str(existing_sync.get("status") or "") == "running":
            # ``running`` surviving outside this process means the worker may
            # have appended the remote row before crashing but did not commit
            # the local success marker.  Retrying an append cannot be proven
            # idempotent, so leave it for reconciliation rather than creating a
            # duplicate DingTalk row.  Known worker failures are marked
            # ``pending`` and remain safe to retry through the normal path.
            return {
                "ok": False,
                "needs_review": True,
                "write_outcome_unknown": True,
                "reason_code": "DINGTALK_INTERCEPTION_WRITE_OUTCOME_UNKNOWN",
                "register_id": int(register_id),
                "error": (
                    "钉钉在线登记上次写入停留在 running，远端结果未知；"
                    "为避免重复登记，已禁止自动重写"
                ),
                "sync": existing_sync,
            }

        attempt = self.repository.begin_dingtalk_interception_sync(int(register_id))
        record = dict(attempt["record"])
        sync = dict(attempt["sync"])
        task_id = int(record.get("task_id") or 0)
        action_prefix = f"DINGTALK_INTERCEPTION_REGISTER:{register_id}:{int(time.time())}"
        if attempt.get("already_synced"):
            return {
                "ok": True,
                "already_synced": True,
                "register_id": int(register_id),
                "sync": sync,
            }

        try:
            dingtalk_config = resolve_dingtalk_target_config(
                root_dingtalk_config,
                str(record.get("dingtalk_target_key") or ""),
            )
            if not bool(dingtalk_config.get("enabled", False)):
                raise DingTalkInterceptionRegisterError(
                    "当前仓库对应的钉钉登记目标未启用"
                )
        except DingTalkInterceptionRegisterError as exc:
            sync = self.repository.mark_dingtalk_interception_sync_failure(
                int(register_id), str(exc)
            )
            payload = {
                "ok": False,
                "needs_review": True,
                "reason_code": "DINGTALK_INTERCEPTION_TARGET_INVALID",
                "register_id": int(register_id),
                "error": str(exc),
                "sync": sync,
            }
            if task_id:
                self.repository.log_action(
                    task_id,
                    action_prefix,
                    "DINGTALK_INTERCEPTION_REGISTER",
                    "failed",
                    payload,
                )
            return payload

        runtime = dict(self.config.raw.get("runtime") or {})
        timeout_seconds = float(
            runtime.get("dingtalk_interception_worker_timeout_seconds", 120)
        )
        try:
            worker = run_isolated_dingtalk_interception_register(
                project_root=self.config.project_root,
                evidence_dir=self.config.evidence_dir,
                dingtalk_config=dingtalk_config,
                record=record,
                local_duplicate_tracking=bool(attempt.get("local_duplicate_tracking")),
                timeout_seconds=timeout_seconds,
            )
            worker_result = dict(worker.get("result") or {})
            sync = self.repository.mark_dingtalk_interception_sync_success(
                int(register_id),
                remote_row=int(worker_result["remote_row"]),
                duplicate_tracking=bool(worker_result.get("duplicate_tracking")),
            )
        except DingTalkInterceptionWorkerError as exc:
            sync = self.repository.mark_dingtalk_interception_sync_failure(
                int(register_id), str(exc)
            )
            payload = {
                "ok": False,
                "register_id": int(register_id),
                "error": str(exc),
                "sync": sync,
                "worker_crashed": exc.crashed,
            }
            if task_id:
                self.repository.log_action(
                    task_id,
                    action_prefix,
                    "DINGTALK_INTERCEPTION_REGISTER",
                    "failed",
                    payload,
                )
            return payload

        payload = {
            "ok": True,
            "register_id": int(register_id),
            "target_key": str(record.get("dingtalk_target_key") or ""),
            "result": worker_result,
            "sync": sync,
        }
        if task_id:
            self.repository.log_action(
                task_id,
                action_prefix,
                "DINGTALK_INTERCEPTION_REGISTER",
                "success",
                payload,
            )
        return payload

    def sync_pending_dingtalk_interception_registers(self, *, limit: int = 100) -> dict[str, Any]:
        """Retry retained rows that have not reached the shared online sheet."""
        pending = self.repository.list_pending_dingtalk_interception_registers(limit=limit)
        results = [
            self.sync_dingtalk_interception_register(int(register["id"]))
            for register in pending
        ]
        return {
            "ok": all(item.get("ok", False) or item.get("skipped", False) for item in results),
            "attempted": len(results),
            "succeeded": sum(1 for item in results if item.get("ok")),
            "failed": sum(1 for item in results if not item.get("ok") and not item.get("skipped")),
            "results": results,
        }

    def get_workflow_priority_config(self) -> dict[str, Any]:
        return validate_workflow_priority_config(
            self.repository.get_json_setting(WORKFLOW_PRIORITY_SETTING_KEY, WORKFLOW_PRIORITY_DEFAULT)
        )

    def update_workflow_priority_config(self, definition: dict[str, Any]) -> dict[str, Any]:
        validated = validate_workflow_priority_config(definition)
        validated["version"] = self.get_workflow_priority_config().get("version", 1) + 1
        self.repository.set_json_setting(WORKFLOW_PRIORITY_SETTING_KEY, validated)
        return validated

    def list_prioritized_tasks(
        self,
        state: str | None = None,
        view: str = "all",
        task_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        if view not in {"all", "pending", "processed"}:
            raise ValueError(f"不支持的工单视图：{view}")
        snapshot = self._ensure_live_sync_display_snapshot(
            self.repository.get_json_setting(LIVE_SYNC_SETTING_KEY)
        )
        display_ids = {
            int(item)
            for item in snapshot.get("display_task_ids", snapshot.get("task_ids", []))
            if str(item).isdigit()
        }
        if task_ids is not None:
            # Load the exact requested rows without the dashboard's 200-row
            # history cap; a full multi-shop scan touches far more rows than
            # that cap, and shops scanned early would otherwise fall out of
            # the updated_at window before the runtime queue is built.
            raw_tasks = self.repository.list_tasks_by_ids(task_ids)
            if state:
                raw_tasks = [task for task in raw_tasks if task["state"] == state]
        elif view == "pending" and snapshot.get("run_id"):
            raw_tasks = self.repository.list_tasks_by_ids(sorted(display_ids))
            if state:
                raw_tasks = [task for task in raw_tasks if task["state"] == state]
        else:
            raw_tasks = self.repository.list_tasks(state=state)
        tasks = prioritize_tasks(
            raw_tasks, self.get_workflow_priority_config()
        )
        current_ids = {
            int(item) for item in snapshot.get("task_ids", []) if str(item).isdigit()
        }
        stale_source_errors: dict[int, str] = {}
        for shop_snapshot in snapshot.get("display_shops", []):
            if not isinstance(shop_snapshot, dict):
                continue
            source_stale_sets = (
                (
                    "work_order_sync_error",
                    "stale_work_order_task_ids",
                ),
                (
                    "aftersale_sync_error",
                    "stale_aftersale_task_ids",
                ),
            )
            for error_key, task_ids_key in source_stale_sets:
                error = str(shop_snapshot.get(error_key) or "").strip()
                if not error:
                    continue
                for task_id in shop_snapshot.get(task_ids_key, []):
                    if str(task_id).isdigit():
                        stale_source_errors[int(task_id)] = error
        has_snapshot = bool(snapshot.get("run_id"))
        for task in tasks:
            source_evidence = (task.get("facts") or {}).get("source_evidence") or {}
            source_system = str(source_evidence.get("source_system") or "")
            if source_system == "qianniu_aftersale":
                task_kind = "qianniu"
                source_label = "千牛未收货退款"
            elif source_system == "aftersale_workbench":
                task_kind = "aftersale"
                source_label = "售后工作台"
            else:
                task_kind = "work_order"
                source_label = "工单管理"
            task["task_kind"] = task_kind
            task["task_source_label"] = source_label
            task["queue_scope"] = str(source_evidence.get("queue_scope") or "")
            task["current_sync"] = (task["id"] in current_ids) if has_snapshot else True
            task["live_pending"] = (task["id"] in display_ids) if has_snapshot else True
            task["source_stale"] = task["id"] in stale_source_errors
            task["source_sync_error"] = stale_source_errors.get(task["id"], "")
            task["sync_run_id"] = snapshot.get("run_id", "")
            task["sync_finished_at"] = snapshot.get("finished_at", "")
            task["display_state"] = DISPLAY_STATE_LABELS.get(task["state"], task["state"])
        if view == "pending":
            tasks = [
                task
                for task in tasks
                if task["live_pending"]
                and task["state"] not in NON_PENDING_STATES
            ]
        elif view == "processed":
            tasks = [task for task in tasks if task["state"] in NON_PENDING_STATES]
            tasks.sort(
                key=lambda task: str(task.get("completed_at") or task.get("updated_at") or ""),
                reverse=True,
            )
        return tasks

    def list_prioritized_tasks_by_ids(self, task_ids: list[int]) -> list[dict[str, Any]]:
        """Load an exact frozen runtime batch without the history row limit."""
        return prioritize_tasks(
            self.repository.list_tasks_by_ids(task_ids),
            self.get_workflow_priority_config(),
        )

    def connector_health(self) -> dict[str, Any]:
        results = {
            connector.name: asdict(connector.health())
            for connector in (
                self.pdd_readonly,
                self.jst,
                self.logistics,
                self.messages,
            )
        }
        return results

    def discover_live_readonly(
        self,
        shop_id: str | None = None,
        *,
        resume_snapshot: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        return self._discover_live_readonly_checkpointed(
            shop_id, resume_snapshot=resume_snapshot, cancel_event=cancel_event
        )

    def _legacy_discover_live_readonly(self, shop_id: str | None = None) -> dict[str, Any]:
        """从当前可见Edge窗口采集工单；没有任何真实写动作。"""
        if self.repository.is_paused():
            raise RuntimeError("系统已全局暂停")
        selected = [shop for shop in self.config.shops if shop.enabled]
        if shop_id:
            selected = [shop for shop in selected if shop.id == shop_id]
            if not selected:
                raise KeyError(f"店铺不存在或已停用：{shop_id}")

        valid_fields = {field.name for field in fields(WorkOrderFacts)}
        sync_run_id = uuid.uuid4().hex
        sync_started_at = datetime.now().isoformat(timespec="seconds")
        results: list[dict[str, Any]] = []
        for shop_index, shop in enumerate(selected):
            shop_data = {"id": shop.id, "name": shop.name, "edge_profile": shop.edge_profile}
            result = self.pdd_readonly.discover(shop_data)
            item_result: dict[str, Any] = {
                "shop_id": shop.id,
                "shop_name": shop.name,
                "ok": result.ok,
                "error": result.error,
                "task_ids": [],
                "total": 0,
                "evidence": result.evidence,
                "login_recovery": result.data.get("login_recovery") if result.ok else None,
                "pagination_trace": (result.data or {}).get("pagination_trace", []),
                "work_order_error": str(
                    (result.data or {}).get("work_order_error") or ""
                ),
                "work_order_total": int(
                    (result.data or {}).get("work_order_total") or 0
                ),
                "aftersale_error": str(
                    (result.data or {}).get("aftersale_error") or ""
                ),
                "aftersale_total": int(
                    (result.data or {}).get("aftersale_total") or 0
                ),
                "aftersale_discovered_total": int(
                    (result.data or {}).get("aftersale_discovered_total") or 0
                ),
                "aftersale_collection_summary": dict(
                    (result.data or {}).get("aftersale_collection_summary") or {}
                ),
                "sync_run_id": sync_run_id,
            }
            if result.ok:
                for item_index, item in enumerate(result.data.get("items", [])):
                    facts = WorkOrderFacts(
                        **{key: value for key, value in item.items() if key in valid_fields}
                    )
                    try:
                        page_no = int((item.get("source_evidence") or {}).get("page_no") or 1)
                    except (TypeError, ValueError):
                        page_no = 1
                    try:
                        page_index = int((item.get("source_evidence") or {}).get("page_index") or item_index)
                    except (TypeError, ValueError):
                        page_index = item_index
                    facts.source_evidence = {
                        **facts.source_evidence,
                        "sync_run_id": sync_run_id,
                        "sync_started_at": sync_started_at,
                        "shop_scan_order": shop_index,
                        "page_no": page_no,
                        "page_index": page_index,
                        "scan_order": (shop_index * 1000000) + (page_no * 10000) + page_index,
                    }
                    task_id_value = self.repository.upsert_task(facts)
                    self.repository.log_action(
                        task_id_value,
                        f"{facts.business_key}:PDD_READONLY",
                        "DISCOVER_PDD_READONLY",
                        "success",
                        {
                            "connector": self.pdd_readonly.name,
                            "evidence": result.evidence,
                            "pagination_trace": (result.data or {}).get("pagination_trace", []),
                            "write_performed": False,
                        },
                    )
                    item_result["task_ids"].append(task_id_value)
                item_result["total"] = len(item_result["task_ids"])
            else:
                self.repository.log_action(
                    None,
                    f"PDD_READONLY_ERROR:{shop.id}:{int(time.time())}",
                    "DISCOVER_PDD_READONLY",
                    "failed",
                    {"shop_id": shop.id, "error": result.error, "write_performed": False},
                )
            results.append(item_result)
        sync_finished_at = datetime.now().isoformat(timespec="seconds")
        all_task_ids = [task_id for item in results for task_id in item["task_ids"]]
        snapshot = {
            "run_id": sync_run_id,
            "started_at": sync_started_at,
            "finished_at": sync_finished_at,
            "requested_shop_id": shop_id or "",
            "complete": all(item["ok"] for item in results),
            "task_ids": all_task_ids,
            "total": len(all_task_ids),
            "shops": [
                {
                    "shop_id": item["shop_id"],
                    "shop_name": item["shop_name"],
                    "ok": item["ok"],
                    "error": item["error"],
                    "total": item["total"],
                    "task_ids": item["task_ids"],
                    "evidence": item["evidence"],
                    "login_recovery": item.get("login_recovery"),
                }
                for item in results
            ],
        }
        self.repository.set_json_setting(LIVE_SYNC_SETTING_KEY, snapshot)
        return {"ok": snapshot["complete"], "results": results, "sync": snapshot}

    def _live_sync_snapshot(
        self,
        *,
        run_id: str,
        started_at: str,
        requested_shop_id: str | None,
        selected: list[Any],
        results: list[dict[str, Any]],
        next_shop_index: int,
        in_progress: bool,
        current_shop_id: str = "",
        current_attempt: int = 0,
        baseline_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        all_task_ids = [task_id for item in results for task_id in item.get("task_ids", [])]
        baseline = self._ensure_live_sync_display_snapshot(dict(baseline_snapshot or {}))
        baseline_shop_items = (
            baseline.get("display_shops")
            or baseline.get("baseline_shops")
            or baseline.get("shops")
            or []
        )
        baseline_by_shop = {
            str(item.get("shop_id")): dict(item)
            for item in baseline_shop_items
            if isinstance(item, dict) and item.get("shop_id")
        }
        result_by_shop = {
            str(item.get("shop_id")): item
            for item in results
            if isinstance(item, dict) and item.get("shop_id")
        }
        partial_aftersale_shop_ids = {
            shop_key
            for shop_key, item in result_by_shop.items()
            if item.get("ok") and str(item.get("aftersale_error") or "").strip()
        }
        partial_work_order_shop_ids = {
            shop_key
            for shop_key, item in result_by_shop.items()
            if item.get("ok") and str(item.get("work_order_error") or "").strip()
        }
        previous_aftersale_task_ids: set[int] = set()
        previous_work_order_task_ids: set[int] = set()
        partial_source_shop_ids = (
            partial_aftersale_shop_ids | partial_work_order_shop_ids
        )
        if partial_source_shop_ids:
            candidate_ids = list(
                dict.fromkeys(
                    int(task_id)
                    for shop_key in partial_source_shop_ids
                    for task_id in baseline_by_shop.get(shop_key, {}).get("task_ids", [])
                    if str(task_id).isdigit()
                )
            )
            for task in self.repository.list_tasks_by_ids(candidate_ids):
                source = (task.get("facts") or {}).get("source_evidence") or {}
                queue_scope = str(source.get("queue_scope") or "").strip()
                if (
                    source.get("source_system") == "aftersale_workbench"
                    and queue_scope in {"", "pending_soon_overdue"}
                ):
                    previous_aftersale_task_ids.add(int(task["id"]))
                elif source.get("source_system") != "aftersale_workbench":
                    previous_work_order_task_ids.add(int(task["id"]))
        selected_by_id = {str(shop.id): shop for shop in selected}
        display_shop_ids = list(selected_by_id)
        # Refreshing one shop must not hide the other ten shops.
        if requested_shop_id:
            display_shop_ids.extend(
                key for key in baseline_by_shop if key not in selected_by_id
            )
        display_shops: list[dict[str, Any]] = []
        display_task_ids: list[int] = []
        for shop_key in display_shop_ids:
            current = result_by_shop.get(shop_key)
            previous = baseline_by_shop.get(shop_key, {})
            work_order_sync_error = ""
            aftersale_sync_error = ""
            stale_work_order_task_ids: list[int] = []
            stale_aftersale_task_ids: list[int] = []
            if current is not None and current.get("ok"):
                current_ids = [int(item) for item in current.get("task_ids", [])]
                work_order_sync_error = str(
                    current.get("work_order_error") or ""
                ).strip()
                aftersale_sync_error = str(
                    current.get("aftersale_error") or ""
                ).strip()
                if work_order_sync_error:
                    stale_work_order_task_ids = [
                        int(item)
                        for item in previous.get("task_ids", [])
                        if str(item).isdigit()
                        and int(item) in previous_work_order_task_ids
                        and int(item) not in current_ids
                    ]
                if aftersale_sync_error:
                    stale_aftersale_task_ids = [
                        int(item)
                        for item in previous.get("task_ids", [])
                        if str(item).isdigit()
                        and int(item) in previous_aftersale_task_ids
                        and int(item) not in current_ids
                    ]
                chosen_ids = list(
                    dict.fromkeys(
                        [
                            *current_ids,
                            *stale_work_order_task_ids,
                            *stale_aftersale_task_ids,
                        ]
                    )
                )
                source = (
                    "current_partial"
                    if work_order_sync_error or aftersale_sync_error
                    else "current"
                )
            else:
                chosen_ids = [
                    int(item)
                    for item in previous.get("task_ids", [])
                    if str(item).isdigit()
                ]
                source = "previous"
            shop = selected_by_id.get(shop_key)
            display_shops.append(
                {
                    "shop_id": shop_key,
                    "shop_name": (
                        str(getattr(shop, "name", ""))
                        or str(previous.get("shop_name") or shop_key)
                    ),
                    "task_ids": chosen_ids,
                    "total": len(chosen_ids),
                    "source": source,
                    "scanned": current is not None,
                    "work_order_stale": bool(work_order_sync_error),
                    "work_order_sync_error": work_order_sync_error,
                    "stale_work_order_task_ids": stale_work_order_task_ids,
                    "aftersale_stale": bool(aftersale_sync_error),
                    "aftersale_sync_error": aftersale_sync_error,
                    "stale_aftersale_task_ids": stale_aftersale_task_ids,
                }
            )
            display_task_ids.extend(chosen_ids)
        display_task_ids = list(dict.fromkeys(display_task_ids))
        return {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": "" if in_progress else datetime.now().isoformat(timespec="seconds"),
            "requested_shop_id": requested_shop_id or "",
            "selected_shop_ids": [shop.id for shop in selected],
            "complete": (
                (not in_progress)
                and all(item.get("ok") for item in results)
                and not any(
                    str(item.get("work_order_error") or "").strip()
                    or str(item.get("aftersale_error") or "").strip()
                    for item in results
                )
            ),
            "in_progress": in_progress,
            "next_shop_index": int(next_shop_index),
            "current_shop_id": current_shop_id,
            "current_attempt": int(current_attempt),
            "task_ids": all_task_ids,
            "total": len(all_task_ids),
            "baseline_task_ids": list(
                dict.fromkeys(
                    int(item)
                    for item in baseline.get(
                        "display_task_ids", baseline.get("task_ids", [])
                    )
                    if str(item).isdigit()
                )
            ),
            "baseline_shops": baseline_shop_items,
            "display_task_ids": display_task_ids,
            "display_total": len(display_task_ids),
            "display_shops": display_shops,
            "shops": [
                {
                    "shop_id": item["shop_id"],
                    "shop_name": item["shop_name"],
                    "ok": item["ok"],
                    "error": item.get("error"),
                    "total": item.get("total", 0),
                    "task_ids": item.get("task_ids", []),
                    "evidence": item.get("evidence", []),
                    "login_recovery": item.get("login_recovery"),
                    "attempts": item.get("attempts", 1),
                    "work_order_total": item.get("work_order_total", 0),
                    "work_order_sync_error": item.get("work_order_error", ""),
                    "aftersale_total": item.get("aftersale_total", 0),
                    "aftersale_discovered_total": item.get(
                        "aftersale_discovered_total", 0
                    ),
                    "aftersale_collection_summary": item.get(
                        "aftersale_collection_summary", {}
                    ),
                    "aftersale_sync_error": item.get("aftersale_error", ""),
                }
                for item in results
            ],
        }

    def _ensure_live_sync_display_snapshot(
        self, snapshot: dict[str, Any], *, persist: bool = False
    ) -> dict[str, Any]:
        """Upgrade legacy/empty snapshots without changing any task state."""
        if not snapshot.get("run_id") or "display_task_ids" in snapshot:
            return snapshot
        upgraded = dict(snapshot)
        task_ids = [
            int(item) for item in snapshot.get("task_ids", []) if str(item).isdigit()
        ]
        shops = [dict(item) for item in snapshot.get("shops", []) if isinstance(item, dict)]
        if not task_ids and not shops:
            recovered = self.repository.recover_latest_live_tasks_by_shop()
            task_ids = recovered["task_ids"]
            shops = recovered["shops"]
            upgraded["display_recovered"] = True
        upgraded["display_task_ids"] = list(dict.fromkeys(task_ids))
        upgraded["display_total"] = len(upgraded["display_task_ids"])
        upgraded["display_shops"] = [
            {
                **item,
                "task_ids": [
                    int(task_id)
                    for task_id in item.get("task_ids", [])
                    if str(task_id).isdigit()
                ],
                "source": item.get("source") or "previous",
            }
            for item in shops
        ]
        if persist:
            self.repository.set_json_setting(LIVE_SYNC_SETTING_KEY, upgraded)
        return upgraded

    def finalize_live_sync_after_stop(self) -> dict[str, Any]:
        """Freeze the last visible live queue when the operator stops runtime."""
        snapshot = self._ensure_live_sync_display_snapshot(
            self.repository.get_json_setting(LIVE_SYNC_SETTING_KEY)
        )
        if not snapshot.get("run_id"):
            return snapshot
        frozen = {
            **snapshot,
            "in_progress": False,
            "complete": False,
            "interrupted": True,
            "current_shop_id": "",
            "current_attempt": 0,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.repository.set_json_setting(LIVE_SYNC_SETTING_KEY, frozen)
        return frozen

    def _discover_live_readonly_checkpointed(
        self,
        shop_id: str | None = None,
        *,
        resume_snapshot: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Scan shops with a durable checkpoint before every UIA operation."""
        if self.repository.is_paused():
            raise RuntimeError("绯荤粺宸插叏灞€鏆傚仠")
        selected = [shop for shop in self.config.shops if shop.enabled]
        if shop_id:
            selected = [shop for shop in selected if shop.id == shop_id]
            if not selected:
                raise KeyError(f"搴楅摵涓嶅瓨鍦ㄦ垨宸插仠鐢細{shop_id}")

        valid_fields = {field.name for field in fields(WorkOrderFacts)}
        candidate = dict(resume_snapshot or {})
        existing_snapshot = self._ensure_live_sync_display_snapshot(
            self.repository.get_json_setting(LIVE_SYNC_SETTING_KEY)
        )
        selected_ids = [shop.id for shop in selected]
        can_resume = bool(candidate.get("in_progress")) and (
            list(candidate.get("selected_shop_ids") or selected_ids) == selected_ids
        ) and str(candidate.get("requested_shop_id") or "") == (shop_id or "")
        sync_run_id = (
            str(candidate.get("run_id") or uuid.uuid4().hex)
            if can_resume
            else uuid.uuid4().hex
        )
        sync_started_at = (
            str(candidate.get("started_at") or datetime.now().isoformat(timespec="seconds"))
            if can_resume
            else datetime.now().isoformat(timespec="seconds")
        )
        previous_by_shop = (
            {
                str(item.get("shop_id")): dict(item)
                for item in (candidate.get("shops") or [])
                if isinstance(item, dict) and item.get("shop_id")
            }
            if can_resume
            else {}
        )
        baseline_snapshot = candidate if can_resume else existing_snapshot
        runtime_config = self.config.raw.get("runtime", {})
        try:
            # ``scan_retry_per_shop`` means retries *after* the first scan.
            # A value of 1 therefore produces two scan attempts.  The old
            # range(1, retry_count + 1) treated it as the total attempt count,
            # so a transient source-level UIA failure received no retry.
            retry_count = max(
                0, min(int(runtime_config.get("scan_retry_per_shop", 1)), 4)
            )
        except (TypeError, ValueError):
            retry_count = 1
        scan_attempts = 1 + retry_count
        try:
            retry_delay = max(
                0.2, min(float(runtime_config.get("scan_retry_delay_seconds", 1)), 30.0)
            )
        except (TypeError, ValueError):
            retry_delay = 1.0

        results: list[dict[str, Any]] = []
        next_shop_index = 0
        if can_resume:
            try:
                next_shop_index = max(
                    0, min(int(candidate.get("next_shop_index", 0)), len(selected))
                )
            except (TypeError, ValueError):
                next_shop_index = 0
            for completed_index in range(next_shop_index):
                previous = previous_by_shop.get(selected[completed_index].id)
                if previous and previous.get("ok"):
                    results.append(previous)

        # Write the initial checkpoint before touching UIA. If the native layer
        # crashes during the first shop, the next process still knows where to
        # retry and does not mistake an old completed sync for a live one.
        self.repository.set_json_setting(
            LIVE_SYNC_SETTING_KEY,
            self._live_sync_snapshot(
                run_id=sync_run_id,
                started_at=sync_started_at,
                requested_shop_id=shop_id,
                selected=selected,
                results=results,
                next_shop_index=next_shop_index,
                in_progress=True,
                baseline_snapshot=baseline_snapshot,
            ),
        )

        for shop_index, shop in enumerate(selected):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("用户立即停止运行")
            previous = previous_by_shop.get(shop.id)
            if can_resume and shop_index < next_shop_index and previous and previous.get("ok"):
                continue

            shop_data = {"id": shop.id, "name": shop.name, "edge_profile": shop.edge_profile}
            result = None
            attempts = 0
            for attempts in range(1, scan_attempts + 1):
                self.repository.set_json_setting(
                    LIVE_SYNC_SETTING_KEY,
                    self._live_sync_snapshot(
                        run_id=sync_run_id,
                        started_at=sync_started_at,
                        requested_shop_id=shop_id,
                        selected=selected,
                        results=results,
                        next_shop_index=shop_index,
                        in_progress=True,
                        current_shop_id=shop.id,
                        current_attempt=attempts,
                        baseline_snapshot=baseline_snapshot,
                    ),
                )
                result = self.pdd_readonly.discover(shop_data, cancel_event=cancel_event)
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("用户立即停止运行")
                source_errors = [
                    str((result.data or {}).get(key) or "").strip()
                    for key in ("work_order_error", "aftersale_error")
                ]
                scan_complete = bool(result.ok) and not any(source_errors)
                # A connector is deliberately ``ok`` when either source
                # succeeds, so the fresh source remains usable.  It is not a
                # complete shop scan, however: retry once before accepting a
                # partial result, otherwise the queue can contain only the
                # aftersale rows while the current work-order rows are stale.
                if scan_complete or attempts >= scan_attempts:
                    break
                if cancel_event is not None:
                    if cancel_event.wait(retry_delay):
                        raise InterruptedError("用户立即停止运行")
                else:
                    time.sleep(retry_delay)

            assert result is not None
            item_result: dict[str, Any] = {
                "shop_id": shop.id,
                "shop_name": shop.name,
                "ok": result.ok,
                "error": result.error,
                "task_ids": [],
                "total": 0,
                "evidence": result.evidence,
                "login_recovery": result.data.get("login_recovery") if result.ok else None,
                "pagination_trace": (result.data or {}).get("pagination_trace", []),
                "work_order_error": str(
                    (result.data or {}).get("work_order_error") or ""
                ),
                "work_order_total": int(
                    (result.data or {}).get("work_order_total") or 0
                ),
                "aftersale_error": str(
                    (result.data or {}).get("aftersale_error") or ""
                ),
                "aftersale_total": int(
                    (result.data or {}).get("aftersale_total") or 0
                ),
                "aftersale_discovered_total": int(
                    (result.data or {}).get("aftersale_discovered_total") or 0
                ),
                "aftersale_collection_summary": dict(
                    (result.data or {}).get("aftersale_collection_summary") or {}
                ),
                "sync_run_id": sync_run_id,
                "attempts": attempts,
            }
            if result.ok:
                for item_index, item in enumerate(result.data.get("items", [])):
                    facts = WorkOrderFacts(
                        **{key: value for key, value in item.items() if key in valid_fields}
                    )
                    try:
                        page_no = int((item.get("source_evidence") or {}).get("page_no") or 1)
                    except (TypeError, ValueError):
                        page_no = 1
                    try:
                        page_index = int(
                            (item.get("source_evidence") or {}).get("page_index") or item_index
                        )
                    except (TypeError, ValueError):
                        page_index = item_index
                    facts.source_evidence = {
                        **facts.source_evidence,
                        "sync_run_id": sync_run_id,
                        "sync_started_at": sync_started_at,
                        "shop_scan_order": shop_index,
                        "page_no": page_no,
                        "page_index": page_index,
                        "scan_order": (shop_index * 1000000) + (page_no * 10000) + page_index,
                    }
                    task_id_value = self.repository.upsert_task(facts)
                    self.repository.log_action(
                        task_id_value,
                        f"{facts.business_key}:PDD_READONLY",
                        "DISCOVER_PDD_READONLY",
                        "success",
                        {
                            "connector": self.pdd_readonly.name,
                            "evidence": result.evidence,
                            "write_performed": False,
                        },
                    )
                    item_result["task_ids"].append(task_id_value)
                item_result["total"] = len(item_result["task_ids"])
            else:
                self.repository.log_action(
                    None,
                    f"PDD_READONLY_ERROR:{shop.id}:{int(time.time())}",
                    "DISCOVER_PDD_READONLY",
                    "failed",
                    {
                        "shop_id": shop.id,
                        "error": result.error,
                        "attempts": attempts,
                        "worker_crash": bool((result.data or {}).get("worker_crashed")),
                        "pagination_trace": (result.data or {}).get("pagination_trace", []),
                        "write_performed": False,
                    },
                )
            results.append(item_result)
            next_shop_index = shop_index + 1
            self.repository.set_json_setting(
                LIVE_SYNC_SETTING_KEY,
                self._live_sync_snapshot(
                    run_id=sync_run_id,
                    started_at=sync_started_at,
                    requested_shop_id=shop_id,
                    selected=selected,
                    results=results,
                    next_shop_index=next_shop_index,
                    in_progress=True,
                    baseline_snapshot=baseline_snapshot,
                ),
            )

        qianniu_result: dict[str, Any] | None = None
        qianniu_cycle = self._qianniu_cycle_config()
        if not shop_id and qianniu_cycle.workflow_enabled:
            # This call is deliberately after the completed PDD loop: it is
            # the twelfth scan and can never displace or short-circuit a PDD
            # shop result.
            try:
                qianniu_result = self.discover_qianniu_readonly(
                    cancel_event=cancel_event
                )
            except Exception as exc:
                raw = self._qianniu_raw_config()
                qianniu_result = {
                    "shop_id": str(raw.get("id") or "qianniu_01"),
                    "shop_name": str(raw.get("shop_name") or "示例天猫旗舰店"),
                    "ok": False,
                    "error": str(exc),
                    "task_ids": [],
                    "total": 0,
                    "platform": "tmall",
                    "scan_position": qianniu_cycle.scan_position,
                }
            results.append(qianniu_result)

        snapshot = self._live_sync_snapshot(
            run_id=sync_run_id,
            started_at=sync_started_at,
            requested_shop_id=shop_id,
            selected=selected,
            results=results,
            next_shop_index=len(selected),
            in_progress=False,
            baseline_snapshot=baseline_snapshot,
        )
        if qianniu_result is not None:
            qianniu_display = {
                "shop_id": qianniu_result["shop_id"],
                "shop_name": qianniu_result["shop_name"],
                "task_ids": list(qianniu_result.get("task_ids") or []),
                "total": int(qianniu_result.get("total") or 0),
                "source": "current" if qianniu_result.get("ok") else "failed",
                "scanned": True,
                "work_order_stale": False,
                "work_order_sync_error": "",
                "stale_work_order_task_ids": [],
                "aftersale_stale": not bool(qianniu_result.get("ok")),
                "aftersale_sync_error": str(qianniu_result.get("error") or ""),
                "stale_aftersale_task_ids": [],
                "platform": "tmall",
                "scan_position": qianniu_cycle.scan_position,
            }
            snapshot["display_shops"] = [
                *list(snapshot.get("display_shops") or []),
                qianniu_display,
            ]
            snapshot["display_task_ids"] = list(
                dict.fromkeys(
                    [
                        *list(snapshot.get("display_task_ids") or []),
                        *list(qianniu_result.get("task_ids") or []),
                    ]
                )
            )
            snapshot["display_total"] = len(snapshot["display_task_ids"])
        self.repository.set_json_setting(LIVE_SYNC_SETTING_KEY, snapshot)
        return {"ok": snapshot["complete"], "results": results, "sync": snapshot}

    def discover_qianniu_readonly(
        self, *, cancel_event: threading.Event | None = None
    ) -> dict[str, Any]:
        """Scan the configured twelfth shop and persist eligible rows only."""

        cycle = self._qianniu_cycle_config()
        raw = self._qianniu_raw_config()
        if not cycle.workflow_enabled:
            return {
                "shop_id": str(raw.get("id") or "qianniu_01"),
                "shop_name": str(raw.get("shop_name") or "示例天猫旗舰店"),
                "ok": True,
                "skipped": True,
                "reason": "千牛工作流开关未开启",
                "task_ids": [],
                "total": 0,
            }
        worker = self._run_qianniu_worker(
            operation="scan", cancel_event=cancel_event
        )
        records = list(worker.get("eligible_records") or [])
        task_ids: list[int] = []
        shop_id = str(raw.get("id") or "qianniu_01")
        shop_name = str(raw.get("shop_name") or "示例天猫旗舰店")
        edge_profile = str(raw.get("edge_profile") or "Default")
        sync_run_id = uuid.uuid4().hex
        for item_index, record in enumerate(records):
            facts = WorkOrderFacts(
                shop_id=shop_id,
                shop_name=shop_name,
                edge_profile=edge_profile,
                work_order_no=str(record.get("aftersale_no") or ""),
                order_no=str(record.get("order_no") or ""),
                work_order_title=QIANNIU_UNRECEIVED_REFUND,
                refund_type=str(record.get("refund_type") or ""),
                refund_amount=float(record.get("refund_amount") or 0),
                pdd_order_status=str(record.get("shipping_status") or ""),
                source_evidence={
                    "source_system": "qianniu_aftersale",
                    "platform": "tmall",
                    "queue_scope": "unreceived_refund_pending",
                    "aftersale_no": str(record.get("aftersale_no") or ""),
                    "aftersale_status": str(record.get("aftersale_status") or ""),
                    "available_actions": list(record.get("available_actions") or []),
                    "automation_eligible": True,
                    "sync_run_id": sync_run_id,
                    # PDD shop indexes are zero-based 0..10; Qianniu is 11.
                    "shop_scan_order": cycle.scan_position - 1,
                    "page_no": 1,
                    "page_index": item_index,
                    "scan_order": ((cycle.scan_position - 1) * 1000000) + item_index,
                },
            )
            task_id = self.repository.upsert_task(facts)
            task_ids.append(task_id)
            self.repository.log_action(
                task_id,
                f"{facts.business_key}:QIANNIU_READONLY",
                "DISCOVER_QIANNIU_READONLY",
                "success",
                {"record": record, "write_performed": False},
            )
        return {
            "shop_id": shop_id,
            "shop_name": shop_name,
            "ok": True,
            "error": "",
            "task_ids": task_ids,
            "total": len(task_ids),
            "source_total": int(worker.get("total") or 0),
            "eligible_total": int(worker.get("eligible_total") or 0),
            "evidence": [],
            "sync_run_id": sync_run_id,
            "platform": "tmall",
            "scan_position": cycle.scan_position,
        }

    def publish_qianniu_live_snapshot(
        self, qianniu_result: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace only the twelfth-shop slice of the live console snapshot."""

        snapshot = dict(
            self.repository.get_json_setting(LIVE_SYNC_SETTING_KEY, {}) or {}
        )
        shop_id = str(qianniu_result.get("shop_id") or "qianniu_01")
        task_ids = list(
            dict.fromkeys(int(item) for item in qianniu_result.get("task_ids") or [])
        )
        old_ids: set[int] = set()
        old_display_shops = list(snapshot.get("display_shops") or [])
        for item in old_display_shops:
            if str(item.get("shop_id") or "") == shop_id:
                old_ids.update(int(value) for value in item.get("task_ids") or [])

        display_shop = {
            "shop_id": shop_id,
            "shop_name": str(
                qianniu_result.get("shop_name") or "示例天猫旗舰店"
            ),
            "task_ids": task_ids,
            "total": len(task_ids),
            "source": "current" if qianniu_result.get("ok") else "failed",
            "scanned": True,
            "work_order_stale": False,
            "work_order_sync_error": "",
            "stale_work_order_task_ids": [],
            "aftersale_stale": not bool(qianniu_result.get("ok")),
            "aftersale_sync_error": str(qianniu_result.get("error") or ""),
            "stale_aftersale_task_ids": [],
            "platform": "tmall",
            "scan_position": int(qianniu_result.get("scan_position") or 12),
        }
        snapshot["display_shops"] = [
            *[
                item
                for item in old_display_shops
                if str(item.get("shop_id") or "") != shop_id
            ],
            display_shop,
        ]
        for key, total_key in (
            ("task_ids", "total"),
            ("display_task_ids", "display_total"),
        ):
            retained = [
                int(item)
                for item in snapshot.get(key) or []
                if int(item) not in old_ids
            ]
            snapshot[key] = list(dict.fromkeys([*retained, *task_ids]))
            snapshot[total_key] = len(snapshot[key])
        self.repository.set_json_setting(LIVE_SYNC_SETTING_KEY, snapshot)
        return snapshot

    def _complete_qianniu_externally_handled(
        self,
        task_id: int,
        *,
        reason: str,
        stage: str,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = dict(task.get("facts") or {})
        source = dict(facts.get("source_evidence") or {})
        source["qianniu_external_resolution"] = {
            "status": "EXTERNALLY_HANDLED",
            "stage": stage,
            "reason": str(reason or "平台状态已由人工或其他流程改变"),
            "state": dict(state or {}),
            "business_write_performed": False,
        }
        facts["source_evidence"] = source
        self.repository.update_facts(task_id, facts)
        self.repository.mark_task_status(
            task_id,
            TaskState.COMPLETED,
            "千牛售后已由人工或外部流程处理，当前无需继续操作",
        )
        return {
            "ok": True,
            "status": "SKIPPED_EXTERNALLY_HANDLED",
            "externally_handled": True,
            "stage": stage,
            "reason": str(reason or ""),
            "task": self.repository.get_task(task_id),
        }

    def _revalidate_qianniu_task(
        self,
        task_id: int,
        *,
        stage: str,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = dict(task.get("facts") or {})
        source = dict(facts.get("source_evidence") or {})
        worker = self._run_qianniu_worker(
            operation="revalidate",
            order_no=str(facts.get("order_no") or ""),
            aftersale_no=str(
                source.get("aftersale_no") or facts.get("work_order_no") or ""
            ),
            cancel_event=cancel_event,
        )
        if worker.get("externally_handled") or not worker.get("processable"):
            return self._complete_qianniu_externally_handled(
                task_id,
                reason=str(worker.get("reason") or "平台状态已变化"),
                stage=stage,
                state=dict(worker.get("state") or {}),
            )
        return None

    def _qianniu_worker_external_result(
        self, task_id: int, worker: dict[str, Any], *, stage: str
    ) -> dict[str, Any] | None:
        result = dict(worker.get("result") or {})
        if not worker.get("externally_handled") and not result.get(
            "externally_handled"
        ):
            return None
        return self._complete_qianniu_externally_handled(
            task_id,
            reason=str(worker.get("reason") or result.get("reason") or ""),
            stage=stage,
            state=dict(result.get("state") or worker.get("state") or {}),
        )

    def collect_qianniu_task(
        self, task_id: int, *, cancel_event: threading.Event | None = None
    ) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = dict(task.get("facts") or {})
        source = dict(facts.get("source_evidence") or {})
        if source.get("source_system") != "qianniu_aftersale":
            raise RuntimeError("当前任务不是千牛未收货退款")
        order_no = str(facts.get("order_no") or "")
        aftersale_no = str(source.get("aftersale_no") or facts.get("work_order_no") or "")
        detail_worker = self._run_qianniu_worker(
            operation="collect",
            order_no=order_no,
            aftersale_no=aftersale_no,
            cancel_event=cancel_event,
        )
        if detail_worker.get("externally_handled"):
            return self._complete_qianniu_externally_handled(
                task_id,
                reason=str(detail_worker.get("reason") or ""),
                stage="collect_detail",
                state=dict(detail_worker.get("detail") or {}),
            )
        detail = dict(detail_worker.get("detail") or {})
        runtime = dict(self.config.raw.get("runtime") or {})
        try:
            wdt = run_isolated_qianniu_wdt(
                project_root=self.config.project_root,
                database_path=self.config.database_path,
                evidence_dir=self.config.evidence_dir,
                order_no=order_no,
                tracking_no=str(detail.get("tracking_no") or ""),
                timeout_seconds=float(runtime.get("qianniu_wdt_worker_timeout_seconds", 180)),
                cancel_event=cancel_event,
            )
        except QianniuWdtWorkerError as exc:
            raise RuntimeError(str(exc)) from exc
        facts.update(
            {
                "carrier": str(detail.get("carrier") or ""),
                "tracking_no": str(detail.get("tracking_no") or ""),
                "latest_logistics_text": str(detail.get("latest_logistics_text") or ""),
                "wdt_result_count": 1,
                "wdt_order_id": str(wdt.get("wdt_order_id") or ""),
                "wdt_platform": str(wdt.get("platform") or ""),
                "wdt_order_status": str(wdt.get("order_status") or ""),
                "wdt_warehouse": str(wdt.get("warehouse") or ""),
                "wdt_logistics_company": str(wdt.get("logistics_company") or ""),
                "wdt_product_names": list(wdt.get("product_names") or []),
                "screenshots": list(
                    dict.fromkeys(
                        [
                            *list(facts.get("screenshots") or []),
                            *list(detail_worker.get("evidence") or []),
                        ]
                    )
                ),
            }
        )
        source.update(
            {
                "qianniu_detail": detail,
                "qianniu_wdt": wdt,
                "wdt_original_order_mismatch_ignored": not bool(
                    wdt.get("original_order_matches", False)
                ),
            }
        )
        facts["source_evidence"] = source
        self.repository.update_facts(task_id, facts)
        for path in detail_worker.get("evidence") or []:
            self.repository.add_task_evidence(
                task_id, "qianniu_aftersale_detail", str(path)
            )
        return {
            "ok": True,
            "detail": detail,
            "wdt": wdt,
            "task": self.repository.get_task(task_id),
        }

    def prepare_qianniu_interception(self, task_id: int) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = dict(task.get("facts") or {})
        routing = decide_qianniu_interception(
            {
                "order_no": facts.get("order_no", ""),
                "wdt_logistics_company": facts.get("wdt_logistics_company", ""),
                "wdt_warehouse": facts.get("wdt_warehouse", ""),
                "carrier": facts.get("carrier", ""),
                "tracking_no": facts.get("tracking_no", ""),
                "latest_logistics_text": facts.get("latest_logistics_text", ""),
                "wdt_product_names": facts.get("wdt_product_names", []),
            },
            self.get_in_transit_config(),
        )
        if routing.get("ok"):
            facts.setdefault("source_evidence", {})["interception_route"] = routing
            self.repository.update_facts(task_id, facts)
        return routing

    def _close_qianniu_task_detail(
        self,
        task_id: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            return {"ok": True, "skipped": True, "reason": "task_missing"}
        facts = dict(task.get("facts") or {})
        source = dict(facts.get("source_evidence") or {})
        return self._run_qianniu_worker(
            operation="close_detail",
            order_no=str(facts.get("order_no") or ""),
            aftersale_no=str(
                source.get("aftersale_no") or facts.get("work_order_no") or ""
            ),
            cancel_event=cancel_event,
        )

    def process_qianniu_task(
        self,
        task_id: int,
        *,
        live: bool,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Run one Tmall task and always close its detail tab afterwards."""

        try:
            result = self._process_qianniu_task_inner(
                task_id, live=live, cancel_event=cancel_event
            )
        except Exception as exc:
            current = self.repository.get_task(task_id) or {}
            # NEED_REVIEW after a platform click means the external result is
            # unknown. Preserve that page/popup for inspection; only failures
            # that occurred before a business action may be safely closed.
            preserve_unknown = str(current.get("state") or "") == str(
                TaskState.NEED_REVIEW
            )
            retryable_read = (
                "千牛详情未读取到唯一有效物流单号" in str(exc)
                and not preserve_unknown
            )
            cleanup: dict[str, Any] = {}
            try:
                if preserve_unknown:
                    raise RuntimeError("业务点击结果未知，保留详情现场")
                cleanup = self._close_qianniu_task_detail(
                    task_id, cancel_event=cancel_event
                )
            except Exception as cleanup_exc:
                self.repository.log_action(
                    task_id,
                    f"QIANNIU:{task_id}:DETAIL_CLEANUP",
                    "CLOSE_QIANNIU_DETAIL",
                    "skipped" if preserve_unknown else "failed",
                    {
                        "error": str(cleanup_exc),
                        "preserved_for_review": preserve_unknown,
                        "business_write_performed": False,
                    },
                )
            if retryable_read:
                self.repository.mark_task_status(
                    task_id,
                    TaskState.FAILED,
                    "千牛详情物流单号暂未唯一读取，保留至下一轮重新查询",
                )
                self.repository.log_action(
                    task_id,
                    f"QIANNIU:{task_id}:RETRY_NEXT_SCAN",
                    "QIANNIU_READ_RETRY",
                    "retry_pending",
                    {
                        "error": str(exc),
                        "retry_on_next_discovery": True,
                        "business_write_performed": False,
                    },
                )
                return {
                    "ok": False,
                    "status": "RETRY_PENDING",
                    "reason": "物流单号未唯一读取，下一轮重新查询",
                    "error": str(exc),
                    "detail_cleanup": cleanup,
                    "business_write_performed": False,
                }
            raise
        try:
            cleanup = self._close_qianniu_task_detail(
                task_id, cancel_event=cancel_event
            )
        except Exception as cleanup_exc:
            cleanup = {
                "ok": False,
                "error": str(cleanup_exc),
                "business_write_performed": False,
            }
            self.repository.log_action(
                task_id,
                f"QIANNIU:{task_id}:DETAIL_CLEANUP",
                "CLOSE_QIANNIU_DETAIL",
                "failed",
                cleanup,
            )
        result["detail_cleanup"] = cleanup
        return result

    def _process_qianniu_task_inner(
        self,
        task_id: int,
        *,
        live: bool,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Run one Tmall task; all PDD tasks are ordered before this method."""

        cycle = self._qianniu_cycle_config()
        if live and not cycle.production_enabled:
            raise RuntimeError("千牛正式执行开关未开启")
        collected = self.collect_qianniu_task(task_id, cancel_event=cancel_event)
        if collected.get("externally_handled"):
            return collected
        task = self.repository.get_task(task_id)
        assert task is not None
        facts = dict(task.get("facts") or {})
        source = dict(facts.get("source_evidence") or {})
        order_no = str(facts.get("order_no") or "")
        aftersale_no = str(source.get("aftersale_no") or facts.get("work_order_no") or "")
        detail = dict(collected.get("detail") or {})
        wdt = dict(collected.get("wdt") or {})
        wdt_status = str(wdt.get("order_status") or "").strip()
        # 未揽收分支与“已发货无轨迹”一致：不发群、不登记，直接同意退款。
        if wdt_status not in {"已发货", "已完成"}:
            decision = decide_qianniu_unreceived_refund(
                wdt_order_status=wdt_status,
                wdt_warehouse=str(wdt.get("warehouse") or ""),
                logistics_text=str(detail.get("logistics_text") or ""),
            ).to_dict()
            source["qianniu_decision"] = decision
            facts["source_evidence"] = source
            self.repository.update_facts(task_id, facts)
            if not live:
                return {
                    "ok": True,
                    "status": "READY_FOR_PRODUCTION",
                    "collected": collected,
                    "decision": decision,
                    "business_write_performed": False,
                }
            if decision.get("action") != QIANNIU_AGREE_REFUND:
                self.repository.mark_task_status(
                    task_id,
                    TaskState.NEED_REVIEW,
                    str(decision.get("reason") or "千牛未揽收规则需人工复核"),
                )
                return {"ok": False, "status": "NEED_REVIEW", "decision": decision}
            external = self._revalidate_qianniu_task(
                task_id,
                stage="before_agree_refund",
                cancel_event=cancel_event,
            )
            if external:
                return external
            try:
                worker = self._run_qianniu_worker(
                    operation="execute_agree_refund",
                    order_no=order_no,
                    aftersale_no=aftersale_no,
                    live=True,
                    cancel_event=cancel_event,
                )
            except QianniuWorkerError as exc:
                self.repository.mark_task_status(
                    task_id,
                    TaskState.NEED_REVIEW,
                    "天猫同意退款点击已开始但结果未知，禁止自动重试",
                )
                raise RuntimeError(str(exc)) from exc
            external = self._qianniu_worker_external_result(
                task_id, worker, stage="execute_agree_refund"
            )
            if external:
                return external
            for path in worker.get("evidence") or []:
                self.repository.add_task_evidence(
                    task_id, "qianniu_refund_terminal", str(path)
                )
            self.repository.mark_task_status(
                task_id, TaskState.COMPLETED, "千牛未揽收退款已确认完成"
            )
            return {
                "ok": True,
                "status": "COMPLETED",
                "decision": decision,
                "platform": worker,
            }

        # 已揽收分支：先确定群并发送拦截，再读取悬浮物流轨迹。
        routing = self.prepare_qianniu_interception(task_id)
        if not routing.get("ok"):
            raise RuntimeError(str(routing.get("reason") or "千牛拦截路由失败"))
        if live:
            external = self._revalidate_qianniu_task(
                task_id,
                stage="before_group_dispatch",
                cancel_event=cancel_event,
            )
            if external:
                return external
        dispatch = (
            self.dispatch_in_transit_interception(
                task_id, routing, cancel_event=cancel_event
            )
            if live
            else {"ok": True, "preview": True, "business_write_performed": False}
        )
        trajectory_worker = self._run_qianniu_worker(
            operation="read_trajectory",
            order_no=order_no,
            aftersale_no=aftersale_no,
            cancel_event=cancel_event,
        )
        trajectory = dict(trajectory_worker.get("trajectory") or {})
        task = self.repository.get_task(task_id)
        assert task is not None
        facts = dict(task.get("facts") or {})
        facts["logistics_trajectory_count"] = int(
            trajectory.get("logistics_trajectory_count") or 0
        )
        facts["screenshots"] = list(
            dict.fromkeys(
                [
                    *list(facts.get("screenshots") or []),
                    *list(trajectory_worker.get("evidence") or []),
                ]
            )
        )
        source = dict(facts.get("source_evidence") or {})
        source["qianniu_logistics_trajectory"] = trajectory
        decision = decide_qianniu_unreceived_refund(
            wdt_order_status=wdt_status,
            wdt_warehouse=str(wdt.get("warehouse") or ""),
            logistics_text=str(detail.get("logistics_text") or ""),
            logistics_trajectory_count=int(
                trajectory.get("logistics_trajectory_count") or 0
            ),
        ).to_dict()
        source["qianniu_decision"] = decision
        facts["source_evidence"] = source
        self.repository.update_facts(task_id, facts)
        for path in trajectory_worker.get("evidence") or []:
            self.repository.add_task_evidence(
                task_id, "qianniu_logistics_trajectory", str(path)
            )
        if not live:
            return {
                "ok": True,
                "status": "READY_FOR_PRODUCTION",
                "collected": collected,
                "routing": routing,
                "dispatch": dispatch,
                "trajectory": trajectory,
                "decision": decision,
                "business_write_performed": False,
            }
        if decision.get("action") == QIANNIU_AGREE_REFUND:
            external = self._revalidate_qianniu_task(
                task_id,
                stage="before_agree_refund",
                cancel_event=cancel_event,
            )
            if external:
                return external
            try:
                platform = self._run_qianniu_worker(
                    operation="execute_agree_refund",
                    order_no=order_no,
                    aftersale_no=aftersale_no,
                    live=True,
                    cancel_event=cancel_event,
                )
            except QianniuWorkerError as exc:
                self.repository.mark_task_status(
                    task_id,
                    TaskState.NEED_REVIEW,
                    "天猫同意退款点击已开始但结果未知，禁止自动重试",
                )
                raise RuntimeError(str(exc)) from exc
            external = self._qianniu_worker_external_result(
                task_id, platform, stage="execute_agree_refund"
            )
            if external:
                return external
            for path in platform.get("evidence") or []:
                self.repository.add_task_evidence(
                    task_id, "qianniu_refund_terminal", str(path)
                )
            # Only a completed refund enters DingTalk; interception-only tasks
            # never create a sheet row.
            task = self.repository.get_task(task_id)
            assert task is not None
            register = self._upsert_routed_interception_register(
                task_id=task_id,
                facts=dict(task.get("facts") or {}),
                refund_reason="天猫未收货退款-已揽收五条以内",
            )
            dingtalk = self.sync_dingtalk_interception_register(int(register["id"]))
            if not dingtalk.get("ok"):
                self.repository.mark_task_status(
                    task_id,
                    TaskState.NEED_REVIEW,
                    "天猫退款已完成，但钉钉在线表格登记待人工复核；禁止重复退款",
                )
                return {
                    "ok": False,
                    "status": "NEED_REVIEW_DINGTALK_AFTER_REFUND",
                    "decision": decision,
                    "dispatch": dispatch,
                    "platform": platform,
                    "dingtalk_register": register,
                    "dingtalk": dingtalk,
                }
            self.repository.mark_task_status(
                task_id,
                TaskState.COMPLETED,
                "群拦截、天猫五条以内同意退款及钉钉登记已完成",
            )
            return {
                "ok": True,
                "status": "COMPLETED",
                "decision": decision,
                "dispatch": dispatch,
                "trajectory": trajectory,
                "platform": platform,
                "dingtalk_register": register,
                "dingtalk": dingtalk,
            }
        if decision.get("action") != QIANNIU_INTERCEPT:
            self.repository.mark_task_status(
                task_id,
                TaskState.NEED_REVIEW,
                str(decision.get("reason") or "千牛物流轨迹需人工复核"),
            )
            return {
                "ok": False,
                "status": "NEED_REVIEW",
                "decision": decision,
                "dispatch": dispatch,
                "trajectory": trajectory,
            }
        external = self._revalidate_qianniu_task(
            task_id,
            stage="before_intercept_express",
            cancel_event=cancel_event,
        )
        if external:
            return external
        try:
            platform = self._run_qianniu_worker(
                operation="execute_intercept",
                order_no=order_no,
                aftersale_no=aftersale_no,
                live=True,
                cancel_event=cancel_event,
            )
        except QianniuWorkerError as exc:
            self.repository.mark_task_status(
                task_id,
                TaskState.NEED_REVIEW,
                "天猫拦截快递点击已开始但结果未知，禁止自动重试",
            )
            raise RuntimeError(str(exc)) from exc
        external = self._qianniu_worker_external_result(
            task_id, platform, stage="execute_intercept"
        )
        if external:
            return external
        for path in platform.get("evidence") or []:
            self.repository.add_task_evidence(
                task_id, "qianniu_intercept_terminal", str(path)
            )
        task = self.repository.get_task(task_id)
        assert task is not None
        facts = dict(task.get("facts") or {})
        source = dict(facts.get("source_evidence") or {})
        platform_result = dict(platform.get("result") or {})
        source["qianniu_platform_feedback"] = {
            "terminal": str(platform_result.get("terminal") or ""),
            "terminal_feedback": str(
                platform_result.get("terminal_feedback") or ""
            ),
            "verified": bool(
                dict(platform_result.get("confirmation") or {}).get(
                    "terminal_verified"
                )
            ),
            "business_write_performed": bool(
                platform.get("business_write_performed")
            ),
        }
        facts["source_evidence"] = source
        self.repository.update_facts(task_id, facts)
        self.repository.mark_task_status(
            task_id,
            TaskState.COMPLETED,
            "群拦截及天猫拦截快递已完成；未登记钉钉，后续退款由人工处理",
        )
        return {
            "ok": True,
            "status": "COMPLETED",
            "decision": decision,
            "dispatch": dispatch,
            "trajectory": trajectory,
            "platform": platform,
            "manual_refund_followup": True,
            "dingtalk_registration_skipped": True,
        }

    def open_shop_windows(self) -> dict[str, Any]:
        """Open every operational browser window without performing business writes."""
        shops = [
            {"id": shop.id, "name": shop.name, "edge_profile": shop.edge_profile}
            for shop in self.config.shops
            if shop.enabled
        ]
        results: list[dict[str, Any]] = []
        shop_results = self.edge_launcher.open_shops(shops)
        for item in shop_results:
            results.append({"kind": "pdd_shop", **item})

        external_windows = (
            (
                "qianniu",
                QianniuEdgeConfig.from_raw(
                    self.config.raw.get("qianniu_shop_browser") or {}
                ),
                QianniuEdgeWindow,
                QianniuEdgeError,
            ),
            (
                "yto",
                YtoServiceChatConfig.from_raw(
                    self.config.raw.get("yto_service_chat") or {}
                ),
                YtoServiceChatWriter,
                YtoServiceChatError,
            ),
        )
        for kind, external_config, writer_type, error_type in external_windows:
            if not external_config.enabled:
                results.append(
                    {
                        "kind": kind,
                        "ok": False,
                        "action": "disabled",
                        "opened": False,
                        "verified": False,
                        "note": "配置未启用",
                    }
                )
                continue
            try:
                writer = writer_type(external_config, self.config.evidence_dir)
                opened = writer.open_window()
                results.append({"kind": kind, **opened})
            except error_type as exc:
                results.append(
                    {
                        "kind": kind,
                        "ok": False,
                        "action": "failed",
                        "opened": False,
                        "verified": False,
                        "profile": external_config.edge_profile,
                        "note": str(exc),
                        "write_performed": False,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "kind": kind,
                        "ok": False,
                        "action": "failed",
                        "opened": False,
                        "verified": False,
                        "profile": external_config.edge_profile,
                        "note": f"窗口启动异常：{exc}",
                        "write_performed": False,
                    }
                )
        dingtalk_root = dict(
            self.config.raw.get("dingtalk_interception_register") or {}
        )
        if not bool(dingtalk_root.get("enabled", False)):
            results.append(
                {
                    "kind": "dingtalk",
                    "ok": False,
                    "action": "disabled",
                    "opened": False,
                    "verified": False,
                    "note": "配置未启用",
                    "write_performed": False,
                }
            )
        else:
            try:
                target_configs = iter_dingtalk_target_configs(dingtalk_root)
            except DingTalkInterceptionRegisterError as exc:
                target_configs = []
                results.append(
                    {
                        "kind": "dingtalk",
                        "ok": False,
                        "action": "failed",
                        "opened": False,
                        "verified": False,
                        "note": str(exc),
                        "write_performed": False,
                    }
                )
            for target_key, target_raw in target_configs:
                try:
                    external_config = DingTalkInterceptionRegisterConfig.from_raw(
                        target_raw
                    )
                    writer = DingTalkInterceptionRegisterWriter(
                        external_config, self.config.evidence_dir
                    )
                    opened = writer.open_window()
                    results.append(
                        {"kind": "dingtalk", "target_key": target_key, **opened}
                    )
                except DingTalkInterceptionRegisterError as exc:
                    results.append(
                        {
                            "kind": "dingtalk",
                            "target_key": target_key,
                            "ok": False,
                            "action": "failed",
                            "opened": False,
                            "verified": False,
                            "profile": str(target_raw.get("edge_profile") or ""),
                            "note": str(exc),
                            "write_performed": False,
                        }
                    )
                except Exception as exc:
                    results.append(
                        {
                            "kind": "dingtalk",
                            "target_key": target_key,
                            "ok": False,
                            "action": "failed",
                            "opened": False,
                            "verified": False,
                            "profile": str(target_raw.get("edge_profile") or ""),
                            "note": f"窗口启动异常：{exc}",
                            "write_performed": False,
                        }
                    )
        failed = [
            item
            for item in results
            if not item.get("ok", item.get("verified", False))
            or not item.get("verified", False)
            or item.get("action") in {"failed", "reuse_unverified", "disabled"}
        ]
        self.repository.log_action(
            None,
            f"OPEN_SHOP_WINDOWS:{int(time.time())}",
            "OPEN_SHOP_WINDOWS",
            "failed" if failed else "success",
            {
                "results": results,
                "business_write_performed": False,
                "external_windows": ["qianniu", "yto", "dingtalk"],
            },
        )
        opened = sum(
            1
            for item in results
            if item.get("opened")
            or (
                item.get("verified")
                and item.get("action") in {"reuse", "reuse_restored"}
            )
        )
        return {
            "ok": not failed,
            "results": results,
            "summary": {
                "pdd_shop_count": len(shops),
                "opened_or_reused": opened,
                "failed": len(failed),
                "business_write_performed": False,
            },
        }

    def collect_task_detail_readonly(
        self,
        task_id: int,
        *,
        keep_detail_open: bool = False,
        require_logistics: bool = True,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if self.repository.is_paused():
            raise RuntimeError("系统已全局暂停")
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = task["facts"]
        if facts.get("source_evidence", {}).get("collection_mode") not in {"edge_readonly", "edge_uia"}:
            raise RuntimeError("仅真实 Edge 工单允许采集详情")
        pdd_config = self.config.raw.get("pdd_readonly", {})
        runtime_config = self.config.raw.get("runtime", {})
        isolated = bool(
            pdd_config.get(
                "detail_worker_isolation",
                runtime_config.get("detail_worker_isolation", True),
            )
        )
        shop = next(
            (
                {
                    "id": item.id,
                    "name": item.name,
                    "edge_profile": item.edge_profile,
                }
                for item in self.config.shops
                if item.id == facts.get("shop_id") and item.enabled
            ),
            None,
        )
        if not shop:
            raise KeyError(f"店铺不存在或已停用：{facts.get('shop_id')}")
        detail_execution_id = f"PDD_DETAIL:{task_id}:{uuid.uuid4().hex[:12]}"
        if isolated:
            self.repository.log_action(
                task_id,
                f"{detail_execution_id}:worker_started",
                "PDD_DETAIL_WORKER_LIFECYCLE",
                "started",
                {
                    "execution_id": detail_execution_id,
                    "isolated": True,
                    "keep_detail_open": bool(keep_detail_open),
                    "business_write_performed": False,
                },
            )
            try:
                payload = run_isolated_pdd_detail(
                    project_root=self.config.project_root,
                    evidence_dir=self.config.evidence_dir,
                    config=pdd_config,
                    shop=shop,
                    order_no=str(facts["order_no"]),
                    keep_detail_open=keep_detail_open,
                    require_logistics=require_logistics,
                    timeout_seconds=float(
                        pdd_config.get(
                            "detail_worker_timeout_seconds",
                            runtime_config.get("detail_worker_timeout_seconds", 180),
                        )
                    ),
                    cancel_event=cancel_event,
                )
            except PddDetailWorkerError as exc:
                self.repository.log_action(
                    task_id,
                    f"{detail_execution_id}:worker_failed",
                    "PDD_DETAIL_WORKER_LIFECYCLE",
                    "failed",
                    {
                        "execution_id": detail_execution_id,
                        "isolated": True,
                        "returncode": exc.returncode,
                        "native_crash": exc.crashed,
                        "error_type": exc.error_type,
                        "error": str(exc),
                        "business_write_performed": False,
                    },
                )
                if exc.error_type == "already_processed":
                    raise PddAlreadyProcessedError(str(exc)) from exc
                if exc.error_type == "not_found":
                    raise PddOrderNotFoundError(
                        f"订单 {facts['order_no']} 已不在当前待处理列表，按已由人工处理跳过"
                    ) from exc
                raise RuntimeError(str(exc)) from exc
            result = DetailCollectionResult(
                shop_id=str(payload.get("shop_id") or facts["shop_id"]),
                order_no=str(payload.get("order_no") or facts["order_no"]),
                detail_url=str(payload.get("detail_url") or ""),
                tracking_no=str(payload.get("tracking_no") or ""),
                carrier=str(payload.get("carrier") or ""),
                latest_logistics_text=str(payload.get("latest_logistics_text") or ""),
                refund_amount=float(payload.get("refund_amount") or 0),
                evidence_path=str(payload.get("evidence_path") or ""),
                list_page_number=int(payload.get("list_page_number") or 1),
            )
            self.repository.log_action(
                task_id,
                f"{detail_execution_id}:worker_success",
                "PDD_DETAIL_WORKER_LIFECYCLE",
                "success",
                {
                    "execution_id": detail_execution_id,
                    "isolated": True,
                    "keep_detail_open": bool(keep_detail_open),
                    "detail_url": result.detail_url,
                    "business_write_performed": False,
                },
            )
        else:
            result = self.detail_collector.collect(
                facts["shop_id"],
                facts["order_no"],
                keep_detail_open=keep_detail_open,
                require_logistics=require_logistics,
            )
        facts["tracking_no"] = result.tracking_no
        if result.carrier:
            facts["carrier"] = result.carrier
        if result.latest_logistics_text:
            facts["latest_logistics_text"] = result.latest_logistics_text
        if result.refund_amount > 0:
            facts["refund_amount"] = result.refund_amount
        screenshots = list(facts.get("screenshots") or [])
        if result.evidence_path not in screenshots:
            screenshots.append(result.evidence_path)
        facts["screenshots"] = screenshots
        facts.setdefault("source_evidence", {}).update(
            {
                "detail_url": result.detail_url,
                "detail_list_page_number": result.list_page_number,
                "detail_collection_mode": "edge_uia",
            }
        )
        self.repository.update_facts(task_id, facts)
        if result.evidence_path:
            self.repository.add_task_evidence(
                task_id,
                "pdd_work_order_detail",
                result.evidence_path,
            )
        self.repository.log_action(
            task_id,
            f"{task['business_key']}:DETAIL_READONLY:{int(time.time())}",
            "RUN_WORKFLOW_DETAIL_READONLY",
            "success",
            {
                "workflow": [
                    "ActivateWindow",
                    "AssertShopIdentity",
                    "FindWorkOrderCard",
                    "ClickElement:立即处理",
                    "ReadDetailFields" if require_logistics else "VerifyDetailIdentity",
                    "SaveEvidence",
                    "CloseDetailTab:after_task",
                ],
                **result.to_dict(),
                "detail_retained": bool(keep_detail_open),
            },
        )
        return {
            "ok": True,
            "task": self.repository.get_task(task_id),
            **result.to_dict(),
            "detail_retained": bool(keep_detail_open),
        }

    def _aftersale_shop(self, facts: dict[str, Any]) -> dict[str, Any]:
        shop = next(
            (
                {"id": item.id, "name": item.name, "edge_profile": item.edge_profile}
                for item in self.config.shops
                if item.id == facts.get("shop_id") and item.enabled
            ),
            None,
        )
        if not shop:
            raise KeyError(f"店铺不存在或已停用：{facts.get('shop_id')}")
        return shop

    def collect_aftersale_detail(
        self,
        task_id: int,
        *,
        keep_detail_open: bool = True,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = dict(task["facts"])
        source_evidence = facts.get("source_evidence") or {}
        if source_evidence.get("source_system") != "aftersale_workbench":
            raise RuntimeError("当前任务不是售后工作台任务")
        queue_scope = str(source_evidence.get("queue_scope") or "").strip()
        if queue_scope and queue_scope != "pending_soon_overdue":
            raise RuntimeError("仅支持售后工作台“待处理即将逾期”任务的详情读取")
        shop = self._aftersale_shop(facts)
        runtime = dict(self.config.raw.get("runtime") or {})
        execution_id = f"AFTERSALE_DETAIL:{task_id}:{uuid.uuid4().hex[:12]}"
        self.repository.log_action(
            task_id,
            f"{execution_id}:started",
            "AFTERSALE_WORKER_LIFECYCLE",
            "started",
            {"operation": "collect", "business_write_performed": False},
        )
        try:
            payload = run_isolated_aftersale(
                project_root=self.config.project_root,
                evidence_dir=self.config.evidence_dir,
                config=dict(self.config.raw.get("pdd_readonly") or {}),
                shop=shop,
                operation="collect",
                order_no=str(facts.get("order_no") or ""),
                aftersale_no=str(
                    facts.get("aftersale_no")
                    or (
                        ((facts.get("source_evidence") or {}).get("aftersale_card") or {}).get(
                            "aftersale_no"
                        )
                    )
                    or ""
                ),
                facts=facts,
                keep_detail_open=keep_detail_open,
                timeout_seconds=float(runtime.get("aftersale_worker_timeout_seconds", 180)),
                cancel_event=cancel_event,
            )
        except AftersaleWorkerError as exc:
            self.repository.log_action(
                task_id,
                f"{execution_id}:failed",
                "AFTERSALE_WORKER_LIFECYCLE",
                "failed",
                {
                    "operation": "collect",
                    "error": str(exc),
                    "native_crash": exc.crashed,
                    "business_write_performed": False,
                },
            )
            raise RuntimeError(str(exc)) from exc
        detail = dict(payload.get("facts") or {})
        facts.update(detail)
        facts["aftersale_detail_url"] = str(payload.get("detail_url") or "")
        facts["aftersale_decision"] = dict(payload.get("decision") or {})
        facts["screenshots"] = list(
            dict.fromkeys([*list(facts.get("screenshots") or []), *list(payload.get("evidence") or [])])
        )
        facts.setdefault("source_evidence", {})["aftersale_detail"] = {
            "detail_url": facts["aftersale_detail_url"],
            "decision": facts["aftersale_decision"],
            "detail_cleanup": dict(payload.get("detail_cleanup") or {}),
            "business_write_performed": False,
        }
        self.repository.update_facts(task_id, facts)
        for path in {
            str(item).strip()
            for item in (payload.get("evidence") or [])
            if str(item).strip()
        }:
            self.repository.add_task_evidence(
                task_id,
                "pdd_aftersale_detail",
                path,
            )
        self.repository.log_action(
            task_id,
            f"{execution_id}:success",
            "AFTERSALE_WORKER_LIFECYCLE",
            "success",
            {
                "operation": "collect",
                "detail_url": facts["aftersale_detail_url"],
                "decision": facts["aftersale_decision"],
                "business_write_performed": False,
            },
        )
        return {
            "ok": True,
            "facts": facts,
            "decision": dict(facts["aftersale_decision"]),
            "task": self.repository.get_task(task_id),
            "detail_cleanup": dict(payload.get("detail_cleanup") or {}),
            "detail_retained": bool(payload.get("detail_retained")),
            "business_write_performed": False,
        }

    def complete_aftersale_action(
        self,
        task_id: int,
        action: str,
        *,
        live: bool,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = dict(task["facts"])
        shop = self._aftersale_shop(facts)
        if live:
            try:
                assert_action_allowed(
                    action,
                    self.config.execution_mode,
                    self.config.live_actions_enabled,
                    approved=True,
                )
            except SafetyViolation as exc:
                raise RuntimeError(str(exc)) from exc
        runtime = dict(self.config.raw.get("runtime") or {})
        payload = run_isolated_aftersale(
            project_root=self.config.project_root,
            evidence_dir=self.config.evidence_dir,
            config=dict(self.config.raw.get("pdd_readonly") or {}),
            shop=shop,
            operation="execute",
            facts=facts,
            action=action,
            live=live,
            timeout_seconds=float(runtime.get("aftersale_worker_timeout_seconds", 180)),
            cancel_event=cancel_event,
        )
        result = dict(payload.get("result") or payload)
        deferred = bool(payload.get("deferred") or result.get("deferred"))
        write_outcome_unknown = bool(
            payload.get("write_outcome_unknown")
            or result.get("write_outcome_unknown")
        )
        skipped = bool(payload.get("skipped") or result.get("skipped"))
        result_ok = bool(payload.get("ok", True)) and bool(result.get("ok", True))
        business_write_performed = bool(
            payload.get("business_write_performed")
            or result.get("business_write_performed")
        )
        if deferred:
            outcome_status = "deferred"
        elif write_outcome_unknown:
            outcome_status = "uncertain"
        elif skipped:
            outcome_status = "skipped"
        elif not result_ok:
            outcome_status = "failed"
        else:
            outcome_status = "success"

        # A cooldown is not a completed business action.  Likewise, when the
        # UI worker cannot prove the post-submit state, persisting a final
        # action would make a later recovery look like a confirmed success.
        if outcome_status == "success":
            facts["aftersale_final_action"] = action
        facts["aftersale_business_write_performed"] = business_write_performed
        facts["aftersale_last_action_attempt"] = {
            "action": action,
            "status": outcome_status,
            "deferred": deferred,
            "write_outcome_unknown": write_outcome_unknown,
            "business_write_performed": business_write_performed,
            "submit_attempted": bool(result.get("submit_attempted")),
            "reason_code": str(result.get("reason_code") or ""),
            "defer_kind": str(result.get("defer_kind") or ""),
            "stage_committed": bool(result.get("stage_committed")),
            "negotiation_round_submitted": bool(
                result.get("negotiation_round_submitted")
            ),
            "reject_surface": str(result.get("reject_surface") or ""),
            "retry_after_seconds": int(result.get("retry_after_seconds") or 0),
            "prompt_text": str(result.get("prompt_text") or ""),
        }
        evidence_paths = [
            str(item).strip()
            for item in payload.get("evidence") or []
            if str(item).strip()
        ]
        facts["screenshots"] = list(
            dict.fromkeys([*list(facts.get("screenshots") or []), *evidence_paths])
        )
        self.repository.update_facts(task_id, facts)
        for path in evidence_paths:
            self.repository.add_task_evidence(task_id, "pdd_aftersale_action", path)
        self.repository.log_action(
            task_id,
            f"{task['business_key']}:AFTERSALE_FINAL:{int(time.time())}",
            "AFTERSALE_FINAL_ACTION",
            outcome_status,
            {
                "action": action,
                "result": result,
                "refund_amount": float(facts.get("refund_amount") or 0),
                "deferred": deferred,
                "write_outcome_unknown": write_outcome_unknown,
                "business_write_performed": business_write_performed,
            },
        )
        return {
            "ok": result_ok and not write_outcome_unknown,
            "action": action,
            "result": result,
            "detail_cleanup": dict(payload.get("detail_cleanup") or {}),
            "deferred": deferred,
            "write_outcome_unknown": write_outcome_unknown,
            "business_write_performed": business_write_performed,
        }

    def close_aftersale_detail(
        self, task_id: int, *, cancel_event: threading.Event | None = None
    ) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = dict(task["facts"])
        payload = run_isolated_aftersale(
            project_root=self.config.project_root,
            evidence_dir=self.config.evidence_dir,
            config=dict(self.config.raw.get("pdd_readonly") or {}),
            shop=self._aftersale_shop(facts),
            operation="close",
            facts=facts,
            timeout_seconds=float(
                (self.config.raw.get("runtime") or {}).get(
                    "aftersale_worker_close_timeout_seconds", 60
                )
            ),
            cancel_event=cancel_event,
        )
        return dict(payload.get("detail_cleanup") or {})

    def close_active_shop_detail(self, shop_id: str) -> dict[str, Any]:
        """Leave the shop's Edge window on its list tab between queue items."""
        clear = getattr(self.refund_workflow, "clear_prepared_detail", None)
        try:
            pdd_config = self.config.raw.get("pdd_readonly", {})
            runtime_config = self.config.raw.get("runtime", {})
            isolated = bool(
                pdd_config.get(
                    "detail_worker_isolation",
                    runtime_config.get("detail_worker_isolation", True),
                )
            )
            shop = next(
                (
                    {
                        "id": item.id,
                        "name": item.name,
                        "edge_profile": item.edge_profile,
                    }
                    for item in self.config.shops
                    if item.id == shop_id and item.enabled
                ),
                None,
            )
            if not shop:
                raise KeyError(f"店铺不存在或已停用：{shop_id}")
            if isolated:
                return run_isolated_pdd_close(
                    project_root=self.config.project_root,
                    evidence_dir=self.config.evidence_dir,
                    config=pdd_config,
                    shop=shop,
                    timeout_seconds=float(
                        pdd_config.get(
                            "detail_worker_close_timeout_seconds",
                            runtime_config.get("detail_worker_close_timeout_seconds", 60),
                        )
                    ),
                )
            return self.detail_collector.close_active_detail(shop_id)
        finally:
            if callable(clear):
                clear(shop_id)

    def check_wdt_by_components(
        self,
        task_id: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if self.repository.is_paused():
            raise RuntimeError("系统已全局暂停")
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = task["facts"]
        title = str(facts.get("work_order_title") or "")
        is_shipped = title == "已发货无轨迹退款处理"
        is_aftersale_shipped = title == AFTERSALE_SHIPPED_REFUND
        is_in_transit = title == IN_TRANSIT_NO_REASON_TITLE
        if not (is_shipped or is_in_transit or is_aftersale_shipped):
            raise RuntimeError("当前组件流程不支持该任务类型")
        if not facts.get("tracking_no"):
            raise RuntimeError("请先运行详情采集组件读取物流单号")
        context = {"tracking_no": facts["tracking_no"], "order_no": facts["order_no"]}
        workflow_id = (
            "WF-WDT-SHIPPED-NO-TRACE-CHECK"
            if is_shipped
            else "WF-WDT-IN-TRANSIT-LOOKUP"
        )
        execution_id = f"WDT:{task_id}:{uuid.uuid4().hex[:12]}"
        runtime_config = self.config.raw.get("runtime", {})
        isolated = bool(runtime_config.get("wdt_worker_isolation", True))
        if isolated:
            self.repository.log_action(
                task_id,
                f"{execution_id}:worker_started",
                "WDT_WORKER_LIFECYCLE",
                "started",
                {"workflow_id": workflow_id, "execution_id": execution_id, "isolated": True},
            )
            try:
                result = run_isolated_wdt_workflow(
                    project_root=self.config.project_root,
                    database_path=self.config.database_path,
                    evidence_dir=self.config.evidence_dir,
                    workflow_id=workflow_id,
                    task_id=task_id,
                    execution_id=execution_id,
                    context=context,
                    timeout_seconds=float(runtime_config.get("wdt_worker_timeout_seconds", 180)),
                    cancel_event=cancel_event,
                )
            except WdtWorkerError as exc:
                self.repository.log_action(
                    task_id,
                    f"{execution_id}:worker_failed",
                    "WDT_WORKER_LIFECYCLE",
                    "failed",
                    {
                        "workflow_id": workflow_id,
                        "execution_id": execution_id,
                        "isolated": True,
                        "returncode": exc.returncode,
                        "native_crash": exc.crashed,
                        "error": str(exc),
                    },
                )
                raise RuntimeError(str(exc)) from exc
            self.repository.log_action(
                task_id,
                f"{execution_id}:worker_success",
                "WDT_WORKER_LIFECYCLE",
                "success",
                {"workflow_id": workflow_id, "execution_id": execution_id, "isolated": True},
            )
        else:
            result = self.desktop_workflows.run(
                workflow_id,
                context,
                task_id=task_id,
                execution_id=execution_id,
            )
        worker_context = result.get("context") if isinstance(result, dict) else None
        if isinstance(worker_context, dict):
            # The isolated worker owns a separate process, so its mutations do
            # not update the parent dictionary.  Merge the returned context
            # before persisting facts or reading wdt_order_status below.
            context = {**context, **worker_context}
        # The in-transit route intentionally does not use WDT order status:
        # it dispatches from the exact carrier result and decides the final
        # PDD action from the latest logistics city.  Do not reject a valid
        # in-transit lookup merely because that unused painted column was not
        # read (or was obscured by a WDT modal).
        if is_shipped and not str(context.get("wdt_order_status", "")).strip():
            raise RuntimeError("旺店通 Worker 未返回订单状态")

        # Do not touch Edge UIA from the API process after the isolated WDT
        # worker returns.  The post-WDT PDD activation is performed by the
        # isolated refund worker from preview_refund_components().
        facts.update(
            {
                "wdt_order_id": context.get("wdt_order_id", ""),
                "wdt_platform": context.get("wdt_platform", ""),
                "wdt_order_status": context.get("wdt_order_status", ""),
                "wdt_warehouse": context.get("wdt_warehouse", ""),
                "wdt_logistics_company": context.get("wdt_logistics_company", ""),
                "wdt_product_names": [
                    item.strip()
                    for item in str(context.get("wdt_product_names") or "").splitlines()
                    if item.strip()
                ],
                "wdt_result_count": 1,
                "wdt_eligible": bool(context.get("wdt_eligible")),
                "wdt_requires_interception": bool(
                    is_aftersale_shipped
                    or context.get("wdt_requires_interception")
                ),
            }
        )
        facts.setdefault("source_evidence", {})["wdt_component_workflow"] = {
            "workflow_id": result["workflow_id"],
            "trace": result["trace"],
            "business_write_performed": False,
        }
        aftersale_decision: dict[str, Any] | None = None
        if is_aftersale_shipped:
            # The initial detail decision is intentionally provisional because
            # WDT has not supplied a warehouse yet. Recalculate immediately
            # after the verified lookup so 聚水潭 uses <=1 while 俊富 keeps <=3.
            aftersale_decision = decide_aftersale(facts).to_dict()
            facts["aftersale_decision"] = dict(aftersale_decision)
            facts["aftersale_agree_refund_trace_limit"] = (
                shipped_refund_trace_limit(facts)
            )
            facts["source_evidence"]["aftersale_warehouse_threshold"] = {
                "warehouse": str(facts.get("wdt_warehouse") or ""),
                "max_agree_refund_trace_count": int(
                    facts["aftersale_agree_refund_trace_limit"]
                ),
                "decision": dict(aftersale_decision),
                "business_write_performed": False,
            }
        self.repository.update_facts(task_id, facts)
        self.repository.log_action(
            task_id,
            f"{task['business_key']}:WDT_COMPONENT:{int(time.time())}",
            "RUN_WDT_COMPONENT_WORKFLOW",
            "success",
            {
                "workflow_id": result["workflow_id"],
                "trace": result["trace"],
                "business_write_performed": False,
                "aftersale_decision": aftersale_decision,
            },
        )
        return {
            "ok": True,
            "eligible": bool(context.get("wdt_eligible")),
            "requires_interception": bool(
                is_aftersale_shipped
                or context.get("wdt_requires_interception")
            ),
            "order_status": context.get("wdt_order_status", ""),
            "warehouse": context.get("wdt_warehouse", ""),
            "logistics_company": context.get("wdt_logistics_company", ""),
            "product_names": [
                item.strip()
                for item in str(context.get("wdt_product_names") or "").splitlines()
                if item.strip()
            ],
            "aftersale_decision": aftersale_decision,
            "max_agree_refund_trace_count": (
                shipped_refund_trace_limit(facts)
                if is_aftersale_shipped
                else None
            ),
            "task": self.repository.get_task(task_id),
            "trace": result["trace"],
        }

    def _run_isolated_pdd_refund_stage(
        self,
        task_id: int,
        facts: dict[str, Any],
        *,
        operation: str,
        confirm_order: str = "",
        confirm_amount: float = 0,
        reason_text: str = "",
        action: str = "",
        routing_config: dict[str, Any] | None = None,
        live: bool = False,
        reconcile_after_execute: bool = False,
        close_detail_after_execute: bool = False,
    ) -> dict[str, Any]:
        """Run retained-detail PDD UIA work outside the API process.

        The WDT and PDD workers are deliberately separate.  The important
        boundary is that the FastAPI process never creates a pywinauto/UIA
        object after a WDT worker has returned; this prevents the native
        ``_ctypes.pyd`` access violation observed in production.
        """
        runtime_config = self.config.raw.get("runtime", {})
        execution_id = f"PDD_REFUND:{task_id}:{uuid.uuid4().hex[:12]}"
        timeout_seconds = float(
            runtime_config.get("pdd_refund_worker_timeout_seconds", 180)
        )
        self.repository.log_action(
            task_id,
            f"{execution_id}:worker_started",
            "PDD_REFUND_WORKER_LIFECYCLE",
            "started",
            {
                "operation": operation,
                "execution_id": execution_id,
                "isolated": True,
                "business_write_performed": operation in {
                    "execute",
                    "in_transit_complete",
                    "confirm_earlier_shipping",
                } and live,
            },
        )
        try:
            payload = run_isolated_pdd_refund_stage(
                project_root=self.config.project_root,
                database_path=self.config.database_path,
                evidence_dir=self.config.evidence_dir,
                pdd_config=self.config.raw.get("pdd_readonly", {}),
                facts=facts,
                operation=operation,
                confirm_order=confirm_order,
                confirm_amount=confirm_amount,
                reason_text=reason_text,
                action=action,
                routing_config=routing_config,
                live=live,
                reconcile_after_execute=reconcile_after_execute,
                close_detail_after_execute=close_detail_after_execute,
                timeout_seconds=timeout_seconds,
            )
        except PddRefundWorkerError as exc:
            self.repository.log_action(
                task_id,
                f"{execution_id}:worker_failed",
                "PDD_REFUND_WORKER_LIFECYCLE",
                "failed",
                {
                    "operation": operation,
                    "execution_id": execution_id,
                    "isolated": True,
                    "returncode": exc.returncode,
                    "native_crash": exc.crashed,
                    "error": str(exc),
                    "business_write_performed": False,
                },
            )
            raise RuntimeError(str(exc)) from exc
        self.repository.log_action(
            task_id,
            f"{execution_id}:worker_success",
            "PDD_REFUND_WORKER_LIFECYCLE",
            "success",
            {
                "operation": operation,
                "execution_id": execution_id,
                "isolated": True,
                "business_write_performed": operation in {
                    "execute",
                    "in_transit_complete",
                    "confirm_earlier_shipping",
                } and live,
            },
        )
        return payload

    def complete_earlier_shipping(
        self, task_id: int, *, live: bool = False
    ) -> dict[str, Any]:
        """Submit the exact earlier-shipping option without a WDT lookup."""
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = task["facts"]
        if str(facts.get("work_order_title") or "") != EARLIER_SHIPPING_TITLE:
            raise RuntimeError("当前工单不是确认更早发货时间")
        if live:
            if not self.config.live_actions_enabled:
                raise RuntimeError("live_actions_enabled 尚未开启")
            if str(self.config.execution_mode) != "auto":
                raise RuntimeError("execution_mode 不是 auto")
        worker = self._run_isolated_pdd_refund_stage(
            task_id,
            facts,
            operation="confirm_earlier_shipping",
            live=live,
            close_detail_after_execute=bool(live),
        )
        result = dict(worker.get("result") or {})
        detail_cleanup = worker.get("detail_cleanup")
        if isinstance(detail_cleanup, dict):
            result["detail_cleanup"] = dict(detail_cleanup)
        if live and result.get("ok"):
            facts["earlier_shipping_confirmed"] = True
            facts["earlier_shipping_option"] = str(result.get("selected_option") or "")
            self.repository.update_facts(task_id, facts)
        self.repository.log_action(
            task_id,
            f"{task['business_key']}:CONFIRM_EARLIER_SHIPPING:{int(time.time())}",
            "CONFIRM_EARLIER_SHIPPING",
            "success",
            result,
        )
        return result

    def preview_refund_components(
        self, task_id: int, *, retain_detail: bool = True
    ) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = task["facts"]
        if facts.get("work_order_title") != "已发货无轨迹退款处理":
            raise RuntimeError("退款组件流程与工单类型不匹配")
        if not facts.get("wdt_eligible"):
            raise RuntimeError("旺店通组件流程尚未通过")
        if retain_detail:
            worker = self._run_isolated_pdd_refund_stage(
                task_id,
                facts,
                operation="prepare",
            )
            result = dict(worker.get("preview") or {})
            result["detail_activation"] = worker.get("detail_activation")
            result["worker_isolated"] = True
            facts.setdefault("source_evidence", {})["pdd_refund_preflight"] = result
            self.repository.update_facts(task_id, facts)
        else:
            # Manual preview from the task page may still need to open a new
            # detail tab.  Keep that legacy path, while the production queue
            # always passes retain_detail=True and therefore stays isolated.
            result = self.refund_workflow.preview_first_stage(
                facts, retain_detail=False
            )
        self.repository.log_action(
            task_id,
            f"{task['business_key']}:REFUND_PREVIEW:{int(time.time())}",
            "PREVIEW_PDD_REFUND_COMPONENTS",
            "success",
            result,
        )
        return {"ok": True, **result}

    def execute_refund_pilot(
        self,
        task_id: int,
        confirm_order: str,
        confirm_amount: float,
        *,
        reconcile_inline: bool = False,
        close_inline: bool = False,
    ) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        facts = task["facts"]
        safety = self.config.raw.get("safety", {})
        try:
            assert_action_allowed(
                "AGREE_REFUND",
                self.config.execution_mode,
                self.config.live_actions_enabled,
                approved=True,
            )
            shop = next((item for item in self.config.shops if item.id == facts.get("shop_id")), None)
            if not shop:
                raise SafetyViolation("任务店铺不在启用的店铺白名单中")
            if safety.get("require_exact_shop_name", True) or safety.get("require_exact_edge_profile", True):
                validate_shop_context(
                    SafetyContext(
                        configured_shop_name=shop.name,
                        page_shop_name=str(facts.get("shop_name") or ""),
                        task_shop_name=str(facts.get("shop_name") or ""),
                        configured_profile=shop.edge_profile,
                        active_profile=str(facts.get("edge_profile") or ""),
                    )
                )
        except SafetyViolation as exc:
            raise RuntimeError(str(exc)) from exc

        amount = float(facts.get("refund_amount") or 0)
        max_single = float(safety.get("max_single_refund_amount") or 0)
        if max_single > 0 and amount > max_single + 0.001:
            raise RuntimeError(f"单笔退款金额 {amount:.2f} 超过安全上限 {max_single:.2f}")

        prior_real_logs = [
            item
            for item in self.repository.list_logs(task_id=task_id, limit=100)
            if item.get("action_type") in {"EXECUTE_PDD_REAL_REFUND", "RECONCILE_PDD_REAL_REFUND"}
            and item.get("status") == "success"
            and (item.get("details") or {}).get("business_write_performed")
        ]
        if prior_real_logs:
            raise RuntimeError("该工单已有真实退款写入记录，拒绝重复执行")

        safety_result = {
            "execution_mode": str(self.config.execution_mode),
            "live_actions_enabled": self.config.live_actions_enabled,
            "max_single_refund_amount": max_single,
        }
        worker = self._run_isolated_pdd_refund_stage(
            task_id,
            facts,
            operation="execute",
            confirm_order=confirm_order,
            confirm_amount=confirm_amount,
            reconcile_after_execute=reconcile_inline,
            close_detail_after_execute=close_inline,
        )
        result = dict(worker.get("result") or {})
        if worker.get("detail_activation") is not None:
            result["detail_activation"] = worker.get("detail_activation")
        result["worker_isolated"] = True
        result["safety"] = safety_result
        result["preflight"] = dict(worker.get("preview") or {})
        result["detail_cleanup"] = worker.get("detail_cleanup")
        self.repository.log_action(
            task_id,
            f"{task['business_key']}:REAL_REFUND:{int(time.time())}",
            "EXECUTE_PDD_REAL_REFUND",
            "success",
            result,
        )
        if reconcile_inline:
            reconciliation = dict(worker.get("reconciliation") or {})
            if not reconciliation:
                raise RuntimeError("退款 Worker 未返回内联对账结果")
            result["reconciliation"] = self._persist_refund_reconciliation(
                task_id, reconciliation
            )
        return result

    def _persist_refund_reconciliation(
        self, task_id: int, result: dict[str, Any]
    ) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        result = dict(result)
        result["worker_isolated"] = True
        facts = task["facts"]
        facts.setdefault("source_evidence", {})["real_refund_result"] = result
        self.repository.update_facts(task_id, facts)
        self.repository.mark_task_status(
            task_id,
            TaskState.COMPLETED,
            "退款已提交并通过页面结果对账；平台可能仍显示退款处理中",
        )
        self.repository.set_json_setting(
            "refund_uia_regression",
            {
                "confirmed": True,
                "shop_id": facts.get("shop_id"),
                "shop_name": facts.get("shop_name"),
                "task_id": task_id,
                "order_no": facts.get("order_no"),
                "refund_amount": result.get("refund_amount"),
                "status": "refund_processing"
                if result.get("success", {}).get("refund_processing")
                else "refund_success",
                "evidence": (facts.get("screenshots") or [])[-1:],
                "confirmed_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        self.repository.log_action(
            task_id,
            f"{task['business_key']}:REAL_REFUND_RECONCILED:{int(time.time())}",
            "RECONCILE_PDD_REAL_REFUND",
            "success",
            result,
        )
        return result

    def reconcile_refund_result(self, task_id: int) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        worker = self._run_isolated_pdd_refund_stage(
            task_id,
            task["facts"],
            operation="reconcile",
        )
        return self._persist_refund_reconciliation(
            task_id, dict(worker.get("result") or {})
        )

    def enrich_and_evaluate(self, task_id: int) -> dict[str, Any]:
        if self.repository.is_paused():
            raise RuntimeError("系统已全局暂停")
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")

        state = TaskState(task["state"])
        if state is TaskState.DISCOVERED:
            self.repository.transition(task_id, TaskState.COLLECTING_DATA, "开始采集模拟数据")
        elif state not in {TaskState.COLLECTING_DATA, TaskState.WAIT_RULE_MATCH, TaskState.NEED_REVIEW}:
            raise RuntimeError(f"当前状态不允许重新评估：{state}")

        facts = task["facts"]
        jst = self.jst.query_order(facts["order_no"], facts.get("tracking_no", ""))
        logistics = self.logistics.query(facts.get("carrier", ""), facts.get("tracking_no", ""))
        if not jst.ok or not logistics.ok:
            self.repository.transition(task_id, TaskState.DATA_CONFLICT, "数据连接器返回失败")
            return self.repository.get_task(task_id) or {}

        facts.update(jst.data)
        facts.update(logistics.data)
        facts.setdefault("source_evidence", {}).update(
            {"jst": "模拟查询", "logistics": "模拟查询"}
        )
        self.repository.update_facts(task_id, facts)

        current = self.repository.get_task(task_id)
        if current and TaskState(current["state"]) is TaskState.COLLECTING_DATA:
            self.repository.transition(task_id, TaskState.WAIT_RULE_MATCH, "数据采集完成")

        decision = evaluate_rules(facts, self.repository.load_rules())
        self.repository.save_decision(task_id, decision)
        current = self.repository.get_task(task_id)
        if current:
            current_state = TaskState(current["state"])
            if current_state is TaskState.WAIT_RULE_MATCH:
                target = TaskState.READY if decision.matched else TaskState.NEED_REVIEW
                self.repository.transition(task_id, target, decision.reasons[0])
                if target is TaskState.READY and decision.approval_required:
                    self.repository.transition(task_id, TaskState.WAIT_APPROVAL, "规则要求人工批准")
            elif current_state is TaskState.NEED_REVIEW and decision.matched:
                # 人工待审任务在补充/启用规则后可以重新进入批准队列，
                # 但仍然不能直接跳到真实执行。
                self.repository.transition(task_id, TaskState.WAIT_APPROVAL, "重新评估后命中规则")
        self.repository.log_action(
            task_id,
            f"{task['business_key']}:RULE:{task['version']}",
            "EVALUATE_RULES",
            "success",
            {"decision": asdict(decision)},
        )
        return self.repository.get_task(task_id) or {}

    def preview_intercept(self, task_id: int, group_name: str) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise KeyError(f"任务不存在：{task_id}")
        tracking_no = task["facts"].get("tracking_no", "")
        if not tracking_no:
            raise RuntimeError("任务缺少运单号")
        routing_config = self.get_in_transit_config()
        message = str(
            routing_config.get("message_template", "{tracking_no}+\u62e6\u622a")
        ).format(
            tracking_no=tracking_no,
            carrier=task["facts"].get("carrier", ""),
        )
        result = self.messages.preview_send(group_name, message)
        action_key = f"{task['business_key']}:PREVIEW_INTERCEPT"
        self.repository.log_action(
            task_id,
            action_key,
            "PREVIEW_INTERCEPT_MESSAGE",
            "success" if result.ok else "failed",
            {"connector": self.messages.name, **result.data, "evidence": result.evidence},
        )
        return {"ok": result.ok, **result.data, "evidence": result.evidence}
