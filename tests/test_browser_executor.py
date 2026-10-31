from __future__ import annotations

import pytest

from pdd_rpa.browser_executor import BrowserExecutionError, PddBrowserComponentExecutor


class _FakeWin32Gui:
    def __init__(self, foreground: int):
        self.foreground = foreground

    def GetForegroundWindow(self):
        return self.foreground


class _FakeWindow:
    def __init__(self, handle: int, title: str):
        self.handle = handle
        self._title = title

    def window_text(self):
        return self._title


class _FakeDesktop:
    def __init__(self, window: _FakeWindow):
        self._window = window

    def window(self, handle: int):
        assert handle == self._window.handle
        return self._window


class _FakeSession:
    def __init__(self, *, foreground: int, url: str, shop: str, order: str):
        self.window = _FakeWindow(42, f"{shop} - Microsoft Edge")
        self.Desktop = lambda backend: _FakeDesktop(self.window)
        self.win32gui = _FakeWin32Gui(foreground)
        self.url = url
        self.shop = shop
        self.order = order
        self.focus_calls = 0
        self.detail_window_calls = 0

    def detail_window(self, shop_name: str, expected_url: str, order_no: str):
        self.detail_window_calls += 1
        assert shop_name == self.shop
        assert expected_url == self.url
        assert order_no == self.order
        return self.window

    def _focus(self, window):
        self.focus_calls += 1
        self.win32gui.foreground = int(window.handle)

    def _current_url(self, window):
        return self.url

    def _has_exact_name(self, handle: int, value: str):
        return value in {self.shop, self.order}


def _facts():
    return {
        "shop_name": "示例书店 A",
        "order_no": "260000-182849669745241",
        "source_evidence": {
            "detail_url": "https://mms.pinduoduo.com/aftersales/work_order/tododetail?id=40000000000000"
        },
    }


def test_activate_detail_after_wdt_focuses_and_validates(monkeypatch):
    facts = _facts()
    expected_url = facts["source_evidence"]["detail_url"]
    session = _FakeSession(
        foreground=7,
        url=expected_url,
        shop=facts["shop_name"],
        order=facts["order_no"],
    )
    monkeypatch.setattr("pdd_rpa.browser_executor.PddEdgeUiaSession", lambda config: session)

    result = PddBrowserComponentExecutor({}).activate_detail_after_wdt(facts)

    assert result["ok"] is True
    assert result["foreground_match"] is True
    assert result["shop_match"] is True
    assert result["order_match"] is True
    assert result["url_match"] is True
    assert session.focus_calls == 1

    # The activated target is reused by later component calls; locating the
    # detail window is not repeated for every refund click.
    executor = PddBrowserComponentExecutor({})
    monkeypatch.setattr("pdd_rpa.browser_executor.PddEdgeUiaSession", lambda config: session)
    executor.activate_detail_after_wdt(facts)
    executor._detail_target(facts)
    executor._detail_target(facts)
    assert session.detail_window_calls == 2


def test_activate_detail_after_wdt_rejects_url_mismatch(monkeypatch):
    facts = _facts()
    expected_url = facts["source_evidence"]["detail_url"]
    session = _FakeSession(
        foreground=42,
        url=expected_url,
        shop=facts["shop_name"],
        order=facts["order_no"],
    )
    monkeypatch.setattr("pdd_rpa.browser_executor.PddEdgeUiaSession", lambda config: session)
    session._current_url = lambda window: expected_url + "&stale=1"

    with pytest.raises(BrowserExecutionError, match="详情页校验失败"):
        PddBrowserComponentExecutor({}).activate_detail_after_wdt(facts)


def test_withdraw_request_progress_is_terminal_for_reject_flow():
    executor = PddBrowserComponentExecutor({})
    state = {
        "completed": False,
        "refund_success": False,
        "refund_processing": False,
        "withdraw_requested": True,
    }
    executor.inspect_page_state = lambda facts: state

    result = executor.wait_for_terminal_state({}, expected_action="REJECT_REFUND")

    assert result is state
