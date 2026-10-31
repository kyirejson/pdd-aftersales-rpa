from __future__ import annotations

from types import SimpleNamespace

import pytest

from pdd_rpa import aftersale_uia
from pdd_rpa.aftersale_uia import PddAftersaleUiaSession


class FakeKeyboard:
    def __init__(self, session):
        self.session = session
        self.keys: list[str] = []

    def send_keys(self, keys: str, **_kwargs):
        self.keys.append(keys)
        if keys not in {"^a", "{PGDN}", "{PGUP}", "^{END}", "^{HOME}"}:
            self.session.search.value = keys


class FakeMouse:
    def __init__(self):
        self.clicks: list[tuple[int, int]] = []

    def click(self, *, coords):
        self.clicks.append(coords)


class FakeWindow:
    handle = 101


class FakeDetailControl:
    def __init__(self, session):
        self.session = session
        self.clicks = 0
        self.physical_clicks = 0
        self.iface_invoke = SimpleNamespace(Invoke=self._invoke)

    def _invoke(self):
        self.clicks += 1
        self.session.detail_open = True

    def click_input(self):
        self.clicks += 1
        self.physical_clicks += 1
        self.session.detail_open = True


class FakeSearch:
    def __init__(self):
        self.value = ""
        self.iface_value = SimpleNamespace(SetValue=self._set_value)
        self.iface_scroll_item = SimpleNamespace(ScrollIntoView=lambda: None)

    def _set_value(self, value: str):
        self.value = value

    def get_value(self):
        return self.value


class FakeButton:
    def __init__(self):
        self.iface_invoke = SimpleNamespace(Invoke=lambda: None)


class FakeOrderText:
    def __init__(self, name: str):
        self.element_info = SimpleNamespace(name=name)


class FakeSearchSession(PddAftersaleUiaSession):
    def __init__(self):
        self.config = {
            "aftersale_scan_max_scrolls": 3,
            "aftersale_scan_wait_seconds": 0.1,
        }
        self.search = FakeSearch()
        self.search_button = FakeButton()
        self.keyboard = FakeKeyboard(self)
        self.mouse = FakeMouse()
        self.window = FakeWindow()
        self.detail_window = FakeWindow()
        self.detail_open = False
        self.pending_checks = 0
        self.target = FakeDetailControl(self)
        self.search_rect = (369, 324, 686, 351)
        self.target_rect = (1580, 500, 1660, 530)

    def _activate_detail_by_order(self, shop, order_no):
        return self.detail_window if self.detail_open else None

    def _list_window(self, shop, **_kwargs):
        return self.window

    def _click_quick_pending(self, window, shop):
        self.pending_checks += 1
        raise AssertionError("processing must not touch the pending quick-filter")

    def _focus(self, window):
        return None

    def _dismiss_overlay_popups(self, window):
        return {"closed": 0, "clear": True}

    def _detail_controls_for_order(self, handle, order_no):
        return [self.target]

    def _order_search(self, handle):
        return self.search

    def _order_search_button(self, handle, search):
        return self.search_button

    def _rect(self, control):
        if control is self.search:
            return self.search_rect
        if control is self.search_button:
            return (695, 329, 719, 346)
        return self.target_rect

    def assert_page_contract(self, *args, **kwargs):
        return None

    def _current_url(self, window):
        return "https://mms.pinduoduo.com/after-sales/detail"


def test_open_detail_searches_exact_order_then_uses_result_row(monkeypatch):
    monkeypatch.setattr(aftersale_uia.time, "sleep", lambda _seconds: None)
    session = FakeSearchSession()

    window, url = session.open_detail(
        {"name": "测试店铺"},
        "260000-241011470889208",
    )

    assert window is session.detail_window
    assert url == "https://mms.pinduoduo.com/after-sales/detail"
    assert session.pending_checks == 0
    assert session.target.clicks == 1
    assert session.target.physical_clicks == 0
    assert session.search.value == "260000-241011470889208"
    assert session.keyboard.keys == []
    assert session.mouse.clicks == []


def test_open_detail_searches_exact_aftersale_number_and_verifies_detail(monkeypatch):
    monkeypatch.setattr(aftersale_uia.time, "sleep", lambda _seconds: None)
    session = FakeSearchSession()
    expected = "AS123456789"
    detail_url = (
        "https://mms.pinduoduo.com/aftersales-ssr/detail?id=AS123456789"
    )
    session._current_url = lambda _window: detail_url
    session._controls = lambda _handle: []

    window, url = session.open_detail(
        {"name": "测试店铺"},
        "260000-241011470889208",
        search_value=expected,
        expected_aftersale_no=expected,
    )

    assert window is session.detail_window
    assert url == detail_url
    assert session.search.value == expected
    assert session.target.clicks == 1


def test_open_detail_fails_closed_when_aftersale_number_mismatches(monkeypatch):
    monkeypatch.setattr(aftersale_uia.time, "sleep", lambda _seconds: None)
    session = FakeSearchSession()
    session._current_url = lambda _window: (
        "https://mms.pinduoduo.com/aftersales-ssr/detail?id=AS999999999"
    )
    session._controls = lambda _handle: []
    closed = []
    session.close_detail = lambda window, shop: (
        closed.append((window, shop))
        or {"handled": True, "closed": 1, "list_ready": True}
    )

    with pytest.raises(aftersale_uia.PddUiaError, match="售后编号校验失败"):
        session.open_detail(
            {"name": "测试店铺"},
            "260000-241011470889208",
            search_value="AS123456789",
            expected_aftersale_no="AS123456789",
        )

    assert len(closed) == 1


class FakeRowControl:
    def __init__(self, name: str, rect: tuple[int, int, int, int]):
        self.element_info = SimpleNamespace(name=name)
        self._rect = rect


def test_detail_control_is_resolved_from_same_order_row():
    session = object.__new__(PddAftersaleUiaSession)
    order = FakeRowControl("260000-241011470889208", (290, 500, 470, 525))
    duplicate_order_text = FakeRowControl("260000-241011470889208", (490, 510, 670, 535))
    right_detail = FakeRowControl("查看详情", (1590, 510, 1660, 535))
    other_detail = FakeRowControl("查看详情", (1590, 710, 1660, 735))
    session._controls = lambda _handle: [order, duplicate_order_text, right_detail, other_detail]
    session._name = lambda control: control.element_info.name
    session._rect = lambda control: control._rect

    assert session._detail_controls_for_order(101, order.element_info.name) == [right_detail]


def test_shipped_refund_search_skips_closed_duplicate_and_selects_open_result():
    session = object.__new__(PddAftersaleUiaSession)
    closed_target = object()
    open_target = object()
    session._detail_controls_for_order = lambda _handle, _order: [
        closed_target,
        open_target,
    ]
    session._visible_cards = lambda _handle: [
        aftersale_uia.AftersaleCard(
            order_no="260000-241011470889208",
            aftersale_no="AS-CLOSED",
            refund_type="退款",
            shipping_status="已发货",
            status="商家退款关闭",
        ),
        aftersale_uia.AftersaleCard(
            order_no="260000-241011470889208",
            aftersale_no="AS-OPEN",
            refund_type="退款",
            shipping_status="已发货",
            status="待商家处理",
        ),
    ]

    selected, selected_aftersale_no = (
        session._select_shipped_refund_search_candidate(
            101, "260000-241011470889208"
        )
    )

    assert selected == 1
    assert selected_aftersale_no == "AS-OPEN"
    assert session.last_search_candidate_rows[0]["refund_closed"] is True
    assert session.last_search_candidate_rows[0]["eligible"] is False
    assert session.last_search_candidate_rows[1]["eligible"] is True


def test_shipped_refund_search_skips_succeeded_duplicate_and_selects_open_result():
    session = object.__new__(PddAftersaleUiaSession)
    succeeded_target = object()
    open_target = object()
    session._detail_controls_for_order = lambda _handle, _order: [
        succeeded_target,
        open_target,
    ]
    session._visible_cards = lambda _handle: [
        aftersale_uia.AftersaleCard(
            order_no="260000-003530767446000",
            aftersale_no="AS-SUCCEEDED",
            refund_type="退款",
            shipping_status="已发货",
            status="平台退款成功",
        ),
        aftersale_uia.AftersaleCard(
            order_no="260000-003530767446000",
            aftersale_no="AS-PENDING",
            refund_type="退款",
            shipping_status="已发货",
            status="待商家处理",
        ),
    ]

    selected, selected_aftersale_no = (
        session._select_shipped_refund_search_candidate(
            101, "260000-003530767446000"
        )
    )

    assert selected == 1
    assert selected_aftersale_no == "AS-PENDING"
    assert session.last_search_candidate_rows[0]["refund_succeeded"] is True
    assert session.last_search_candidate_rows[0]["eligible"] is False
    assert session.last_search_candidate_rows[1]["eligible"] is True


def test_shipped_refund_search_ignores_all_closed_without_opening_detail():
    session = object.__new__(PddAftersaleUiaSession)
    session._detail_controls_for_order = lambda _handle, _order: [object(), object()]
    session._visible_cards = lambda _handle: [
        aftersale_uia.AftersaleCard(
            order_no="260000-241011470889208",
            refund_type="退款",
            shipping_status="已发货",
            status="退款关闭",
        ),
        aftersale_uia.AftersaleCard(
            order_no="260000-241011470889208",
            refund_type="退款",
            shipping_status="已发货",
            status="商家退款关闭",
        ),
    ]

    with pytest.raises(
        aftersale_uia.PddAftersaleSearchResultsClosed,
        match="均为退款关闭",
    ):
        session._select_shipped_refund_search_candidate(
            101, "260000-241011470889208"
        )


def test_shipped_refund_search_ignores_closed_and_succeeded_terminal_results():
    session = object.__new__(PddAftersaleUiaSession)
    session._detail_controls_for_order = lambda _handle, _order: [object(), object()]
    session._visible_cards = lambda _handle: [
        aftersale_uia.AftersaleCard(
            order_no="260000-003530767446000",
            refund_type="退款",
            shipping_status="已发货",
            status="退款关闭",
        ),
        aftersale_uia.AftersaleCard(
            order_no="260000-003530767446000",
            refund_type="退款",
            shipping_status="已发货",
            status="商家退款成功",
        ),
    ]

    with pytest.raises(
        aftersale_uia.PddAftersaleSearchResultsClosed,
        match="退款关闭、退款成功",
    ):
        session._select_shipped_refund_search_candidate(
            101, "260000-003530767446000"
        )


def test_shipped_refund_search_ignores_row_that_changed_branch_before_processing():
    session = object.__new__(PddAftersaleUiaSession)
    session._detail_controls_for_order = lambda _handle, _order: [object()]
    session._visible_cards = lambda _handle: [
        aftersale_uia.AftersaleCard(
            order_no="260000-006772202885327",
            refund_type="退货退款",
            shipping_status="已发货",
            status="待商家处理",
        )
    ]

    with pytest.raises(
        aftersale_uia.PddAftersaleSearchResultsClosed,
        match="状态已变化并忽略",
    ):
        session._select_shipped_refund_search_candidate(
            101, "260000-006772202885327"
        )


def test_order_search_button_waits_for_transient_local_repaint(monkeypatch):
    monkeypatch.setattr(aftersale_uia.time, "sleep", lambda _seconds: None)
    session = object.__new__(PddAftersaleUiaSession)
    session.config = {"aftersale_search_button_wait_seconds": 1.0}
    search = FakeSearch()
    search.value = "260000-006772202885327"
    button = object()
    reads = {"count": 0}
    session._order_search = lambda _handle: search
    session._rect = lambda control: (
        (369, 324, 686, 351) if control is search else (695, 329, 719, 346)
    )
    session._name = lambda control: "搜索" if control is button else ""

    def controls(_handle):
        reads["count"] += 1
        return [] if reads["count"] == 1 else [button]

    session._controls = controls

    assert session._order_search_button(101, search) is button
    assert reads["count"] == 2


def test_read_detail_accepts_duplicate_order_nodes_when_detail_url_binds_order(
    monkeypatch,
):
    monkeypatch.setattr(aftersale_uia.time, "sleep", lambda _seconds: None)
    order_no = "260000-006772202885327"
    detail_url = (
        "https://mms.pinduoduo.com/aftersales-ssr/detail"
        f"?id=123&orderSn={order_no}"
    )
    session = object.__new__(PddAftersaleUiaSession)
    controls = [FakeOrderText(order_no), FakeOrderText(order_no)]
    session._current_url = lambda _window: detail_url
    session.assert_page_contract = lambda *_args, **_kwargs: {"ready": True}
    session._focus = lambda _window: None
    session.keyboard = SimpleNamespace(send_keys=lambda *_args, **_kwargs: None)
    session._controls = lambda _handle: controls
    session._current_aftersale_status_from_controls = lambda _controls: "待商家处理"
    session._detail_apply_time = lambda _controls: ""
    session._aftersale_no_from_values = lambda _values: "123"
    session._action_capabilities_from_controls = lambda _controls: {
        "action_controls_read_complete": True,
        "available_aftersale_actions": [],
        "has_agree_refund": False,
        "has_agree_return_after_refund": False,
        "has_agree_recall_after_refund": False,
        "has_reject_refund": False,
    }

    result = session.read_detail(
        FakeWindow(), order_no, detail_url, {"name": "测试店铺"}
    )

    assert result["order_no"] == order_no
    assert result["aftersale_status"] == "待商家处理"


def test_shipped_refund_search_fails_closed_when_two_open_results_are_ambiguous():
    session = object.__new__(PddAftersaleUiaSession)
    session._detail_controls_for_order = lambda _handle, _order: [object(), object()]
    session._visible_cards = lambda _handle: [
        aftersale_uia.AftersaleCard(
            order_no="260000-241011470889208",
            aftersale_no=f"AS-OPEN-{index}",
            refund_type="退款",
            shipping_status="已发货",
            status="待商家处理",
        )
        for index in (1, 2)
    ]

    with pytest.raises(aftersale_uia.PddUiaError, match="无法唯一确定目标"):
        session._select_shipped_refund_search_candidate(
            101, "260000-241011470889208"
        )


def test_open_detail_scrolls_filtered_result_with_uia_only(monkeypatch):
    monkeypatch.setattr(aftersale_uia.time, "sleep", lambda _seconds: None)
    session = FakeSearchSession()
    session.target_rect = (1580, 1120, 1660, 1150)
    session.target.iface_scroll_item = SimpleNamespace(
        ScrollIntoView=lambda: setattr(session, "target_rect", (1580, 820, 1660, 850))
    )

    window, _url = session.open_detail(
        {"name": "测试店铺"},
        "260000-241011470889208",
    )

    assert window is session.detail_window
    assert session.target.clicks == 1
    assert session.target.physical_clicks == 0
    assert session.keyboard.keys == []


def test_open_detail_uses_one_physical_fallback_when_invoke_is_unavailable(monkeypatch):
    monkeypatch.setattr(aftersale_uia.time, "sleep", lambda _seconds: None)
    session = FakeSearchSession()
    del session.target.iface_invoke

    window, _url = session.open_detail(
        {"name": "测试店铺"},
        "260000-241011470889208",
    )

    assert window is session.detail_window
    assert session.target.clicks == 1
    assert session.target.physical_clicks == 1


def test_open_detail_uses_exact_search_value_pattern_below_old_fixed_viewport(monkeypatch):
    monkeypatch.setattr(aftersale_uia.time, "sleep", lambda _seconds: None)
    session = FakeSearchSession()
    # This matches the real 1080p PDD workbench placement that used to fail:
    # the Edit is usable through ValuePattern even though its bottom is >1000.
    session.search_rect = (369, 1007, 686, 1034)
    session.search.iface_scroll_item = SimpleNamespace(
        ScrollIntoView=lambda: (_ for _ in ()).throw(
            AssertionError("search resolution must not scroll the page")
        )
    )

    window, _url = session.open_detail(
        {"name": "测试店铺"},
        "260000-241011470889208",
    )

    assert window is session.detail_window
    assert session.target.clicks == 1
    assert session.target.physical_clicks == 0
    assert session.keyboard.keys == []
    assert session.search.value == "260000-241011470889208"


def test_open_detail_fails_closed_when_search_is_not_enumerated(monkeypatch):
    monkeypatch.setattr(aftersale_uia.time, "sleep", lambda _seconds: None)
    session = FakeSearchSession()
    session.config["aftersale_search_wait_seconds"] = 0
    session._order_search = lambda _handle: (_ for _ in ()).throw(
        aftersale_uia.PddUiaError("售后订单搜索框不唯一：0")
    )

    try:
        session.open_detail(
            {"name": "测试店铺"},
            "260000-241011470889208",
        )
    except aftersale_uia.PddUiaError as exc:
        assert "已禁止全局滚动和坐标兜底" in str(exc)
    else:
        raise AssertionError("搜索框缺失时应安全失败")

    assert session.keyboard.keys == []
    assert session.mouse.clicks == []


def test_open_second_candidate_closes_stale_same_order_detail(monkeypatch):
    monkeypatch.setattr(aftersale_uia.time, "sleep", lambda _seconds: None)
    session = FakeSearchSession()
    session.detail_open = True
    first = FakeDetailControl(session)
    second = FakeDetailControl(session)
    session._detail_controls_for_order = lambda _handle, _order: [first, second]
    closed = []

    def close_detail(window, shop):
        closed.append((window, shop))
        session.detail_open = False
        return {"handled": True, "closed": 1, "list_ready": True}

    session.close_detail = close_detail

    window, _url = session.open_detail(
        {"name": "测试店铺"},
        "260000-241011470889208",
        candidate_index=1,
    )

    assert window is session.detail_window
    assert len(closed) == 1
    assert first.clicks == 0
    assert second.clicks == 1


class CloseDetailKeyboard:
    def __init__(self, session):
        self.session = session
        self.keys = []

    def send_keys(self, keys: str, **_kwargs):
        self.keys.append(keys)
        if keys == "^w":
            self.session.current_url = aftersale_uia.AFTERSALE_LIST_URL


class CloseDetailSession(PddAftersaleUiaSession):
    def __init__(self, *, list_available: bool):
        self.config = {"tab_switch_attempts": 2, "tab_cleanup_attempts": 4}
        self.window = FakeWindow()
        self.current_url = "https://mms.pinduoduo.com/aftersales-ssr/detail?id=1"
        self.list_available = list_available
        self.keyboard = CloseDetailKeyboard(self)
        self.navigation_calls = 0
        self.list_calls = 0

    def _current_url(self, _window):
        return self.current_url

    def _controls(self, _handle):
        return [FakeOrderText("260000-241011470889208")]

    def _name(self, control):
        return control.element_info.name

    def _has_exact_name(self, _handle, _name):
        return True

    def _window_matches_shop(self, _window, _shop):
        return True

    def assert_page_contract(self, *_args, **_kwargs):
        return {"ready": True}

    def _focus(self, _window):
        return None

    def _list_window(self, _shop, **_kwargs):
        self.list_calls += 1
        if not self.list_available:
            raise aftersale_uia.PddUiaError("测试店铺 未找到售后工作台列表页")
        self.current_url = aftersale_uia.AFTERSALE_LIST_URL
        return self.window

    def _activate_exact_detail_tab(self, _window, _shop, detail_url, _order_no, *, attempts):
        assert attempts == 4
        self.current_url = detail_url
        return True

    def _navigate_aftersale_list(self, _window, _shop=None):
        self.navigation_calls += 1
        self.current_url = aftersale_uia.AFTERSALE_LIST_URL


def test_close_detail_closes_detail_when_a_list_anchor_exists(monkeypatch):
    monkeypatch.setattr(aftersale_uia.time, "sleep", lambda _seconds: None)
    session = CloseDetailSession(list_available=True)

    cleanup = session.close_detail(session.window, {"name": "测试店铺"})

    assert cleanup["disposition"] == "detail_tab_closed_list_active"
    assert cleanup["closed"] == 1
    assert session.keyboard.keys == ["^w"]
    assert session.navigation_calls == 0
    assert session.list_calls == 1


def test_close_detail_reuses_current_tab_only_when_no_list_anchor_exists(monkeypatch):
    monkeypatch.setattr(aftersale_uia.time, "sleep", lambda _seconds: None)
    session = CloseDetailSession(list_available=False)

    cleanup = session.close_detail(session.window, {"name": "测试店铺"})

    assert cleanup["disposition"] == "detail_reused_as_single_list_anchor"
    assert cleanup["navigated"] == 1
    assert session.keyboard.keys == []
    assert session.navigation_calls == 1
