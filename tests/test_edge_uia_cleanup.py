from __future__ import annotations

import pytest

from pdd_rpa.edge_uia import (
    PddEdgeUiaSession,
    PddSidebarNavigationError,
    PddUiaError,
)


LIST_URL = "https://mms.pinduoduo.com/aftersales/work_order/list?msfrom=mms_sidenav"
DETAIL_URL = "https://mms.pinduoduo.com/aftersales/work_order/tododetail?id=123"
AFTERSALE_LIST_URL = "https://mms.pinduoduo.com/aftersales/aftersale_list?msfrom=mms_sidenav"
AFTERSALE_SETUP_URL = "https://mms.pinduoduo.com/aftersales/setup?tab=3"
SHOP = {
    "id": "shop_04",
    "name": "示例书店 D",
    "edge_profile": "Profile 3",
}


class FakeWindow:
    handle = 101

    def __init__(self, session):
        self.session = session

    def window_text(self):
        if self.session.closed:
            raise RuntimeError("window closed")
        return f"{SHOP['name']} - Microsoft Edge"


class FakeDesktop:
    def __init__(self, session):
        self.session = session

    def window(self, *, handle):
        assert handle == self.session.window.handle
        if self.session.closed:
            raise RuntimeError("window closed")
        return self.session.window


class FakeKeyboard:
    def __init__(self, session):
        self.session = session
        self.keys = []

    def send_keys(self, keys, **kwargs):
        self.keys.append(keys)
        if keys == "^{TAB}":
            self.session.active = (self.session.active + 1) % len(self.session.tabs)
        elif keys == "^w":
            if len(self.session.tabs) == 1:
                self.session.closed = True
            else:
                self.session.tabs.pop(self.session.active)
                self.session.active %= len(self.session.tabs)


class FakeCleanupSession(PddEdgeUiaSession):
    def __init__(self, tabs):
        self.config = {
            "list_url": LIST_URL,
            "tab_switch_attempts": 4,
            "tab_switch_wait_seconds": 0,
            "list_restore_timeout_seconds": 2,
        }
        self.tabs = list(tabs)
        self.active = 0
        self.closed = False
        self.window = FakeWindow(self)
        self.keyboard = FakeKeyboard(self)

    def Desktop(self, **kwargs):
        return FakeDesktop(self)

    def _windows(self):
        return [] if self.closed else [self.window]

    def _shop_windows(self, shop):
        return self._windows()

    def _current_url(self, window):
        if self.closed:
            raise RuntimeError("window closed")
        return self.tabs[self.active]

    def _focus(self, window):
        return None

    def _has_exact_name(self, handle, name):
        return name == SHOP["name"]

    def assert_page_contract(self, *args, **kwargs):
        return {"ready": True}

    def _click_sidebar_entry(self, window, text):
        assert text == "工单管理"
        self.tabs[self.active] = LIST_URL

    def navigate_url(self, window, url, **kwargs):
        assert url == LIST_URL
        if kwargs:
            assert kwargs["expected_shop"] == SHOP
            assert kwargs["allowed_source_surfaces"]
        self.tabs[self.active] = url


def test_single_detail_tab_is_navigated_to_list_without_ctrl_w(monkeypatch):
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda *_: None)
    session = FakeCleanupSession([DETAIL_URL])

    result = session.close_detail(session.window, SHOP)

    assert result["closed"] == 0
    assert result["navigated"] == 1
    assert result["list_ready"] is True
    assert session.closed is False
    assert session.tabs == [LIST_URL]
    assert "^w" not in session.keyboard.keys


def test_detail_tab_closes_only_after_list_tab_is_verified(monkeypatch):
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda *_: None)
    session = FakeCleanupSession([DETAIL_URL, LIST_URL])

    result = session.close_detail(session.window, SHOP)

    assert result["closed"] == 1
    assert result["navigated"] == 0
    assert result["list_ready"] is True
    assert result["disposition"] == "detail_tab_closed_list_active"
    assert session.closed is False
    assert session.tabs == [LIST_URL]
    assert session.keyboard.keys.count("^w") == 1


def test_missing_shop_identity_blocks_unsafe_close(monkeypatch):
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda *_: None)
    session = FakeCleanupSession([DETAIL_URL])

    try:
        session.close_detail(session.window)
    except Exception as exc:
        assert "禁止 Ctrl+W" in str(exc)
    else:
        raise AssertionError("missing shop identity must fail closed")

    assert session.closed is False
    assert "^w" not in session.keyboard.keys


def test_aftersale_scan_returns_to_work_order_list_without_closing_window(monkeypatch):
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda *_: None)
    session = FakeCleanupSession([AFTERSALE_LIST_URL])

    result = session.restore_work_order_list_after_aftersale(SHOP, session.window.handle)

    assert result["closed"] == 0
    assert result["navigated"] == 1
    assert result["list_ready"] is True
    assert result["disposition"] == "aftersale_navigated_to_work_order_list"
    assert session.closed is False
    assert session.tabs == [LIST_URL]
    assert "^w" not in session.keyboard.keys


def test_restore_shop_list_anchor_recovers_from_aftersale_page(monkeypatch):
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda *_: None)
    session = FakeCleanupSession([AFTERSALE_LIST_URL])

    result = session.restore_shop_list_anchor(SHOP)

    assert result["closed"] == 0
    assert result["navigated"] == 1
    assert result["list_ready"] is True
    assert result["disposition"] == "aftersale_navigated_to_list"
    assert session.closed is False
    assert session.tabs == [LIST_URL]
    assert "^w" not in session.keyboard.keys


def test_restore_shop_list_anchor_recovers_from_aftersale_setup(monkeypatch):
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda *_: None)
    session = FakeCleanupSession([AFTERSALE_SETUP_URL])

    result = session.restore_shop_list_anchor(SHOP)

    assert result["closed"] == 0
    assert result["navigated"] == 1
    assert result["list_ready"] is True
    assert result["disposition"] == "aftersale_navigated_to_list"
    assert session.closed is False
    assert session.tabs == [LIST_URL]
    assert "^w" not in session.keyboard.keys


def test_aftersale_restore_uses_allowlisted_url_when_sidebar_is_not_exposed(monkeypatch):
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda *_: None)
    session = FakeCleanupSession([AFTERSALE_LIST_URL])

    def missing_sidebar(_window, _text):
        raise PddSidebarNavigationError("未发现左侧导航可点击候选")

    session._click_sidebar_entry = missing_sidebar

    result = session.restore_shop_list_anchor(SHOP)

    assert result["list_ready"] is True
    assert result["navigation_method"] == "allowlisted_address_bar"
    assert session.tabs == [LIST_URL]
    assert session.closed is False
    assert "^w" not in session.keyboard.keys


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://mms.pinduoduo.com/aftersales/work_order/list?x=1",
        "https://mms.pinduoduo.com/aftersales/work_order/list?msfrom=mms_sidenav&x=1",
        "https://mms.pinduoduo.com/aftersales/work_order/list#fragment",
        "https://mms.pinduoduo.com:443/aftersales/work_order/list",
        "https://user@mms.pinduoduo.com/aftersales/work_order/list",
        "https://mms.pinduoduo.com/aftersales/work_order%2flist",
        "https://mms.pinduoduo.com/aftersales/work_order/list?x={ENTER}",
    ],
)
def test_work_order_navigation_rejects_noncanonical_targets(unsafe_url):
    session = FakeCleanupSession([AFTERSALE_LIST_URL])

    with pytest.raises(PddUiaError, match="固定的拼多多工单列表"):
        session._canonical_work_order_url(unsafe_url)

    assert session.keyboard.keys == []


def test_work_order_navigation_canonicalizes_queryless_target():
    session = FakeCleanupSession([AFTERSALE_LIST_URL])

    assert (
        session._canonical_work_order_url(
            "https://mms.pinduoduo.com/aftersales/work_order/list"
        )
        == LIST_URL
    )


def test_address_bar_navigation_rechecks_source_after_focus():
    session = object.__new__(PddEdgeUiaSession)
    session.config = {}
    current_url = [AFTERSALE_LIST_URL]
    emitted_keys = []
    session.keyboard = type(
        "Keyboard",
        (),
        {"send_keys": staticmethod(lambda value, **kwargs: emitted_keys.append(value))},
    )()

    def focus_then_switch_tab(_window):
        current_url[0] = "https://mms.pinduoduo.com/home"

    session._focus = focus_then_switch_tab
    session._current_url = lambda _window: current_url[0]
    session._window_matches_shop = lambda _window, _shop: True

    with pytest.raises(PddUiaError, match="源页面已变化"):
        session.navigate_url(
            FakeWindow(session),
            LIST_URL,
            expected_shop=SHOP,
            allowed_source_surfaces={"aftersale_list"},
        )

    assert emitted_keys == []
