from types import SimpleNamespace

import pytest

from pdd_rpa.edge_uia import PddEdgeUiaSession, PddUiaError, classify_pdd_url


LIST_URL = "https://mms.pinduoduo.com/aftersales/work_order/list?msfrom=mms_sidenav"
SHOP = {"id": "shop_01", "name": "示例书店 A", "edge_profile": "Profile"}


class FakeControl:
    def __init__(self, name, control_type, class_name="", on_click=None):
        self.element_info = SimpleNamespace(
            name=name, control_type=control_type, class_name=class_name
        )
        self._on_click = on_click

    def is_visible(self):
        return True

    def click_input(self):
        if self._on_click:
            self._on_click()


class FakeWindow:
    handle = 101

    def __init__(self, width=1936, height=1048):
        self.url = "https://mms.pinduoduo.com/login/"
        self.title = "拼多多商家后台 - 示例书店 A - Microsoft Edge"
        self.account_clicked = False
        self.login_clicked = False
        self.popup_closed = False
        self.controls = []
        self._rect = SimpleNamespace(
            left=-8,
            top=-8,
            width=lambda: width,
            height=lambda: height,
        )

    def window_text(self):
        return self.title

    def rectangle(self):
        return self._rect


class FakeMouse:
    def __init__(self, session):
        self.session = session
        self.clicks = []

    def click(self, coords):
        self.clicks.append(coords)
        if len(self.clicks) == 1:
            self.session.window.account_clicked = True
        elif len(self.clicks) == 2:
            self.session._login()


class FakeSession(PddEdgeUiaSession):
    def __init__(self, window):
        self.config = {
            "list_url": LIST_URL,
            "login_popup_wait_seconds": 1,
            "login_button_wait_seconds": 1,
            "login_transition_timeout_seconds": 1,
            "login_list_wait_seconds": 1,
        }
        self.window = window
        self.last_login_recovery = None
        window.controls = [
            FakeControl(
                "账号登录",
                "Text",
                on_click=lambda: setattr(window, "account_clicked", True),
            ),
            FakeControl(
                "登录",
                "Button",
                "BTN_outerWrapper_5-155-0",
                on_click=self._login,
            ),
        ]

    def _windows(self):
        return [self.window]

    def _shop_windows(self, shop):
        return [self.window] if shop["name"] in self.window.window_text() else []

    def _current_url(self, window):
        return window.url

    def _controls(self, handle, visible_only=False):
        return list(self.window.controls)

    def _focus(self, window):
        return None

    def _login(self):
        self.window.login_clicked = True
        self.window.url = "https://mms.pinduoduo.com/home"
        close = FakeControl(
            "",
            "Image",
            "MDL_headerCloseIcon_5-155-0",
            on_click=self._close_popup,
        )
        self.window.controls = [
            FakeControl("MDL", "Pane", "MDL_container_5-155-0"),
            close,
        ]

    def _close_popup(self):
        self.window.popup_closed = True
        self.window.controls = []

    def navigate_url(self, window, url):
        window.url = url
        window.controls = [FakeControl(SHOP["name"], "Text")]


class RelativeFallbackSession(FakeSession):
    def __init__(self, window):
        super().__init__(window)
        self.config.update(
            {
                "login_account_form_wait_seconds": 0,
                "login_relative_uia_probe_seconds": 0.01,
            }
        )
        window.controls = []
        self.mouse = FakeMouse(self)


def test_login_url_is_classified_separately():
    assert classify_pdd_url("https://mms.pinduoduo.com/login/") == "login"


def test_recovery_clicks_captured_account_and_login_then_navigates():
    window = FakeWindow()
    session = FakeSession(window)

    result = session.ensure_shop_list(SHOP)

    assert result["status"] == "ready"
    assert result["account_tab_clicked"] is True
    assert result["login_clicked"] is True
    assert result["popup_closed"] is True
    assert window.account_clicked is True
    assert window.login_clicked is True
    assert window.popup_closed is True
    assert window.url == LIST_URL
    assert result["credentials_read"] is False
    assert result["credentials_written"] is False
    assert result["business_write_performed"] is False


def test_duplicate_login_button_fails_closed():
    window = FakeWindow()
    session = FakeSession(window)
    window.controls.append(FakeControl("登录", "Button"))

    with pytest.raises(PddUiaError, match="登录控件不唯一"):
        session.ensure_shop_list(SHOP)


def test_recovery_uses_constrained_relative_fallback_when_edge_hides_uia_names():
    window = FakeWindow()
    session = RelativeFallbackSession(window)

    result = session.ensure_shop_list(SHOP)

    assert result["status"] == "ready"
    assert result["manual_required"] is False
    assert result["account_tab_clicked"] is True
    assert result["login_clicked"] is True
    assert window.account_clicked is True
    assert window.login_clicked is True
    assert session.mouse.clicks == [(1411, 388), (1351, 574)]
    assert any(item.startswith("click_account_login_relative:") for item in result["trace"])
    assert any(item.startswith("click_login_relative:") for item in result["trace"])
    assert "account_login_already_selected" not in result["trace"]


def test_relative_fallback_refuses_an_undersized_login_window():
    window = FakeWindow(width=1000, height=650)
    session = RelativeFallbackSession(window)

    result = session.recover_shop_login(SHOP, window)

    assert result.status == "manual_required"
    assert result.manual_required is True
    assert result.login_clicked is False
    assert session.mouse.clicks == []
    assert any("登录窗口尺寸不足" in item for item in result.trace)
