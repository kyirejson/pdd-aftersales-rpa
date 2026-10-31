from __future__ import annotations

from types import SimpleNamespace

import pytest

from pdd_rpa.aftersale_uia import PddAftersaleUiaSession


class _Rect:
    def __init__(self, left=248, top=197, right=319, bottom=218):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class _Control:
    def __init__(self, name: str, *, parent=None, class_name="", top=197):
        self.element_info = SimpleNamespace(
            name=name,
            control_type="Text",
            class_name=class_name,
        )
        self._parent = parent
        self._rect = _Rect(top=top, bottom=top + 21)

    def parent(self):
        return self._parent

    def rectangle(self):
        return self._rect

    def is_visible(self):
        return True


def _session() -> PddAftersaleUiaSession:
    session = object.__new__(PddAftersaleUiaSession)
    session.config = {
        "aftersale_list_url": (
            "https://mms.pinduoduo.com/aftersales/aftersale_list"
            "?msfrom=mms_sidenav"
        )
    }
    return session


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://mms.pinduoduo.com/aftersales/setup?tab=3", True),
        ("https://mms.pinduoduo.com/aftersales/setup", True),
        ("https://mms.pinduoduo.com/aftersales/setup?tab=2", False),
        ("https://example.com/aftersales/setup?tab=3", False),
        ("https://mms.pinduoduo.com/aftersales/setup?tab=3#x", False),
    ],
)
def test_setup_url_allowlist_is_exact(url, expected):
    assert PddAftersaleUiaSession._is_aftersale_setup_url(url) is expected


def test_refund_aftersale_top_tab_ignores_same_text_outside_real_tab():
    session = _session()
    real_parent = _Control(
        "", class_name="TAB_tabItem_abcd TAB_top_abcd"
    )
    real = _Control("退款/售后", parent=real_parent)
    unrelated_parent = _Control("", class_name="message_panel")
    unrelated = _Control("退款/售后", parent=unrelated_parent, top=600)
    session._controls = lambda *_args, **_kwargs: [real_parent, real, unrelated_parent, unrelated]

    assert session._refund_aftersale_top_tab_target(101) is real


def test_setup_page_clicks_refund_aftersale_tab_then_requires_list(monkeypatch):
    session = _session()
    window = SimpleNamespace(handle=101)
    shop = {"name": "示例书店 B"}
    state = {"url": "https://mms.pinduoduo.com/aftersales/setup?tab=3"}
    parent = _Control("", class_name="TAB_tabItem_abcd TAB_top_abcd")
    target = _Control("退款/售后", parent=parent)
    invoked: list[str] = []
    session._current_url = lambda _window: state["url"]
    session._window_matches_shop = lambda _window, _shop: True
    session._refund_aftersale_top_tab_target = lambda _handle: target

    def invoke(_window, _target, text, **_kwargs):
        invoked.append(text)
        state["url"] = (
            "https://mms.pinduoduo.com/aftersales/aftersale_list"
            "?msfrom=mms_sidenav"
        )
        return {"text": text, "executed": True}

    session._invoke_action_target = invoke
    monkeypatch.setattr("pdd_rpa.aftersale_uia.time.sleep", lambda *_args: None)

    session._ensure_aftersale_list_surface(window, shop, timeout_seconds=2)

    assert invoked == ["退款/售后"]
    assert "/aftersales/aftersale_list" in state["url"]


def test_list_window_accepts_title_verified_shop_after_setup_recovery(monkeypatch):
    session = _session()
    session.config["aftersale_list_wait_seconds"] = 2
    shop = {"name": "示例书店 A"}
    window = SimpleNamespace(
        handle=101,
        window_text=lambda: "示例书店 A - 拼多多商家后台",
    )
    state = {
        "url": "https://mms.pinduoduo.com/aftersales/aftersale_list"
    }
    session.ensure_shop_list = lambda _shop: {"status": "not_needed"}
    session._shop_windows = lambda _shop: [window]
    session._activate_aftersale_tab = lambda *_args, **_kwargs: False
    session._navigate_aftersale_list = lambda *_args, **_kwargs: None
    session._current_url = lambda _window: state["url"]
    session._has_exact_name = lambda *_args: False
    session._window_matches_shop = lambda _window, _shop: True
    session.assert_page_contract = lambda *_args, **_kwargs: {"ready": True}
    monkeypatch.setattr("pdd_rpa.aftersale_uia.time.sleep", lambda *_args: None)

    assert session._list_window(shop) is window


def test_duplicate_real_refund_tabs_fail_closed():
    session = _session()
    parent_a = _Control("", class_name="TAB_tabItem_a TAB_top_a")
    parent_b = _Control("", class_name="TAB_tabItem_b TAB_top_b")
    controls = [
        parent_a,
        _Control("退款/售后", parent=parent_a),
        parent_b,
        _Control("退款/售后", parent=parent_b),
    ]
    session._controls = lambda *_args, **_kwargs: controls

    with pytest.raises(Exception, match="顶部页签不唯一"):
        session._refund_aftersale_top_tab_target(101)
