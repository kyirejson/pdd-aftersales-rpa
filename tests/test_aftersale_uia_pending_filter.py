from __future__ import annotations

from types import SimpleNamespace

import pytest

from pdd_rpa.aftersale_uia import PddAftersaleUiaSession
from pdd_rpa.edge_uia import PddUiaError


class FakeControl:
    def __init__(self, name: str, class_name: str = "", *, parent=None, on_click=None):
        self.element_info = SimpleNamespace(
            name=name,
            control_type="Text",
            class_name=class_name,
            automation_id="",
        )
        self._parent = parent
        self._on_click = on_click
        self.clicks = 0

    def parent(self):
        return self._parent

    def is_enabled(self):
        return True

    def click_input(self):
        self.clicks += 1
        if self._on_click:
            self._on_click()


class FakePendingSession(PddAftersaleUiaSession):
    def __init__(
        self,
        *,
        selected: bool = False,
        select_on_click: bool = True,
        current_url: str = "https://mms.pinduoduo.com/aftersales/aftersale_list",
    ):
        self.config = {
            "aftersale_list_wait_seconds": 0.1,
            "aftersale_pending_selection_wait_seconds": 0.1,
            "aftersale_pending_settle_seconds": 0,
        }
        checked = " CAD_beastCardChecked_5-163-0" if selected else ""
        self.card = FakeControl(
            "",
            "CAD_beastCard_5-163-0 MmsUiQuickFilter___borderStyle___vM_iU2-133-0"
            + checked,
        )

        def select():
            if select_on_click:
                self.card.element_info.class_name += " CAD_beastCardChecked_5-163-0"

        self.target = FakeControl("待处理即将逾期", parent=self.card, on_click=select)
        self.popup_dismissed = False
        self.current_url = current_url

    def _controls(self, handle, visible_only=False):
        return [self.target, self.card]

    def assert_page_contract(self, *args, **kwargs):
        assert self.popup_dismissed is True
        return None

    def _dismiss_overlay_popups(self, window):
        self.popup_dismissed = True
        return {"closed": 0, "clear": True}

    def _focus(self, window):
        return None

    def _current_url(self, window):
        return self.current_url


class FakeWindow:
    handle = 101

    def window_text(self):
        return "测试店铺 - Microsoft Edge"


SHOP = {"name": "测试店铺"}


def test_pending_filter_is_clicked_only_once_and_selected():
    session = FakePendingSession()

    session._click_quick_pending(FakeWindow(), SHOP)

    assert session.target.clicks == 1
    assert session._pending_filter_selected(session.target) is True


def test_already_selected_pending_filter_is_not_clicked_again():
    session = FakePendingSession(selected=True)

    session._click_quick_pending(FakeWindow(), SHOP)

    assert session.target.clicks == 0


def test_search_type_4_url_prevents_reclick_when_uia_checked_class_is_missing():
    session = FakePendingSession(
        selected=False,
        select_on_click=False,
        current_url=(
            "https://mms.pinduoduo.com/aftersales/aftersale_list?searchType=4"
        ),
    )

    session._click_quick_pending(FakeWindow(), SHOP)

    assert session.target.clicks == 0
    assert session._pending_filter_selected(session.target) is False
    assert session._pending_filter_selected_by_url(FakeWindow()) is True


def test_failed_selection_never_triggers_a_second_toggle_click():
    session = FakePendingSession(select_on_click=False)

    with pytest.raises(PddUiaError, match="已禁止二次点击和采集"):
        session._click_quick_pending(FakeWindow(), SHOP)

    assert session.target.clicks == 1


def test_pending_badge_count_reads_zero_immediately_after_filter_label():
    session = PddAftersaleUiaSession.__new__(PddAftersaleUiaSession)

    class Rect:
        def __init__(self, left, top, right, bottom):
            self.left, self.top, self.right, self.bottom = left, top, right, bottom

    class Control(FakeControl):
        def __init__(self, name, rect):
            super().__init__(name)
            self._rectangle = Rect(*rect)

        def rectangle(self):
            return self._rectangle

    label = Control("待处理即将逾期", (530, 440, 650, 475))
    badge = Control("0", (658, 440, 676, 475))
    unrelated = Control("99+", (1500, 90, 1540, 120))
    session._controls = lambda handle, visible_only=True: [label, badge, unrelated]

    assert session._aftersale_pending_badge_count(101) == 0


def test_zero_pending_badge_closes_ads_then_skips_filter_and_scroll(monkeypatch):
    session = PddAftersaleUiaSession.__new__(PddAftersaleUiaSession)
    session.config = {"aftersale_refresh_wait_seconds": 0}
    window = FakeWindow()
    keys = []
    events = []
    session.keyboard = SimpleNamespace(send_keys=lambda value: keys.append(value))
    session._list_window = lambda shop: window
    session._focus = lambda target: None
    session._current_url = lambda target: (
        "https://mms.pinduoduo.com/aftersales/aftersale_list?msfrom=mms_sidenav"
    )
    session._ensure_aftersale_list_surface = lambda *args, **kwargs: None
    session.assert_page_contract = lambda *args, **kwargs: None
    session._dismiss_overlay_popups = lambda target: events.append("ads_closed") or {
        "closed": 2,
        "clear": True,
    }
    session._aftersale_pending_badge_count = lambda handle: (
        events.append("badge_read") or 0
    )
    session._click_quick_pending = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("zero queue must not click the pending filter")
    )
    session._visible_cards = lambda handle: (_ for _ in ()).throw(
        AssertionError("zero queue must not scan cards")
    )
    monkeypatch.setattr("pdd_rpa.aftersale_uia.time.sleep", lambda *_: None)

    result = session.scan_pending(SHOP)

    assert result.total == 0
    assert keys == ["^r"]
    assert events == ["ads_closed", "badge_read"]
    assert session.last_collection_summary["skip_reason"] == "aftersale_pending_badge_zero"
