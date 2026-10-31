from __future__ import annotations

from types import SimpleNamespace

from pdd_rpa.aftersale_uia import AftersaleCard, PddAftersaleUiaSession


def _card(order_no: str) -> AftersaleCard:
    return AftersaleCard(order_no=order_no, refund_type="退货退款")


def test_collect_pending_pages_scans_forward_once_and_restores_first():
    session = PddAftersaleUiaSession.__new__(PddAftersaleUiaSession)
    session.config = {}
    current = {"page": 1}
    materialized: list[int] = []
    go_first_calls: list[bool] = []
    top_calls: list[int] = []

    class Next:
        def click_input(self):
            current["page"] += 1

    class Desktop:
        def window(self, handle):
            return handle

    def pagination_state(handle):
        page = current["page"]
        return {
            "total": 16,
            "current_page": page,
            "page_items": [(page, object())],
            "prev": object(),
            "next": Next(),
            "prev_disabled": page == 1,
            "next_disabled": page == 2,
        }

    def materialize(handle, shop, page_no):
        materialized.append(page_no)
        rows = {
            1: [_card("order-a"), _card("order-b")],
            2: [_card("order-b"), _card("order-c")],
        }
        cards = rows[page_no]
        for index, card in enumerate(cards):
            card.page_no = page_no
            card.page_index = index
        return cards

    def go_first(handle, ensure_pagination_visible=True):
        go_first_calls.append(ensure_pagination_visible)
        current["page"] = 1

    session.Desktop = lambda backend: Desktop()
    session._focus = lambda window: None
    session._pagination_state = pagination_state
    session._materialize_aftersale_page = materialize
    session._page_order_signature = lambda handle: (f"page-{current['page']}",)
    session._wait_for_page_change = lambda handle, old_page, old_signature: None
    session._go_to_first_page = go_first
    session._scroll_to_top = lambda handle: top_calls.append(handle)
    session.last_pending_badge_exact = True
    session._aftersale_pending_badge_count = lambda handle: 16

    cards = session._collect_pending_pages(101, {"name": "测试店铺"})

    assert materialized == [1, 2]
    assert go_first_calls == [True, False]
    assert top_calls == [101]
    assert [card.order_no for card in cards] == ["order-a", "order-b", "order-c"]
    assert [(card.page_no, card.page_index) for card in cards] == [
        (1, 0),
        (1, 1),
        (2, 1),
    ]
    assert session.last_collection_summary == {
        "filter": "待处理即将逾期",
        "discovered_cards": 3,
        "pending_badge_count": 16,
        "pending_badge_exact": True,
        "badge_vs_discovered_difference": 13,
        "badge_vs_pager_difference": 0,
        "pager_total": 16,
        "capture_complete": False,
        "capture_shortfall": 13,
        "skip_reason": "",
        "pages_scanned": 2,
        "single_pass": True,
        "single_page_hint": False,
    }


def test_collect_pending_pages_exact_small_badge_skips_pager_discovery():
    session = PddAftersaleUiaSession.__new__(PddAftersaleUiaSession)
    session.config = {}
    top_calls: list[int] = []
    session._materialize_aftersale_page = lambda handle, shop, page: [
        _card("order-a"),
        _card("order-b"),
    ]
    session._go_to_first_page = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("exact <=8 queue must not inspect pagination")
    )
    session._pagination_state = lambda *_args: (_ for _ in ()).throw(
        AssertionError("exact <=8 queue must not inspect pagination")
    )
    session._scroll_to_top = lambda handle: top_calls.append(handle)
    session.last_pending_badge_exact = True
    session._aftersale_pending_badge_count = lambda handle: 2

    cards = session._collect_pending_pages(
        101, {"name": "测试店铺"}, single_page_hint=True
    )

    assert [card.order_no for card in cards] == ["order-a", "order-b"]
    assert top_calls == [101]
    assert session.last_collection_summary["single_page_hint"] is True


def test_scan_pending_uses_paginated_collector(monkeypatch):
    session = PddAftersaleUiaSession.__new__(PddAftersaleUiaSession)
    session.config = {"aftersale_refresh_wait_seconds": 0}
    window = SimpleNamespace(handle=101, window_text=lambda: "测试店铺")
    keys: list[str] = []
    expected = [_card("order-a"), _card("order-b")]
    session.keyboard = SimpleNamespace(send_keys=lambda value: keys.append(value))
    session._list_window = lambda shop: window
    session._focus = lambda target: None
    session._current_url = lambda target: "https://mms.pinduoduo.com/aftersales/aftersale_list"
    session._ensure_aftersale_list_surface = lambda *args, **kwargs: None
    session.assert_page_contract = lambda *args, **kwargs: None
    session._dismiss_overlay_popups = lambda target: {"closed": 0, "clear": True}
    session._aftersale_pending_badge_count = lambda handle: 2
    session._click_quick_pending = lambda *args, **kwargs: None
    session._collect_pending_pages = lambda handle, shop, **_kwargs: expected
    monkeypatch.setattr("pdd_rpa.aftersale_uia.time.sleep", lambda *_: None)

    result = session.scan_pending({"name": "测试店铺"})

    assert result.cards == expected
    assert keys == ["^r"]


def test_materialize_pending_soon_overdue_waits_for_two_stable_rounds_and_samples_bottom(
    monkeypatch,
):
    session = PddAftersaleUiaSession.__new__(PddAftersaleUiaSession)
    session.config = {
        "aftersale_scan_max_scrolls": 5,
        "aftersale_scan_wait_seconds": 0.15,
    }
    visible_calls = {"count": 0}
    keys: list[str] = []
    bottom_calls: list[int] = []

    class Desktop:
        def window(self, handle):
            return handle

    def visible(_handle):
        visible_calls["count"] += 1
        if visible_calls["count"] == 4:
            return [_card("order-a"), _card("order-bottom")]
        return [_card("order-a")]

    session.Desktop = lambda backend: Desktop()
    session.keyboard = SimpleNamespace(send_keys=lambda value: keys.append(value))
    session._focus = lambda _window: None
    session.assert_page_contract = lambda *args, **kwargs: None
    session._scroll_to_top = lambda _handle: None
    session._scroll_to_pagination = lambda handle: bottom_calls.append(handle)
    session._visible_cards = visible
    monkeypatch.setattr("pdd_rpa.aftersale_uia.time.sleep", lambda *_: None)

    cards = session._materialize_aftersale_page(
        101, {"name": "测试店铺"}, page_no=1
    )

    assert keys == ["{PGDN}", "{PGDN}"]
    assert bottom_calls == [101]
    assert [card.order_no for card in cards] == ["order-a", "order-bottom"]


def test_pending_soon_overdue_fallback_identity_ignores_volatile_countdown_and_status():
    first = AftersaleCard(
        order_no="ORDER-1",
        refund_type="退款",
        shipping_status="已发货",
        refund_reason="0天23时4分24秒",
        refund_amount=12.34,
        status="待商家处理",
        apply_time="2026-08-24 10:20:30",
        urgency_text="0天23时4分24秒",
    )
    later = AftersaleCard(
        order_no="ORDER-1",
        refund_type="退款",
        shipping_status="已签收",
        refund_reason="不想要了",
        refund_amount=12.34,
        status="平台处理中",
        apply_time="2026-08-24 10:20:30",
        urgency_text="23时4分",
    )

    assert (
        PddAftersaleUiaSession._aftersale_card_identity(first)
        == PddAftersaleUiaSession._aftersale_card_identity(later)
    )


def test_pending_soon_overdue_urgency_recognizes_naked_countdowns():
    for value in ("0天23时4分24秒", "23时4分", "已逾期"):
        assert PddAftersaleUiaSession._looks_like_urgency_text(value)
    assert not PddAftersaleUiaSession._looks_like_urgency_text("七天无理由退款")
