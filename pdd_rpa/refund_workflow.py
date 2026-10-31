from __future__ import annotations

import time
from typing import Any

from .browser_executor import PddBrowserComponentExecutor
from .components import validate_workflow
from .db import Repository
from .detail_collector import PddDetailCollector
from .element_capture import ElementCaptureService


class RefundComponentWorkflow:
    def __init__(self, repository: Repository, detail_collector: PddDetailCollector, pdd_config: dict[str, Any]):
        self.repository = repository
        self.detail_collector = detail_collector
        self.browser = PddBrowserComponentExecutor(pdd_config)
        self._prepared_detail: dict[str, str] | None = None

    @staticmethod
    def _detail_identity(facts: dict[str, Any]) -> dict[str, str]:
        return {
            "shop_id": str(facts.get("shop_id") or ""),
            "order_no": str(facts.get("order_no") or ""),
            "detail_url": str(facts.get("source_evidence", {}).get("detail_url") or ""),
        }

    def clear_prepared_detail(self, shop_id: str | None = None) -> None:
        """Forget the retained detail marker after the task cleanup closes tabs."""
        if self._prepared_detail is None:
            self.browser.clear_active_detail()
            return
        if shop_id is None or self._prepared_detail.get("shop_id") == str(shop_id):
            self._prepared_detail = None
            self.browser.clear_active_detail()

    def activate_detail_after_wdt(self, facts: dict[str, Any]) -> dict[str, Any]:
        """Restore the retained PDD detail page after 旺店通 finishes."""
        return self.browser.activate_detail_after_wdt(facts)

    def close_retained_detail(self, facts: dict[str, Any]) -> dict[str, Any]:
        """Close the exact processed detail tab and verify the shop list remains."""
        session, handle = self.browser._detail_target(facts)
        window = session.Desktop(backend="win32").window(handle=handle)
        shop = {
            "id": str(facts.get("shop_id") or ""),
            "name": str(facts.get("shop_name") or ""),
            "edge_profile": str(facts.get("edge_profile") or ""),
        }
        cleanup = dict(session.close_detail(window, shop) or {})
        self.clear_prepared_detail()
        return {**cleanup, "shop_id": shop["id"]}

    def _require_open_detail(self, facts: dict[str, Any]) -> None:
        expected = self._detail_identity(facts)
        if self._prepared_detail != expected:
            raise RuntimeError("退款执行前未复用同一工单详情页，拒绝重新定位")
        try:
            self.browser._detail_target(facts)
        except Exception as exc:
            self.clear_prepared_detail()
            raise RuntimeError("退款执行前工单详情页已关闭或身份发生变化") from exc

    def preview_first_stage(
        self, facts: dict[str, Any], *, retain_detail: bool = False
    ) -> dict[str, Any]:
        workflow = self.repository.get_workflow("WF-PDD-SHIPPED-NO-TRACE-REFUND")
        if not workflow:
            raise RuntimeError("拼多多退款组件流程尚未建立")
        if retain_detail:
            # The automated queue already opened this exact detail page while
            # collecting the tracking number.  Do not close and reopen it.
            try:
                self.browser._detail_target(facts)
            except Exception as exc:
                raise RuntimeError("旺店通核对后未找到仍保持打开的同一工单详情页") from exc
        else:
            detail_error: Exception | None = None
            for attempt in range(1, 3):
                try:
                    self.detail_collector.collect(
                        facts["shop_id"], facts["order_no"], keep_detail_open=True
                    )
                    detail_error = None
                    break
                except Exception as exc:
                    detail_error = exc
                    if attempt == 2:
                        raise
                    time.sleep(0.8)
            if detail_error is not None:
                raise detail_error
        prepared = False
        try:
            page_state = self.browser.inspect_page_state(facts)
            if page_state.get("completed") or page_state.get("refund_success"):
                raise RuntimeError(f"工单已经完结或退款成功，拒绝继续执行退款组件：{page_state}")
            trace = [{"component_id": "PAGE-STATE-GUARD", "name": "工单状态前置校验", "inspection": page_state}]
            for component_id in ("CMP-PDD-SELECT-UNHANDED", "CMP-PDD-SUBMIT-UNHANDED"):
                component = self.repository.get_component(component_id)
                if not component:
                    raise RuntimeError(f"退款组件不存在：{component_id}")
                trace.append({"component_id": component_id, "name": component["name"], "inspection": self.browser.execute_click(component, facts, False)})
            pending = [
                item["component_id"] for item in workflow["nodes"]
                if (self.repository.get_component(item["component_id"]) or {}).get("config", {}).get("capture_pending")
            ]
            prepared = bool(retain_detail)
            if prepared:
                self._prepared_detail = self._detail_identity(facts)
            return {
                "ready_first_stage": True,
                "trace": trace,
                "capture_pending": pending,
                "detail_retained": bool(retain_detail),
                "business_write_performed": False,
            }
        finally:
            if not (retain_detail and prepared):
                try:
                    session, handle = self.browser._detail_target(facts)
                    window = session.Desktop(backend="win32").window(handle=handle)
                    session.close_detail(
                        window,
                        {
                            "id": str(facts.get("shop_id") or ""),
                            "name": str(facts.get("shop_name") or ""),
                            "edge_profile": str(facts.get("edge_profile") or ""),
                        },
                    )
                except Exception:
                    pass
                self.clear_prepared_detail()

    def execute_real_pilot(
        self, facts: dict[str, Any], confirm_order: str, confirm_amount: float
    ) -> dict[str, Any]:
        if confirm_order != facts.get("order_no"):
            raise RuntimeError("实战确认订单号与任务订单号不一致")
        if abs(float(facts.get("refund_amount") or 0) - float(confirm_amount)) > 0.001:
            raise RuntimeError("实战确认退款金额与详情采集金额不一致")
        if facts.get("work_order_title") != "已发货无轨迹退款处理":
            raise RuntimeError("仅允许实战已发货无轨迹退款处理工单")
        if not facts.get("wdt_eligible") or facts.get("wdt_result_count") != 1:
            raise RuntimeError("旺店通唯一订单与退款状态校验未通过")
        if facts.get("wdt_order_status") not in {"未审核", "已取消", "已审核", "待客审"}:
            raise RuntimeError(f"旺店通状态不允许退款：{facts.get('wdt_order_status')}")

        # The runtime keeps the detail page open from the initial collection
        # through WDT verification.  Requiring the prepared target here avoids
        # the old close/reopen cycle and prevents a second "立即处理" lookup.
        self._require_open_detail(facts)
        trace: list[dict[str, Any]] = []
        page_state = self.browser.inspect_page_state(facts)
        trace.append({"component_id": "PAGE-STATE-GUARD", "inspection": page_state})
        if page_state.get("completed") or page_state.get("refund_success"):
            raise RuntimeError("工单已完结或退款成功，拒绝重复退款")

        if not page_state.get("has_agree_refund"):
            for component_id in ("CMP-PDD-SELECT-UNHANDED", "CMP-PDD-SUBMIT-UNHANDED"):
                component = self.repository.get_component(component_id)
                if not component:
                    raise RuntimeError(f"退款组件不存在：{component_id}")
                result = self.browser.execute_click(component, facts, True)
                trace.append({"component_id": component_id, "name": component["name"], "result": result})
            trace.append({"component_id": "WAIT-AGREE-STAGE", "result": self.browser.wait_for_text(facts, "操作同意退款", 20)})

        definitions = [
            self.repository.get_component("CMP-PDD-SELECT-AGREE"),
            self.repository.get_component("CMP-PDD-SUBMIT-FINISH"),
        ]
        capture_needed = any(
            not component or component.get("config", {}).get("capture_pending")
            for component in definitions
        )
        if capture_needed:
            described = self.browser.describe_click_targets(facts, ["同意退款", "提交并完结"])
            definitions = []
            for text, component_id, name, action, anchor in (
                ("同意退款", "CMP-PDD-SELECT-AGREE", "选择同意退款", "select_option", "操作同意退款"),
                ("提交并完结", "CMP-PDD-SUBMIT-FINISH", "提交并完结", "click_button", "同意退款"),
            ):
                capture = described.get(text) or {}
                if capture.get("count") != 1 or not capture.get("selector_unique"):
                    raise RuntimeError(f"第二阶段元素捕获不唯一：{text} {capture}")
                component = ElementCaptureService.component_from_capture(capture, name, "ClickElement")
                component["component_id"] = component_id
                component["config"] = {
                    "action": action,
                    "anchor_text": anchor,
                    "stage": "AGREE_REFUND",
                    "capture_pending": False,
                    "captured_from_order": facts["order_no"],
                }
                self.repository.save_component(component)
                definitions.append(component)
            workflow = self.repository.get_workflow("WF-PDD-SHIPPED-NO-TRACE-REFUND")
            if workflow:
                workflow["description"] = "影刀式固定组件：刷新、旺店通校验、两阶段退款及成功回读；第二阶段已从真实页面捕获"
                workflow["version"] = max(int(workflow.get("version", 1)), 3)
                self.repository.save_workflow(validate_workflow(workflow))
            trace.append({"component_id": "CAPTURE-AGREE-STAGE", "captured": described})
        else:
            trace.append({"component_id": "REUSE-AGREE-STAGE", "components": [item["component_id"] for item in definitions]})

        for component in definitions:
            result = self.browser.execute_click(component, facts, True)
            trace.append({"component_id": component["component_id"], "name": component["name"], "result": result})
        success = self.browser.wait_for_refund_success(facts, 25)
        trace.append({"component_id": "VERIFY-REFUND-SUCCESS", "result": success})
        return {
            "ok": True,
            "order_no": facts["order_no"],
            "refund_amount": float(facts.get("refund_amount") or 0),
            "wdt_order_status": facts.get("wdt_order_status"),
            "captured_components": [item["component_id"] for item in definitions],
            "success": success,
            "trace": trace,
            "business_write_performed": True,
        }

    def execute_shipped_after_interception(
        self, facts: dict[str, Any], *, live: bool
    ) -> dict[str, Any]:
        """Submit 已交给快递 -> 同意退款 on the retained detail page.

        This branch is entered only after the carrier interception connector
        returned an exact sent-message readback.  It deliberately does not
        reuse the old 未交给快递 components because that choice represents the
        opposite business fact.
        """
        state = self.browser.inspect_page_state(facts)
        if state.get("completed") or state.get("refund_success"):
            raise RuntimeError("工单已经完结或退款成功，拒绝重复提交")

        def component(component_id: str, name: str, text: str, control_type: str) -> dict[str, Any]:
            return {
                "component_id": component_id,
                "name": name,
                "version": 1,
                "component_type": "ClickElement",
                "locator": {"text": text, "uia_control_type": control_type},
                "config": {
                    "action": "select_option" if control_type == "Text" else "click_button",
                    "capture_pending": False,
                    "stage": "SHIPPED_AFTER_INTERCEPTION",
                },
            }

        first_stage = [
            component("CMP-PDD-SELECT-HANDED", "选择已交给快递", "已交给快递", "Text"),
            component("CMP-PDD-SUBMIT-HANDED", "提交已交给快递", "提交", "Button"),
        ]
        trace: list[dict[str, Any]] = []
        # A worker may be retried after PDD accepted the first submission but
        # before the second one completed.  In that case the retained detail
        # page already exposes the `同意退款` form and replaying `已交给快递`
        # would either fail or duplicate a durable merchant action.
        if state.get("has_agree_refund"):
            trace.append(
                {
                    "component_id": "RESUME-AGREE-AFTER-INTERCEPT",
                    "result": {"ready": True, "first_stage_already_submitted": True},
                }
            )
        else:
            for definition in first_stage:
                result = self.browser.execute_click(definition, facts, live)
                trace.append({"component_id": definition["component_id"], "result": result})
        if not live:
            return {
                "ok": True,
                "ready": True,
                "trace": trace,
                "business_write_performed": False,
            }

        # The current PDD page does not render the old progress marker
        # `操作同意退款`.  Its actionable second-stage marker is the radio
        # option itself, and the button label is `提交` (not `提交并完结`).
        # Wait for exactly what will be clicked so a slow stage transition is
        # tolerated without turning a valid page into a false timeout.
        ready = self.browser.wait_for_text(facts, "同意退款", 20)
        trace.append({"component_id": "WAIT-AGREE-AFTER-INTERCEPT", "result": ready})
        second_stage = [
            component("CMP-PDD-SELECT-AGREE-AFTER-INTERCEPT", "选择同意退款", "同意退款", "Text"),
            component("CMP-PDD-SUBMIT-AGREE-AFTER-INTERCEPT", "提交同意退款", "提交", "Button"),
        ]
        for definition in second_stage:
            result = self.browser.execute_click(definition, facts, True)
            trace.append({"component_id": definition["component_id"], "result": result})
        success = self.browser.wait_for_refund_success(facts, 25)
        return {
            "ok": True,
            "trace": trace,
            "success": success,
            "business_write_performed": True,
        }

    def reconcile_real_result(self, facts: dict[str, Any]) -> dict[str, Any]:
        state = self.browser.inspect_page_state(facts)
        accepted_refund_state = state.get("refund_success") or state.get("refund_processing")
        submitted_async = (
            state.get("merchant_agreed")
            and state.get("refund_processing")
            and not state.get("visible_forms")
        )
        if not (
            (state.get("completed") and state.get("merchant_agreed") and accepted_refund_state)
            or submitted_async
        ):
            raise RuntimeError(f"退款结果对账未通过：{state}")
        return {
            "ok": True,
            "order_no": facts["order_no"],
            "refund_amount": float(facts.get("refund_amount") or 0),
            "success": state,
            "reconciled_after_submit": True,
            "business_write_performed": True,
        }
