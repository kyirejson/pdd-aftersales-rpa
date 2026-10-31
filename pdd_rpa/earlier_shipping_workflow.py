from __future__ import annotations

import time
from typing import Any

from .browser_executor import PddBrowserComponentExecutor
from .components import validate_component
from .db import Repository
from .element_capture import ElementCaptureService


EARLIER_SHIPPING_TITLE = "确认更早发货时间"
EARLIER_SHIPPING_OPTION_TEXT = "可以发货，但是不确定能否在承诺时间内发货"


class EarlierShippingWorkflowError(RuntimeError):
    pass


class EarlierShippingComponentWorkflow:
    """Handle one `确认更早发货时间` work order through exact UIA components.

    PDD does not render the submit button until the radio option is selected.
    The submit component is therefore discovered only after the option click;
    pre-capturing both controls would be an invalid representation of the live
    page and causes false timeouts.
    """

    OPTION_COMPONENT = "CMP-PDD-EARLIER-SHIPPING-SELECT-UNCERTAIN"
    SUBMIT_COMPONENT = "CMP-PDD-EARLIER-SHIPPING-SUBMIT"

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
        control_type: str,
        described: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        component = self.repository.get_component(component_id)
        if component and not component.get("config", {}).get("capture_pending"):
            return component
        capture = dict(
            described
            or (self.browser.describe_click_targets(facts, [text]).get(text) or {})
        )
        if int(capture.get("count") or 0) != 1 or not capture.get("selector_unique"):
            raise EarlierShippingWorkflowError(
                f"确认更早发货时间元素捕获不唯一：{text} {capture}"
            )
        capture.setdefault("uia_control_type", control_type)
        component = ElementCaptureService.component_from_capture(
            capture,
            f"确认更早发货时间：{text}",
            "ClickElement",
        )
        component["component_id"] = component_id
        component["config"] = {
            "action": action,
            "anchor_text": text,
            "stage": "CONFIRM_EARLIER_SHIPPING",
            "capture_pending": False,
            "captured_from_order": facts.get("order_no", ""),
        }
        self.repository.save_component(validate_component(component))
        return component

    def _wait_for_submit(
        self,
        facts: dict[str, Any],
        *,
        timeout_seconds: float = 12,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(2.0, min(float(timeout_seconds), 30.0))
        last_capture: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_capture = dict(
                self.browser.describe_click_targets(facts, ["提交"]).get("提交") or {}
            )
            if (
                int(last_capture.get("count") or 0) == 1
                and last_capture.get("selector_unique")
            ):
                return last_capture
            time.sleep(0.3)
        raise EarlierShippingWorkflowError(
            f"选择发货时间选项后未出现唯一提交按钮：{last_capture}"
        )

    def execute(self, facts: dict[str, Any], *, live: bool) -> dict[str, Any]:
        if str(facts.get("work_order_title") or "") != EARLIER_SHIPPING_TITLE:
            raise EarlierShippingWorkflowError("当前工单不是确认更早发货时间")

        option = self._capture_or_reuse(
            facts,
            self.OPTION_COMPONENT,
            EARLIER_SHIPPING_OPTION_TEXT,
            action="select_option",
            control_type="Text",
        )
        trace: list[dict[str, Any]] = [
            {"component_id": option["component_id"], "status": "captured"}
        ]
        if not live:
            return {
                "ok": True,
                "live": False,
                "trace": trace,
                "submit_deferred_until_selection": True,
                "business_write_performed": False,
            }

        trace.append(
            {
                "component_id": option["component_id"],
                "result": self.browser.execute_click(option, facts, True),
            }
        )
        submit_capture = self._wait_for_submit(facts)
        submit = self._capture_or_reuse(
            facts,
            self.SUBMIT_COMPONENT,
            "提交",
            action="click_button",
            control_type="Button",
            described=submit_capture,
        )
        trace.append({"component_id": submit["component_id"], "status": "captured_after_option"})
        trace.append(
            {
                "component_id": submit["component_id"],
                "result": self.browser.execute_click(submit, facts, True),
            }
        )
        terminal = self.browser.wait_for_terminal_state(
            facts,
            expected_action="CONFIRM_EARLIER_SHIPPING",
            timeout_seconds=20,
        )
        trace.append(
            {
                "component_id": "VERIFY-PDD-EARLIER-SHIPPING-TERMINAL",
                "result": terminal,
            }
        )
        return {
            "ok": True,
            "live": True,
            "selected_option": EARLIER_SHIPPING_OPTION_TEXT,
            "trace": trace,
            "terminal": terminal,
            "business_write_performed": True,
        }
