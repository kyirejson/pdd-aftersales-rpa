from __future__ import annotations

import re
import time
from typing import Any

from .browser_executor import BrowserExecutionError, PddBrowserComponentExecutor
from .components import validate_component
from .in_transit_routing import (
    _operational_latest_logistics_text,
    latest_logistics_mentions_wuxi,
)
from .db import Repository
from .element_capture import ElementCaptureService


class InTransitWorkflowError(RuntimeError):
    pass


def decide_outcome_form_action(latest_logistics_text: str) -> dict[str, Any]:
    """Choose the alternate PDD `处理情况` form outcome from its latest trace."""
    latest = str(latest_logistics_text or "").strip()
    # PDD often appends a service-help sentence to the actual logistics update.
    # Only the delivery-status part before that annotation is relevant here.
    operational_text = _operational_latest_logistics_text(latest)
    city_labels = [
        label.strip()
        for label in re.findall(r"【\s*([^】]+?)\s*】", operational_text)
        if label.strip().endswith("市")
    ]
    city = city_labels[-1] if city_labels else ""
    blocked_statuses = [
        status for status in ("已签收", "派送中") if status in operational_text
    ]

    # This rule applies only to the alternate outcome form. For the first
    # warehouse, the green latest logistics text may mention the configured
    # city without a bracketed label; either wording is an unconditional
    # direct-refund condition.
    if latest_logistics_mentions_wuxi(operational_text):
        action = "AGREE_REFUND"
        decision_reason = "最新物流满足直退条件：不受已签收或派送中状态限制"
    elif city == "示例城市乙市" and not blocked_statuses:
        action = "AGREE_REFUND"
        decision_reason = "示例城市乙市且未签收、未派送中"
    else:
        action = "AGREED_CONSENSUS"
        decision_reason = "最新物流不满足直退条件，或示例城市乙市已签收/派送中"
    return {
        "action": action,
        "city": city,
        "blocked_statuses": blocked_statuses,
        "operational_logistics_text": operational_text,
        "decision_reason": decision_reason,
    }


class InTransitComponentWorkflow:
    """Fixed-component UIA flow for 在途无理由退款.

    The detail tab is retained while 旺店通 and the interception connector run.
    Components are captured once from the real PDD detail page and then reused
    only when their exact UIA text remains unique and visible.
    """

    REASON_COMPONENT = "CMP-PDD-IN-TRANSIT-SELECT-REASON"
    REASON_SUBMIT_COMPONENT = "CMP-PDD-IN-TRANSIT-SUBMIT-REASON"
    AGREE_COMPONENT = "CMP-PDD-IN-TRANSIT-SELECT-AGREE"
    AGREE_SUBMIT_COMPONENT = "CMP-PDD-IN-TRANSIT-SUBMIT-AGREE"
    WITHDRAW_COMPONENT = "CMP-PDD-IN-TRANSIT-SELECT-WITHDRAW"
    WITHDRAW_SUBMIT_COMPONENT = "CMP-PDD-IN-TRANSIT-SUBMIT-WITHDRAW"
    OUTCOME_AGREE_COMPONENT = "CMP-PDD-IN-TRANSIT-OUTCOME-SELECT-AGREE-REFUND"
    OUTCOME_CONSENSUS_COMPONENT = "CMP-PDD-IN-TRANSIT-OUTCOME-SELECT-AGREED-CONSENSUS"
    OUTCOME_SUBMIT_COMPONENT = "CMP-PDD-IN-TRANSIT-OUTCOME-SUBMIT"

    REASON_FORM = "reason"
    OUTCOME_FORM = "outcome"

    def __init__(
        self,
        repository: Repository,
        pdd_config: dict[str, Any],
        browser: PddBrowserComponentExecutor | None = None,
    ):
        self.repository = repository
        self.browser = browser or PddBrowserComponentExecutor(pdd_config)

    def _capture_or_reuse(
        self,
        facts: dict[str, Any],
        component_id: str,
        text: str,
        *,
        action: str,
        control_type: str = "",
    ) -> dict[str, Any]:
        component = self.repository.get_component(component_id)
        if component and not component.get("config", {}).get("capture_pending"):
            return component
        described = self.browser.describe_click_targets(facts, [text]).get(text) or {}
        if described.get("count") != 1 or not described.get("selector_unique"):
            raise InTransitWorkflowError(f"在途退款元素捕获不唯一：{text} {described}")
        capture = dict(described)
        capture.setdefault("uia_control_type", control_type)
        component = ElementCaptureService.component_from_capture(
            capture,
            f"在途退款：{text}",
            "ClickElement",
        )
        component["component_id"] = component_id
        component["config"] = {
            "action": action,
            "anchor_text": text,
            "stage": "IN_TRANSIT_REFUND",
            "capture_pending": False,
            "captured_from_order": facts.get("order_no", ""),
        }
        self.repository.save_component(validate_component(component))
        return component

    def _reason_option_state(self, facts: dict[str, Any], reason_text: str) -> dict[str, Any]:
        described = self.browser.describe_click_targets(facts, [reason_text]).get(reason_text) or {}
        count = int(described.get("count") or 0)
        if count == 0:
            return {"available": False, "capture": described}
        if count != 1 or not described.get("selector_unique"):
            raise InTransitWorkflowError(
                f"在途退款原因选项不唯一：{reason_text} {described}"
            )
        return {"available": True, "capture": described}

    def _prepare_outcome_form(self, facts: dict[str, Any]) -> dict[str, Any]:
        """Capture the alternate `处理情况` form without selecting anything."""
        decision = decide_outcome_form_action(facts.get("latest_logistics_text", ""))
        agree = self._capture_or_reuse(
            facts,
            self.OUTCOME_AGREE_COMPONENT,
            "同意退款",
            action="select_option",
            control_type="Text",
        )
        consensus = self._capture_or_reuse(
            facts,
            self.OUTCOME_CONSENSUS_COMPONENT,
            "已协商一致",
            action="select_option",
            control_type="Text",
        )
        submit = self._capture_or_reuse(
            facts,
            self.OUTCOME_SUBMIT_COMPONENT,
            "提交",
            action="click_button",
            control_type="Button",
        )
        return {
            "ok": True,
            "detail_form": self.OUTCOME_FORM,
            "final_action_override": decision["action"],
            "refund_reason": (
                "同意退款"
                if decision["action"] == "AGREE_REFUND"
                else "已协商一致"
            ),
            "decision": decision,
            "trace": [
                {"component_id": agree["component_id"], "status": "captured"},
                {"component_id": consensus["component_id"], "status": "captured"},
                {"component_id": submit["component_id"], "status": "captured"},
            ],
            "business_write_performed": False,
        }

    def prepare_reason(self, facts: dict[str, Any], reason_text: str, *, live: bool) -> dict[str, Any]:
        reason_state = self._reason_option_state(facts, reason_text)
        if not reason_state["available"]:
            return self._prepare_outcome_form(facts)
        reason = self._capture_or_reuse(
            facts,
            self.REASON_COMPONENT,
            reason_text,
            action="select_option",
            control_type="Text",
        )
        submit = self._capture_or_reuse(
            facts,
            self.REASON_SUBMIT_COMPONENT,
            "提交",
            action="click_button",
            control_type="Button",
        )
        trace = [{"component_id": reason["component_id"], "status": "captured"},
                 {"component_id": submit["component_id"], "status": "captured"}]
        if live:
            for component in (reason, submit):
                trace.append({
                    "component_id": component["component_id"],
                    "result": self.browser.execute_click(component, facts, True),
                })
        return {
            "ok": True,
            "detail_form": self.REASON_FORM,
            "final_action_override": "",
            "refund_reason": reason_text,
            "trace": trace,
            "business_write_performed": bool(live),
        }

    def wait_for_final_form(
        self,
        facts: dict[str, Any],
        action: str,
        *,
        timeout_seconds: float = 12,
    ) -> dict[str, Any]:
        """Wait for PDD's second-stage form after submitting the reason."""
        if str(facts.get("in_transit_detail_form") or self.REASON_FORM) == self.OUTCOME_FORM:
            return {"ready": True, "detail_form": self.OUTCOME_FORM, "waited": False}
        if action == "AGREE_REFUND":
            target_text = "同意消费者退款申请"
        elif action == "REJECT_REFUND":
            target_text = "消费者撤销售后申请"
        else:
            return {"ready": True, "detail_form": self.REASON_FORM, "waited": False}

        deadline = time.monotonic() + max(2.0, min(float(timeout_seconds), 30.0))
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            described = self.browser.describe_click_targets(facts, [target_text])
            last_state = dict(described.get(target_text) or {})
            if int(last_state.get("count") or 0) == 1 and last_state.get("selector_unique"):
                return {
                    "ready": True,
                    "detail_form": self.REASON_FORM,
                    "target_text": target_text,
                    "waited": True,
                    "capture": last_state,
                }
            time.sleep(0.35)
        raise InTransitWorkflowError(
            f"提交申请原因后未出现下一阶段控件：{target_text} {last_state}"
        )

    def _finish_outcome_form(self, facts: dict[str, Any], action: str, *, live: bool) -> dict[str, Any]:
        if action == "AGREE_REFUND":
            option_id, option_text = self.OUTCOME_AGREE_COMPONENT, "同意退款"
        elif action == "AGREED_CONSENSUS":
            option_id, option_text = self.OUTCOME_CONSENSUS_COMPONENT, "已协商一致"
        else:
            raise InTransitWorkflowError(f"处理情况页面不支持终态：{action}")
        option = self._capture_or_reuse(
            facts,
            option_id,
            option_text,
            action="select_option",
            control_type="Text",
        )
        submit = self._capture_or_reuse(
            facts,
            self.OUTCOME_SUBMIT_COMPONENT,
            "提交",
            action="click_button",
            control_type="Button",
        )
        trace = [
            {"component_id": option["component_id"], "status": "captured"},
            {"component_id": submit["component_id"], "status": "captured"},
        ]
        if live:
            for component in (option, submit):
                trace.append(
                    {
                        "component_id": component["component_id"],
                        "result": self.browser.execute_click(component, facts, True),
                    }
                )
            try:
                # The alternate outcome form should transition immediately
                # after its sole submit.  Do not keep the desktop occupied for
                # the generic terminal timeout when the form itself is still
                # visibly unchanged; this task must be skipped, never retried
                # or allowed to block the following orders.
                terminal = self.browser.wait_for_terminal_state(
                    facts,
                    expected_action=action,
                    timeout_seconds=5,
                )
            except BrowserExecutionError as exc:
                unchanged = self.browser.inspect_page_state(facts)
                if (
                    action == "AGREED_CONSENSUS"
                    and unchanged.get("has_agreed_consensus")
                    and unchanged.get("visible_forms")
                ):
                    trace.append(
                        {
                            "component_id": "VERIFY-IN-TRANSIT-OUTCOME-TERMINAL",
                            "status": "skipped_submission_ineffective",
                            "reason": "已协商一致并提交后表单未变化",
                            "state": unchanged,
                        }
                    )
                    return {
                        "ok": True,
                        "action": action,
                        "detail_form": self.OUTCOME_FORM,
                        "trace": trace,
                        "submission_ineffective": True,
                        "skip_reason": "已协商一致并提交后表单未变化",
                        # Both UIA clicks were attempted.  This must never be
                        # auto-retried after the task is skipped.
                        "business_write_performed": True,
                    }
                raise exc
            trace.append(
                {
                    "component_id": "VERIFY-IN-TRANSIT-OUTCOME-TERMINAL",
                    "result": terminal,
                }
            )
        return {
            "ok": True,
            "action": action,
            "detail_form": self.OUTCOME_FORM,
            "trace": trace,
            "business_write_performed": bool(live),
        }

    def finish(self, facts: dict[str, Any], action: str, *, live: bool) -> dict[str, Any]:
        detail_form = str(facts.get("in_transit_detail_form") or self.REASON_FORM)
        if detail_form == self.OUTCOME_FORM:
            return self._finish_outcome_form(facts, action, live=live)
        if detail_form != self.REASON_FORM:
            raise InTransitWorkflowError(f"未知在途工单详情页面形态：{detail_form}")
        if action == "AGREE_REFUND":
            # The live PDD in-transit form exposes this exact option text.
            # Do not reuse the older generic “同意退款” label: it is absent on
            # the current page and would make an otherwise eligible order
            # fail before the final submit.
            option_id, option_text = self.AGREE_COMPONENT, "同意消费者退款申请"
            submit_id, submit_text = self.AGREE_SUBMIT_COMPONENT, "提交"
        elif action == "REJECT_REFUND":
            option_id, option_text = self.WITHDRAW_COMPONENT, "消费者撤销售后申请"
            submit_id, submit_text = self.WITHDRAW_SUBMIT_COMPONENT, "提交"
        else:
            raise InTransitWorkflowError(f"未知在途退款终态：{action}")
        option = self._capture_or_reuse(
            facts,
            option_id,
            option_text,
            action="select_option",
            control_type="Text",
        )
        submit = self._capture_or_reuse(
            facts,
            submit_id,
            submit_text,
            action="click_button",
            control_type="Button",
        )
        trace = [
            {"component_id": option["component_id"], "status": "captured"},
            {"component_id": submit["component_id"], "status": "captured"},
        ]
        if live and action == "REJECT_REFUND":
            # A reject decision always has two PDD merchant submissions:
            #   1) 消费者撤销售后申请 → 提交
            #   2) 消费者撤销售后申请 → 提交
            # This is intentionally carrier-neutral.  Carrier selection and
            # chat delivery have already finished before this PDD method runs.
            # The second form is rendered asynchronously.  Never treat the
            # first historical progress entry as completion, otherwise the
            # second submission is silently skipped.  If a worker restarts
            # after phase one, resume from the visible second-stage form
            # instead of sending another external interception message or
            # repeating phase one.
            initial_state = self.browser.inspect_page_state(facts)
            if initial_state.get("withdraw_followup_required") and initial_state.get("visible_forms"):
                trace.append(
                    {
                        "component_id": "REJECT-WITHDRAW-RESUME-FOLLOWUP",
                        "status": "started",
                        "state": initial_state,
                    }
                )
            elif initial_state.get("withdraw_requested"):
                if initial_state.get("visible_forms"):
                    raise InTransitWorkflowError(
                        "消费者撤销第一阶段已提交，但未能确认第二阶段表单；为避免误点未继续提交"
                    )
                trace.append(
                    {
                        "component_id": "REJECT-WITHDRAW-ALREADY-FINALIZED",
                        "status": "verified",
                        "state": initial_state,
                    }
                )
                return {
                    "ok": True,
                    "action": action,
                    "trace": trace,
                    "business_write_performed": False,
                }
            else:
                trace.append({"component_id": "REJECT-WITHDRAW-FIRST", "status": "started"})
                for component in (option, submit):
                    trace.append(
                        {
                            "component_id": component["component_id"],
                            "phase": "withdraw_first",
                            "result": self.browser.execute_click(component, facts, True),
                        }
                    )
                first_terminal = self.browser.wait_for_terminal_state(
                    facts,
                    expected_action=action,
                    require_withdraw_followup=True,
                )
                if not first_terminal.get("withdraw_followup_required") or not first_terminal.get("visible_forms"):
                    raise InTransitWorkflowError("消费者撤销第一阶段后未进入第二次提交页面")
                trace.append(
                    {
                        "component_id": "VERIFY-IN-TRANSIT-WITHDRAW-FIRST",
                        "result": first_terminal,
                    }
                )

            trace.append({"component_id": "REJECT-WITHDRAW-FOLLOWUP", "status": "started"})
            for component in (option, submit):
                trace.append(
                    {
                        "component_id": component["component_id"],
                        "phase": "withdraw_followup",
                        "result": self.browser.execute_click(component, facts, True),
                    }
                )
            trace.append(
                {
                    "component_id": "VERIFY-IN-TRANSIT-WITHDRAW-FOLLOWUP",
                    "result": self.browser.wait_for_terminal_state(
                        facts,
                        expected_action=action,
                        require_withdraw_completion=True,
                    ),
                }
            )
        elif live:
            for component in (option, submit):
                trace.append(
                    {
                        "component_id": component["component_id"],
                        "result": self.browser.execute_click(component, facts, True),
                    }
                )
            terminal = self.browser.wait_for_terminal_state(facts, expected_action=action)
            trace.append(
                {
                    "component_id": "VERIFY-IN-TRANSIT-TERMINAL",
                    "result": terminal,
                }
            )
        return {"ok": True, "action": action, "trace": trace, "business_write_performed": bool(live)}
