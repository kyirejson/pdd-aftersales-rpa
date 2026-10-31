from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from .edge_uia import PddEdgeUiaSession


class BrowserExecutionError(RuntimeError):
    pass


class PddBrowserComponentExecutor:
    """Execute captured PDD browser components through Windows UIA only."""

    def __init__(self, pdd_config: dict[str, Any]):
        self.pdd_config = pdd_config
        self._active_detail: dict[str, Any] | None = None

    @staticmethod
    def _work_order_id(facts: dict[str, Any]) -> str:
        url = str(facts.get("source_evidence", {}).get("detail_url", ""))
        return parse_qs(urlparse(url).query).get("id", [""])[0]

    @staticmethod
    def _detail_identity(facts: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(facts.get("shop_id") or ""),
            str(facts.get("order_no") or ""),
            str(facts.get("source_evidence", {}).get("detail_url") or ""),
        )

    def clear_active_detail(self) -> None:
        self._active_detail = None

    def _poll_seconds(self) -> float:
        """React quickly to ready UIA state without shortening safety timeouts."""

        try:
            value = float(self.pdd_config.get("browser_state_poll_seconds", 0.35))
        except (TypeError, ValueError):
            value = 0.35
        return max(0.15, min(value, 1.0))

    def _detail_target(self, facts: dict[str, Any], *, reuse_active: bool = True):
        identity = self._detail_identity(facts)
        if reuse_active and self._active_detail and self._active_detail.get("identity") == identity:
            return self._active_detail["session"], int(self._active_detail["handle"])
        session = PddEdgeUiaSession(self.pdd_config)
        expected_url = str(facts.get("source_evidence", {}).get("detail_url", ""))
        window = session.detail_window(facts["shop_name"], expected_url, facts["order_no"])
        return session, int(window.handle)

    def activate_detail_after_wdt(self, facts: dict[str, Any]) -> dict[str, Any]:
        """Bring the retained PDD detail page back to the foreground once.

        The isolated 旺店通 worker leaves its own desktop window focused.  The
        parent process must explicitly restore the exact PDD detail window
        before any refund component can operate.  This method is intentionally
        called once after the WDT workflow returns; individual refund clicks do
        not repeat the same foreground/identity validation.
        """
        expected_shop = str(facts.get("shop_name") or "").strip()
        expected_order = str(facts.get("order_no") or "").strip()
        expected_url = str(facts.get("source_evidence", {}).get("detail_url") or "").strip()
        if not expected_shop or not expected_order or not expected_url:
            raise BrowserExecutionError(
                "旺店通返回后无法恢复拼多多详情页：缺少店铺、订单号或详情 URL"
            )

        session, handle = self._detail_target(facts, reuse_active=False)
        window = session.Desktop(backend="win32").window(handle=handle)
        session._focus(window)

        foreground_handle = int(session.win32gui.GetForegroundWindow())
        actual_url = session._current_url(window)
        window_title = str(window.window_text() or "")
        shop_match = expected_shop in window_title or session._has_exact_name(handle, expected_shop)
        order_match = session._has_exact_name(handle, expected_order)
        url_match = actual_url == expected_url
        foreground_match = foreground_handle == handle
        validation = {
            "ok": bool(foreground_match and shop_match and order_match and url_match),
            "engine": "edge_uia",
            "window_handle": handle,
            "foreground_handle": foreground_handle,
            "foreground_match": foreground_match,
            "shop_name": expected_shop,
            "shop_match": shop_match,
            "order_no": expected_order,
            "order_match": order_match,
            "expected_url": expected_url,
            "actual_url": actual_url,
            "url_match": url_match,
            "window_title": window_title,
        }
        if not validation["ok"]:
            self.clear_active_detail()
            raise BrowserExecutionError(
                f"旺店通返回后拼多多详情页校验失败：{validation}"
            )
        self._active_detail = {
            "identity": self._detail_identity(facts),
            "session": session,
            "handle": handle,
            "validation": validation,
        }
        return validation

    def refresh_detail(self, facts: dict[str, Any], timeout_seconds: float = 30) -> dict[str, Any]:
        """Refresh the retained detail tab and return its current UIA fields."""
        expected_url = str(facts.get("source_evidence", {}).get("detail_url", "")).strip()
        session, handle = self._detail_target(facts)
        window = session.Desktop(backend="win32").window(handle=handle)
        session._focus(window)
        session.keyboard.send_keys("^r")
        deadline = time.monotonic() + max(5.0, min(float(timeout_seconds), 90.0))
        while time.monotonic() < deadline:
            time.sleep(self._poll_seconds())
            if session._current_url(window) != expected_url:
                continue
            if not session._has_exact_name(handle, str(facts.get("order_no") or "")):
                continue
            try:
                return session.read_detail(handle, str(facts.get("order_no") or ""))
            except Exception:
                continue
        raise BrowserExecutionError("刷新后未能重新读取同一工单详情")

    def wait_for_terminal_state(
        self,
        facts: dict[str, Any],
        timeout_seconds: float = 25,
        expected_action: str = "",
        require_withdraw_completion: bool = False,
        require_withdraw_followup: bool = False,
    ) -> dict[str, Any]:
        """Wait for the terminal merchant action on a PDD detail page.

        A refund agreement becomes an immediate PDD terminal state.  A
        consumer-withdraw request instead leaves a platform follow-up page
        open while the consumer responds, so the durable service-progress
        marker is the terminal state for our merchant workflow.
        """
        deadline = time.monotonic() + max(5.0, min(float(timeout_seconds), 90.0))
        while time.monotonic() < deadline:
            state = self.inspect_page_state(facts)
            if state.get("completed") or state.get("refund_success") or state.get("refund_processing"):
                return state
            if expected_action == "REJECT_REFUND":
                # The withdrawal path has two distinct merchant submissions.
                # The first must advance to PDD's explicit follow-up form;
                # seeing only the historical “消费者撤销” progress text is not
                # sufficient evidence that the second submission happened.
                if require_withdraw_followup:
                    if state.get("withdraw_followup_required") and state.get("visible_forms"):
                        return state
                elif state.get("withdraw_requested") and (
                    not require_withdraw_completion
                    or not state.get("withdraw_followup_required")
                ):
                    return state
            elif expected_action == "AGREED_CONSENSUS":
                # The alternate `处理情况` form does not enter the usual
                # refund-processing state. Its terminal evidence is the
                # negotiated outcome with no live submit form.
                if state.get("has_agreed_consensus") and not state.get("visible_forms"):
                    return state
            time.sleep(self._poll_seconds())
        raise BrowserExecutionError("提交售后处理后未回读到终态")

    def inspect_page_state(self, facts: dict[str, Any]) -> dict[str, Any]:
        session, handle = self._detail_target(facts)
        result = session.detail_state(handle, facts["order_no"], self._work_order_id(facts))
        if not result.get("work_order_id_matches") or not result.get("order_matches"):
            raise BrowserExecutionError(f"工单详情页身份复核失败：{result}")
        return result

    def wait_for_text(
        self, facts: dict[str, Any], required_text: str, timeout_seconds: float = 15
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            session, handle = self._detail_target(facts)
            matches = [
                control for control in session._controls(handle)
                if session._name(control) == required_text
            ]
            if matches:
                return {"ready": True, "marker": required_text, "engine": "edge_uia"}
            time.sleep(self._poll_seconds())
        raise BrowserExecutionError(f"等待 UIA 页面文字超时：{required_text}")

    def describe_click_targets(
        self, facts: dict[str, Any], texts: list[str]
    ) -> dict[str, Any]:
        session, handle = self._detail_target(facts)
        result: dict[str, Any] = {}
        for text in texts:
            inspected = session.inspect_exact(handle, text)
            if not inspected.get("ready"):
                result[text] = {"count": inspected.get("count", 0)}
                continue
            left, top, right, bottom = inspected["rect"]
            result[text] = {
                "count": 1,
                "tag": inspected["control_type"],
                "selector": "",
                "selector_unique": True,
                "text": text,
                "aria_label": text,
                "role": inspected["control_type"],
                "uia_control_type": inspected["control_type"],
                "class_name": "",
                "rect": {"x": left, "y": top, "width": right - left, "height": bottom - top},
                "engine": "edge_uia",
            }
        return result

    def wait_for_refund_success(
        self, facts: dict[str, Any], timeout_seconds: float = 20
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            state = self.inspect_page_state(facts)
            accepted = state.get("refund_success") or state.get("refund_processing")
            submitted_async = (
                state.get("merchant_agreed")
                and state.get("refund_processing")
                and not state.get("visible_forms")
            )
            if (state.get("completed") and state.get("merchant_agreed") and accepted) or submitted_async:
                return state
            time.sleep(self._poll_seconds())
        raise BrowserExecutionError("退款后未回读到已完结、商家同意及退款状态")

    @staticmethod
    def _control_type(component: dict[str, Any]) -> str:
        locator = component.get("locator") or {}
        explicit = str(locator.get("uia_control_type") or "")
        if explicit:
            return explicit
        action = str((component.get("config") or {}).get("action", "click_button"))
        return "Text" if action == "select_option" else "Button"

    def inspect_click(self, component: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
        state = self.inspect_page_state(facts)
        session, handle = self._detail_target(facts)
        text = str((component.get("locator") or {}).get("text", ""))
        result = session.inspect_exact(handle, text, self._control_type(component))
        if result.get("ready") is not True:
            raise BrowserExecutionError(f"组件“{component['name']}”UIA 预检失败：{result}")
        return {**result, "engine": "edge_uia", "work_order_id": state["work_order_id"]}

    def execute_click(
        self, component: dict[str, Any], facts: dict[str, Any], live_actions_enabled: bool
    ) -> dict[str, Any]:
        if component.get("config", {}).get("capture_pending"):
            raise BrowserExecutionError(f"组件“{component['name']}”尚未从真实页面捕获")
        inspected = self.inspect_click(component, facts)
        if not live_actions_enabled:
            return {**inspected, "executed": False, "reason": "live_actions_disabled"}
        session, handle = self._detail_target(facts)
        text = str((component.get("locator") or {}).get("text", ""))
        result = session.click_exact(handle, text, self._control_type(component))
        return {**result, "work_order_id": inspected["work_order_id"]}
