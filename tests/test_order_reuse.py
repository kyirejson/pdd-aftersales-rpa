from __future__ import annotations

import pytest

from pdd_rpa.edge_uia import (
    PddAlreadyProcessedError,
    PddEdgeUiaSession,
    PddOrderNotFoundError,
    PddUiaError,
    UiaCard,
)


SHOP = {"id": "shop_01", "name": "示例书店 A", "edge_profile": "Profile"}
ORDER = "260000-207446580937970"


def _session_without_native_ui():
    return PddEdgeUiaSession.__new__(PddEdgeUiaSession)


def test_open_detail_reuses_existing_list_before_refresh(monkeypatch):
    session = _session_without_native_ui()
    existing = object()
    session.window_for_shop = lambda shop, kind: existing
    session._open_detail_from_list = lambda shop, order_no, window: (window, "detail")
    session.refresh_pending_list = lambda shop: pytest.fail("不应重新刷新工单列表")

    assert session.open_detail(SHOP, ORDER) == (existing, "detail")


def test_open_detail_falls_back_to_refresh_when_list_is_missing():
    session = _session_without_native_ui()
    refreshed = object()
    session.window_for_shop = lambda shop, kind: (_ for _ in ()).throw(PddUiaError("list missing"))
    session.refresh_pending_list = lambda shop: type("Snapshot", (), {"handle": 7})()
    session.Desktop = lambda backend: type(
        "Desktop", (), {"window": lambda self, handle: refreshed}
    )()
    session._open_detail_from_list = lambda shop, order_no, window: (window, "detail")

    assert session.open_detail(SHOP, ORDER) == (refreshed, "detail")


def test_open_detail_refreshes_once_when_existing_list_has_no_filter_controls():
    session = _session_without_native_ui()
    existing = object()
    refreshed = object()
    calls = []
    session.window_for_shop = lambda shop, kind: existing
    session.refresh_pending_list = lambda shop: type("Snapshot", (), {"handle": 7})()
    session.Desktop = lambda backend: type(
        "Desktop", (), {"window": lambda self, handle: refreshed}
    )()

    def open_detail(shop, order_no, window):
        calls.append(window)
        if window is existing:
            raise PddUiaError("订单筛选控件不唯一：edit=0, query=0")
        return window, "detail"

    session._open_detail_from_list = open_detail

    assert session.open_detail(SHOP, ORDER) == (refreshed, "detail")
    assert calls == [existing, refreshed]


def test_existing_list_marks_missing_order_as_skipped(monkeypatch):
    session = _session_without_native_ui()
    session._focus = lambda window: None
    session._dismiss_known_pdd_overlays = lambda window: {"closed": 0, "clear": True}
    session.assert_page_contract = lambda *args, **kwargs: None
    session._set_order_filter = lambda handle, value, **kwargs: None
    session._cards = lambda handle: []
    session._query_total = lambda handle: 0
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda seconds: None)

    with pytest.raises(PddOrderNotFoundError):
        session._open_detail_from_list(SHOP, ORDER, type("Window", (), {"handle": 1})())


def test_existing_list_marks_view_only_order_as_already_processed(monkeypatch):
    session = _session_without_native_ui()
    session._focus = lambda window: None
    session._dismiss_known_pdd_overlays = lambda window: {"closed": 0, "clear": True}
    session.assert_page_contract = lambda *args, **kwargs: None
    session._set_order_filter = lambda handle, value, **kwargs: None
    session._cards = lambda handle: [
        UiaCard(ORDER, "已发货无轨迹退款处理", "立即查看", (0, 0, 1, 1), (0, 0, 1, 1))
    ]
    session._query_total = lambda handle: 1
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda seconds: None)

    with pytest.raises(PddAlreadyProcessedError):
        session._open_detail_from_list(SHOP, ORDER, type("Window", (), {"handle": 1})())


def test_existing_list_accepts_two_stable_unique_rows_without_total(monkeypatch):
    session = _session_without_native_ui()
    session.config = {
        "order_filter_timeout_seconds": 5,
        "order_filter_poll_seconds": 0.15,
        "detail_open_poll_seconds": 0.15,
    }
    window = type("Window", (), {"handle": 1})()
    action_rect = (700, 100, 800, 120)
    card = UiaCard(
        ORDER,
        "已发货无轨迹退款处理",
        "立即处理",
        (300, 100, 500, 120),
        action_rect,
    )
    action = type("Action", (), {"name": "立即处理", "rect": action_rect})()
    reads = {"cards": 0}
    session._focus = lambda _window: None
    session._dismiss_known_pdd_overlays = lambda _window: {"closed": 0, "clear": True}
    session.assert_page_contract = lambda *args, **kwargs: None
    session._set_order_filter = lambda handle, value, **kwargs: None

    def cards(_handle):
        reads["cards"] += 1
        return [card]

    session._cards = cards
    session._query_total = lambda _handle: None
    session._controls = lambda _handle: [action]
    session._name = lambda control: control.name
    session._rect = lambda control: control.rect
    session._invoke_with_uia = lambda control: control is action
    session._current_url = lambda _window: (
        "https://mms.pinduoduo.com/aftersales/work_order/tododetail?id=1"
    )
    session._has_exact_name = lambda _handle, value: value == ORDER
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda _seconds: None)

    opened, url = session._open_detail_from_list(SHOP, ORDER, window)

    assert opened is window
    assert "tododetail" in url
    assert reads["cards"] == 3
