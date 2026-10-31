from types import SimpleNamespace

import pytest

from pdd_rpa.edge_uia import PddEdgeUiaSession, PddUiaError, UiaCard
from pdd_rpa.desktop_executor import DesktopComponentExecutor


def _session(config=None):
    session = object.__new__(PddEdgeUiaSession)
    session.config = dict(config or {})
    return session


def _card(order, page=1, index=0, action="立即处理"):
    return UiaCard(
        order_no=order,
        work_order_title="title",
        action_name=action,
        order_rect=(300, 100, 500, 120),
        action_rect=(700, 100, 800, 120),
        page_no=page,
        page_index=index,
    )


def test_wait_for_page_change_reexposes_pager_after_render():
    session = _session({"page_navigation_timeout_seconds": 2})
    states = iter(
        [
            {"current_page": None},
            {"current_page": 2},
        ]
    )
    signatures = iter([("old",), ("new",)])
    scrolls = []
    session._pagination_state = lambda handle: next(states)
    session._page_order_signature = lambda handle: next(signatures)
    session._scroll_to_pagination = lambda handle: scrolls.append(handle)

    session._wait_for_page_change(10, 1, ("old",))

    assert scrolls == [10]


def test_wait_for_page_change_supplements_ctrl_end_only_once():
    session = _session({"page_navigation_timeout_seconds": 2})
    states = iter(
        [
            {"current_page": None},
            {"current_page": None},
            {"current_page": None},
            {"current_page": 2},
        ]
    )
    signatures = iter([("old",), ("old",), ("old",), ("new",)])
    scrolls = []
    session._pagination_state = lambda handle: next(states)
    session._page_order_signature = lambda handle: next(signatures)
    session._scroll_to_pagination = lambda handle: scrolls.append(handle)

    session._wait_for_page_change(10, 1, ("old",))

    assert scrolls == [10]


def test_pending_filter_badge_count_reads_only_adjacent_number():
    session = _session()

    class Rect:
        def __init__(self, left, top, right, bottom):
            self.left = left
            self.top = top
            self.right = right
            self.bottom = bottom

    class Control:
        def __init__(self, name, rect, parent=None):
            self.element_info = SimpleNamespace(name=name)
            self._rect = Rect(*rect)
            self._parent = parent

        def rectangle(self):
            return self._rect

        def parent(self):
            if self._parent is None:
                raise RuntimeError("no parent")
            return self._parent

    wrapper = Control("", (390, 200, 490, 235))
    pending = Control("待处理", (400, 205, 445, 225), wrapper)
    badge = Control("12", (450, 205, 468, 225), wrapper)
    unrelated_page = Control("2", (1700, 850, 1720, 870), wrapper)
    session._controls = lambda handle, visible_only=True: [
        pending,
        badge,
        unrelated_page,
    ]

    assert session._pending_filter_badge_count(10, pending) == 12


def test_pending_filter_badge_count_returns_none_when_badge_is_absent():
    session = _session()

    class Rect:
        left = 400
        top = 205
        right = 445
        bottom = 225

    class Control:
        element_info = SimpleNamespace(name="待处理")

        def rectangle(self):
            return Rect()

        def parent(self):
            raise RuntimeError("no parent")

    pending = Control()
    session._controls = lambda handle, visible_only=True: [pending]

    assert session._pending_filter_badge_count(10, pending) is None


def test_refresh_pending_list_without_badge_skips_filter_click_and_page_scan(monkeypatch):
    session = _session({"refresh_timeout_seconds": 2})
    session.last_collection_summary = {}
    shop = {"name": "测试图书专营店"}
    scrolls = []
    keys = []

    class Window:
        handle = 10

        def window_text(self):
            return "测试图书专营店 - Microsoft Edge"

    class Pending:
        def click_input(self):
            raise AssertionError("empty pending filter must not be clicked")

    class Keyboard:
        def send_keys(self, value):
            keys.append(value)

    window = Window()
    pending = Pending()
    session.keyboard = Keyboard()
    session._shop_windows = lambda selected_shop: [window]
    session.ensure_shop_list = lambda selected_shop: None
    session.window_for_shop = lambda selected_shop, kind: window
    session.assert_page_contract = lambda *args, **kwargs: None
    session._scroll_to_top = lambda handle: scrolls.append(handle)
    session._focus = lambda target: None
    session._dismiss_known_pdd_overlays = lambda target: {
        "closed": 0,
        "clear": True,
    }
    session._current_url = lambda target: (
        "https://mms.pinduoduo.com/aftersales/work_order/list?msfrom=mms_sidenav"
    )
    session._has_exact_name = lambda handle, text: True
    session._pending_filter = lambda handle: pending
    session._pending_filter_badge_count = lambda handle, control: None
    session._pending_filter_selected = lambda control: (_ for _ in ()).throw(
        AssertionError("selection state must not be read for an empty queue")
    )
    session._collect_all_pages = lambda handle: (_ for _ in ()).throw(
        AssertionError("empty queue must not start pagination")
    )
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda seconds: None)

    result = session.refresh_pending_list(shop)

    assert result.total == 0
    assert result.cards == []
    assert keys == ["^r"]
    assert scrolls == [10]
    assert session.last_collection_summary["skip_reason"] == "pending_badge_missing"


def test_missing_pager_is_accepted_as_single_page():
    state = {
        "page_items": [],
        "prev": None,
        "next": None,
        "total": 4,
    }

    assert PddEdgeUiaSession._pager_absent_single_page(state)


def test_focus_uses_title_bar_fallback_when_set_focus_is_foreground_locked():
    session = _session()

    class Win32Gui:
        foreground = 0
        zoomed = False

        @classmethod
        def GetForegroundWindow(cls):
            return cls.foreground

        @classmethod
        def IsZoomed(cls, _handle):
            return cls.zoomed

        @classmethod
        def ShowWindow(cls, _handle, command):
            assert command == 3
            cls.zoomed = True

    class Window:
        handle = 99
        titlebar_clicks = []

        def is_minimized(self):
            return False

        def is_maximized(self):
            return Win32Gui.zoomed

        def maximize(self):
            Win32Gui.zoomed = True

        def set_focus(self):
            return None

        def click_input(self, *, coords):
            self.titlebar_clicks.append(coords)
            Win32Gui.foreground = self.handle

    session.win32gui = Win32Gui
    original = DesktopComponentExecutor._force_foreground
    DesktopComponentExecutor._force_foreground = staticmethod(lambda handle: False)
    try:
        session._focus(Window())
    finally:
        DesktopComponentExecutor._force_foreground = original

    assert Window.titlebar_clicks == [(5, 5)]


def test_focus_rejects_foreground_window_when_maximize_cannot_be_verified(monkeypatch):
    session = _session()

    class Win32Gui:
        @staticmethod
        def GetForegroundWindow():
            return 99

    class Window:
        handle = 99

        def is_minimized(self):
            return False

        def set_focus(self):
            return None

        def click_input(self, *, coords):
            return None

    session.win32gui = Win32Gui
    session._ensure_window_maximized = lambda window: False
    monkeypatch.setattr(
        DesktopComponentExecutor,
        "_force_foreground",
        staticmethod(lambda handle: True),
    )
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda *_: None)

    with pytest.raises(PddUiaError, match="未置前或未最大化|未能确认最大化"):
        session._focus(Window())


def test_card_identity_keeps_distinct_work_order_rows_for_one_order():
    first = _card("260000-100745846944040")
    second = _card("260000-100745846944040")
    second.work_order_title = "other title"

    assert PddEdgeUiaSession._card_identity(first) != PddEdgeUiaSession._card_identity(second)


def test_card_identity_ignores_page_when_a_row_moves_during_scan():
    first = _card("260000-100745846944040", page=1)
    moved = _card("260000-100745846944040", page=2)

    assert PddEdgeUiaSession._card_identity(first) == PddEdgeUiaSession._card_identity(moved)


def test_materialize_page_accumulates_virtualized_rows():
    session = _session(
        {
            "page_materialize_max_scrolls": 4,
            "page_materialize_wait_seconds": 0.15,
        }
    )
    rows = iter(
        [
            [_card("a") , _card("b")],
            [_card("b"), _card("c")],
            [_card("a"), _card("b"), _card("c")],
            [_card("a"), _card("b"), _card("c")],
        ]
    )
    keys = []
    scroll_to_top = []
    scroll_to_pager = []

    class Keyboard:
        def send_keys(self, value):
            keys.append(value)

    class Desktop:
        def window(self, handle):
            return handle

    session.keyboard = Keyboard()
    session.Desktop = lambda backend: Desktop()
    session._focus = lambda window: None
    session._scroll_to_top = lambda handle: scroll_to_top.append(handle)
    session._scroll_to_pagination = lambda handle: scroll_to_pager.append(handle)
    session._cards = lambda handle, page_no=1: next(rows)

    cards = session._materialize_page_cards(10, 2)

    assert [card.order_no for card in cards] == ["a", "b", "c"]
    assert all(card.page_no == 2 for card in cards)
    assert [card.page_index for card in cards] == [0, 1, 2]
    assert scroll_to_top == [10]
    assert scroll_to_pager == [10]
    assert keys == ["{PGDN}", "{PGDN}"]


def test_materialize_page_stops_after_one_stable_snapshot_without_total_count():
    session = _session({"page_materialize_max_scrolls": 4})
    keys = []
    scroll_to_top = []
    scroll_to_pager = []

    class Keyboard:
        def send_keys(self, value):
            keys.append(value)

    class Desktop:
        def window(self, handle):
            return handle

    session.keyboard = Keyboard()
    session.Desktop = lambda backend: Desktop()
    session._focus = lambda window: None
    session._scroll_to_top = lambda handle: scroll_to_top.append(handle)
    session._scroll_to_pagination = lambda handle: scroll_to_pager.append(handle)
    session._cards = lambda handle, page_no=1: [_card("a"), _card("b")]

    cards = session._materialize_page_cards(10, 2)

    assert [card.order_no for card in cards] == ["a", "b"]
    assert keys == ["{PGDN}"]
    assert scroll_to_top == [10]
    assert scroll_to_pager == [10]


def test_collect_all_pages_accepts_explicitly_empty_pending_list():
    session = _session()
    session._scroll_to_pagination = lambda handle: None
    session._go_to_first_page = lambda handle, ensure_pagination_visible=False: None
    session._pagination_state = lambda handle: {
        "total": 0,
        "page_items": [],
        "prev": None,
        "next": None,
    }
    session._scroll_to_top = lambda handle: None

    assert session._collect_all_pages(10) == (0, [])


def test_collect_all_pages_exact_small_badge_skips_pager_discovery():
    session = _session()
    top_calls = []
    session._materialize_page_cards = lambda handle, page: [
        _card("260000-043836278410068"),
        _card("260000-120709065949367"),
    ]
    session._go_to_first_page = lambda *_args, **_kwargs: pytest.fail(
        "exact <=8 queue must not inspect pagination"
    )
    session._pagination_state = lambda *_args: pytest.fail(
        "exact <=8 queue must not inspect pagination"
    )
    session._scroll_to_top = lambda handle: top_calls.append(handle)

    total, cards = session._collect_all_pages(10, single_page_hint=True)

    assert total == 2
    assert [card.order_no for card in cards] == [
        "260000-043836278410068",
        "260000-120709065949367",
    ]
    assert top_calls == [10]
    assert session.last_collection_summary["single_page_hint"] is True


def test_collect_all_pages_scans_forward_once_deduplicates_and_returns_to_first():
    session = _session({"page_retry_per_page": 1})
    current = {"page": 1}
    go_first_calls = []
    materialized = []
    clicks = []

    class Next:
        def click_input(self):
            current["page"] += 1
            clicks.append(current["page"])

    class Desktop:
        def window(self, handle):
            return handle

    def go_first(handle, ensure_pagination_visible=True):
        go_first_calls.append(ensure_pagination_visible)
        current["page"] = 1

    def pagination_state(handle):
        page = current["page"]
        return {
            "total": 999,
            "current_page": page,
            "page_items": [(page, object())],
            "prev": object(),
            "next": Next(),
            "prev_disabled": page == 1,
            "next_disabled": page == 3,
        }

    def materialize(handle, page_no):
        materialized.append(page_no)
        rows = {
            1: [_card("a", page=1), _card("b", page=1)],
            2: [_card("b", page=2), _card("c", page=2)],
            3: [_card("d", page=3)],
        }
        return rows[page_no]

    session.Desktop = lambda backend: Desktop()
    session._focus = lambda window: None
    session._go_to_first_page = go_first
    session._pagination_state = pagination_state
    session._materialize_page_cards = materialize
    session._page_order_signature = lambda handle: (f"page-{current['page']}",)
    session._wait_for_page_change = lambda handle, old_page, old_signature: None
    session._scroll_to_top = lambda handle: None

    total, cards = session._collect_all_pages(10)

    assert materialized == [1, 2, 3]
    assert clicks == [2, 3]
    assert go_first_calls == [True, False]
    assert [card.order_no for card in cards] == ["a", "b", "c", "d"]
    assert total == 4
    assert session.last_collection_summary == {
        "discovered_cards": 4,
        "pages_scanned": 3,
        "single_pass": True,
    }


def test_collect_all_pages_retries_only_current_page_without_extra_ctrl_end():
    session = _session({"page_retry_per_page": 2})
    attempts = []
    pagination_scrolls = []
    go_first_calls = []

    # Production materialization failures use PddUiaError; make the first
    # attempt return an empty list so the current-page retry path is exercised.
    def materialize_empty_then_ready(handle, page_no):
        attempts.append(page_no)
        return [] if len(attempts) == 1 else [_card("a", page=page_no)]

    session._go_to_first_page = lambda handle, ensure_pagination_visible=True: go_first_calls.append(
        ensure_pagination_visible
    )
    session._pagination_state = lambda handle: {
        "total": 1,
        "current_page": 1,
        "page_items": [(1, object())],
        "prev": None,
        "next": None,
        "prev_disabled": True,
        "next_disabled": True,
    }
    session._materialize_page_cards = materialize_empty_then_ready
    session._scroll_to_pagination = lambda handle: pagination_scrolls.append(handle)
    session._scroll_to_top = lambda handle: None

    total, cards = session._collect_all_pages(10)

    assert attempts == [1, 1]
    assert pagination_scrolls == []
    assert go_first_calls == [True, False]
    assert total == 1
    assert [card.order_no for card in cards] == ["a"]


def test_collect_all_pages_never_ingests_processed_view_cards():
    session = _session({"page_retry_per_page": 1})
    session._go_to_first_page = lambda handle, ensure_pagination_visible=True: None
    session._pagination_state = lambda handle: {
        "total": 2,
        "current_page": 1,
        "page_items": [(1, object())],
        "prev": None,
        "next": None,
        "prev_disabled": True,
        "next_disabled": True,
    }
    session._pager_absent_single_page = lambda state: False
    session._materialize_page_cards = lambda handle, page_no: [
        _card("pending", action="立即处理"),
        _card("processed", action="立查看"),
    ]
    session._scroll_to_top = lambda handle: None

    total, cards = session._collect_all_pages(10)

    assert total == 1
    assert [card.order_no for card in cards] == ["pending"]


def test_latest_logistics_prefers_forwarding_line_over_lower_service_annotation():
    candidates = [
        ("\u3010\u65e0\u9521\u5e02\u3011\u5feb\u4ef6\u5df2\u53d1\u5f80 \u65e0\u9521\u8f6c\u8fd0\u4e2d\u5fc3", 670),
        ("\u3010\u5ba2\u670d\u70ed\u7ebf\uff1a95311\u3011", 720),
    ]

    assert PddEdgeUiaSession._select_latest_logistics_text(candidates) == candidates[0][0]


def test_latest_logistics_accepts_suzhou_forwarding_line():
    candidates = [
        ("\u3010\u82cf\u5dde\u5e02\u3011\u5feb\u4ef6\u5df2\u53d1\u5f80 \u82cf\u5dde\u5206\u62e8\u4e2d\u5fc3", 660),
        ("\u3010\u5ba2\u670d\u70ed\u7ebf\uff1a95311\u3011", 730),
    ]

    assert PddEdgeUiaSession._select_latest_logistics_text(candidates) == candidates[0][0]


def test_latest_logistics_accepts_departed_wuxi_transfer_center_line():
    candidates = [
        ("您的快件离开【无锡转运中心】，正在发往下一站", 680),
        ("【客服热线：95311】", 730),
    ]

    assert PddEdgeUiaSession._select_latest_logistics_text(candidates) == candidates[0][0]


def test_controls_skips_transient_untyped_uia_element_without_aborting_tree():
    session = _session()

    class RawInfo:
        def __init__(self, control_type):
            self.control_type = control_type

    class Wrapped:
        def __init__(self, info):
            self.element_info = info

        def is_visible(self):
            return True

    class RawRoot:
        def descendants(self):
            return [RawInfo(None), RawInfo("Text"), RawInfo("Button")]

    class RootWindow:
        element_info = RawRoot()
        backend = SimpleNamespace(generic_wrapper_class=Wrapped)

        def descendants(self):
            raise AssertionError("bulk wrapper traversal must not be used")

    class Desktop:
        def window(self, handle):
            assert handle == 42
            return RootWindow()

    session.Desktop = lambda backend: Desktop()

    controls = session._controls(42, visible_only=True)

    assert [control.element_info.control_type for control in controls] == ["Text", "Button"]


def test_detail_state_recognizes_split_completed_and_agreed_labels():
    session = _session()

    class Control:
        def __init__(self, name, control_type="Text", visible=True):
            self.element_info = SimpleNamespace(name=name, control_type=control_type)
            self._visible = visible

        def is_visible(self):
            return self._visible

    class Desktop:
        def window(self, handle):
            return object()

    session.Desktop = lambda backend: Desktop()
    session._current_url = lambda window: (
        "https://mms.pinduoduo.com/aftersales/work_order/tododetail?id=40000000000000"
    )
    session._controls = lambda handle: [
        Control("\u3010"),
        Control("\u5df2\u5b8c\u7ed3"),
        Control("\u3011"),
        Control("\u5ba2\u670d\u5df2\u540c\u610f\u9000\u6b3e"),
        Control("\u534f\u5546\u7ed3\u679c"),
        Control("\u540c\u610f\u6d88\u8d39\u8005\u9000\u6b3e\u7533\u8bf7"),
        Control("\u6d88\u8d39\u8005\u64a4\u9500"),
        Control("260000-201608166391037"),
    ]

    result = session.detail_state(42, "260000-201608166391037", "40000000000000")

    assert result["completed"] is True
    assert result["merchant_agreed"] is True
    assert result["has_agree_refund"] is True
    assert result["withdraw_requested"] is True
    assert result["withdraw_followup_required"] is False
    assert result["visible_forms"] == 0


def test_option_label_prefers_radio_bound_control_over_progress_history():
    session = _session()

    class Rect:
        def __init__(self, top):
            self.left = 200
            self.top = top
            self.right = 420
            self.bottom = top + 18

        def height(self):
            return self.bottom - self.top

    class Control:
        def __init__(self, name, control_type, top, visible=True):
            self.element_info = SimpleNamespace(name=name, control_type=control_type)
            self._rect = Rect(top)
            self._visible = visible

        def rectangle(self):
            return self._rect

        def is_visible(self):
            return self._visible

    option_text = "\u6d88\u8d39\u8005\u64a4\u9500\u552e\u540e\u7533\u8bf7"
    label = Control(option_text, "Text", 416)
    history = Control(option_text, "Text", 581)
    radio = Control("\ue6c6 " + option_text, "RadioButton", 418, visible=False)
    session._controls = lambda handle, visible_only=True: [label, history, radio]

    result = session.inspect_exact(42, option_text, "Text")

    assert result["ready"] is True
    assert result["rect"][1] == 416
