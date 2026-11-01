import pytest

from pdd_rpa.qianniu_uia import (
    AGREE_REFUND,
    INTERCEPT_EXPRESS,
    INTERCEPT_SUCCESS_FEEDBACK,
    QIANNIU_REFUND_DETAIL_URL,
    QIANNIU_REFUND_LIST_URL,
    QianniuUiaSession,
    QianniuUiaError,
    classify_qianniu_url,
    detail_aftersale_no,
    record_from_raw,
)


class _Mouse:
    def __init__(self):
        self.clicks = []
        self.moves = []

    def click(self, *, coords):
        self.clicks.append(coords)

    def scroll(self, **_kwargs):
        raise AssertionError("button should already be in viewport")

    def move(self, *, coords):
        self.moves.append(coords)


class _Prompt:
    def __init__(self):
        self.mouse = _Mouse()


class _Browser:
    def __init__(self):
        self.goto_calls = []
        self.reload_calls = 0
        self.press_calls = []

    def goto(self, url):
        self.goto_calls.append(url)
        return url

    def reload(self):
        self.reload_calls += 1

    def press(self, key):
        self.press_calls.append(key)


def test_scan_collects_every_page_with_one_probe_per_page(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    session.prompt = _Prompt()
    window = object()
    monkeypatch.setattr(session, "refresh_refund_list", lambda: {"ok": True})
    monkeypatch.setattr(
        session, "ensure_unreceived_refund_filter", lambda: {"ok": True}
    )
    monkeypatch.setattr(session, "window", lambda **_kwargs: window)
    monkeypatch.setattr(
        session, "dismiss_obstructions", lambda _window=None: {"ok": True}
    )

    def row(aftersale, order):
        return {
            "aftersale_no": aftersale,
            "order_no": order,
            "shipping_status": "已发货",
            "aftersale_status": "退款待处理",
            "refund_amount": "10.00",
            "refund_type": "未收货退款",
            "application_time": "2026-08-31 12:00:00",
            "available_actions": ["拦截快递", "查看详情"],
        }

    responses = iter(
        [
            {
                "total": 1,
                "rows": [row("405000000000000001", "512000000000000001")],
                "pagination": {
                    "has_next": True,
                    "next_count": 1,
                    "next": {"x": 800, "y": 700},
                },
            },
            {
                "total": 1,
                "rows": [row("405000000000000002", "512000000000000002")],
                "pagination": {"has_next": False, "next_count": 1},
            },
        ]
    )
    monkeypatch.setattr(session, "_probe", lambda *_args: next(responses))
    monkeypatch.setattr("pdd_rpa.qianniu_uia.time.sleep", lambda _seconds: None)

    records = session.scan_unreceived_refunds()

    assert [item.aftersale_no for item in records] == [
        "405000000000000001",
        "405000000000000002",
    ]
    assert session.prompt.mouse.clicks == [(800, 700)]


def test_compact_scan_rows_restore_business_fields():
    records = QianniuUiaSession._records_from_scan_payload(
        {
            "rows": [
                [
                    "405594252005293405",
                    "5127709501019017748",
                    "F",
                    "P",
                    "26.90",
                    "2026-08-31 12:56:38",
                    19,
                ]
            ]
        }
    )

    assert len(records) == 1
    assert records[0].aftersale_no == "405594252005293405"
    assert records[0].shipping_status == "已发货"
    assert records[0].aftersale_status == "退款待处理"
    assert records[0].available_actions == (
        "同意退款",
        "拦截快递",
        "查看详情",
    )


def test_refund_list_recovery_navigates_from_detail(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    session.config = type(
        "Config",
        (),
        {
            "shop_name": "示例天猫旗舰店",
            "launch_verify_timeout_seconds": 5,
        },
    )()
    window = object()
    keys = []
    session.prompt = type(
        "Prompt",
        (),
        {
            "keyboard": type(
                "Keyboard",
                (),
                {"send_keys": lambda _self, value, **_kwargs: keys.append(value)},
            )()
        },
    )()
    session.uia = type("Uia", (), {"_focus": lambda _self, _window: None})()
    session.browser = _Browser()
    monkeypatch.setattr(session, "window", lambda **_kwargs: window)
    urls = iter(
        [
            "https://myseller.taobao.com/home.htm/trade-platform/refund-list/detail"
            "?disputeId=282329940779825040&type=1",
            "https://myseller.taobao.com/home.htm/trade-platform/refund-list",
        ]
    )
    last_url = {"value": ""}

    def current_url(_window):
        try:
            last_url["value"] = next(urls)
        except StopIteration:
            pass
        return last_url["value"]

    monkeypatch.setattr(session, "_current_url", current_url)
    monkeypatch.setattr(
        session,
        "_probe",
        lambda _window, _expression: {"shop": True, "refund_list": True},
    )
    monkeypatch.setattr("pdd_rpa.qianniu_uia.time.sleep", lambda _seconds: None)

    assert session.ensure_refund_list_window() is window
    assert keys == []
    assert session.browser.goto_calls == [QIANNIU_REFUND_LIST_URL]


def test_scan_refreshes_list_before_filtering(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    calls = []
    window = object()
    monkeypatch.setattr(
        session, "refresh_refund_list", lambda: calls.append("refresh") or {"ok": True}
    )
    monkeypatch.setattr(
        session,
        "ensure_unreceived_refund_filter",
        lambda: calls.append("filter") or {"ok": True},
    )
    monkeypatch.setattr(session, "window", lambda **_kwargs: window)
    monkeypatch.setattr(
        session,
        "_probe",
        lambda _window, _expression: {"total": 0, "rows": []},
    )

    assert session.scan_unreceived_refunds() == []
    assert calls == ["refresh", "filter"]


def test_refresh_waits_through_transient_skeleton_until_list_is_ready(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    window = object()
    keys = []
    session.config = type(
        "Config",
        (),
        {
            "shop_name": "示例天猫旗舰店",
            "launch_verify_timeout_seconds": 12,
            "refresh_verify_timeout_seconds": 30,
        },
    )()
    session.prompt = type(
        "Prompt",
        (),
        {
            "keyboard": type(
                "Keyboard",
                (),
                {"send_keys": lambda _self, value, **_kwargs: keys.append(value)},
            )(),
        },
    )()
    session.uia = type("Uia", (), {"_focus": lambda _self, _window: None})()
    session.browser = _Browser()
    monkeypatch.setattr(session, "ensure_refund_list_window", lambda: window)
    monkeypatch.setattr(
        session,
        "_current_url",
        lambda _window: QIANNIU_REFUND_LIST_URL,
    )
    responses = iter(
        [
            {
                "ready": False,
                "shop": False,
                "refund_list": False,
                "tab_count": 0,
            },
            {
                "ready": True,
                "shop": True,
                "refund_list": True,
                "tab_count": 1,
            },
        ]
    )
    monkeypatch.setattr(session, "_probe", lambda *_args: next(responses))
    monkeypatch.setattr(
        session,
        "dismiss_obstructions",
        lambda _window: {"closed": 0},
    )
    monkeypatch.setattr("pdd_rpa.qianniu_uia.time.sleep", lambda _seconds: None)

    result = session.refresh_refund_list()

    assert result["ok"] is True
    assert keys == []
    assert session.browser.reload_calls == 1


def test_qianniu_urls_are_strictly_classified():
    assert (
        classify_qianniu_url(
            "https://myseller.taobao.com/home.htm/trade-platform/refund-list"
        )
        == "refund_list"
    )
    detail = (
        "https://myseller.taobao.com/home.htm/trade-platform/refund-list/detail"
        "?disputeId=405119198641780713&type=1"
    )
    assert classify_qianniu_url(detail) == "refund_detail"
    assert detail_aftersale_no(detail) == "405119198641780713"
    assert classify_qianniu_url("https://example.com/refund-list") == "other"


def test_record_marks_only_pending_unreceived_refund_processable():
    record = record_from_raw(
        {
            "aftersale_no": "405119198641780713",
            "order_no": "5127711625084043334",
            "shipping_status": "已发货",
            "aftersale_status": "退款待处理",
            "refund_amount": "19.90",
            "refund_type": "未收货退款",
            "application_time": "2026-08-28 13:06:13",
            "available_actions": ["同意退款", "拦截快递", "查看详情"],
        }
    )

    assert record.processable is True
    assert record.refund_amount == 19.9
    assert record.available_actions == ("同意退款", "拦截快递", "查看详情")


def test_pending_refund_without_intercept_action_is_not_processable():
    record = record_from_raw(
        {
            "aftersale_no": "405119198641780713",
            "order_no": "5127711625084043334",
            "shipping_status": "已发货",
            "aftersale_status": "退款待处理",
            "refund_amount": "19.90",
            "refund_type": "未收货退款",
            "application_time": "2026-08-28 13:06:13",
            "available_actions": ["同意退款", "查看详情"],
        }
    )

    assert record.processable is False


def test_record_rejects_invalid_business_identity():
    with pytest.raises(QianniuUiaError):
        record_from_raw(
            {
                "aftersale_no": "bad",
                "order_no": "5127711625084043334",
            }
        )


def test_logistics_carrier_line_supports_sudi_and_ignores_hotline():
    text = (
        "您的快件已到达妈妈驿站，请联系快递员13800000000，"
        "或致电专属客服95554。\n查看\n圆通速递 YT7640857296270复制"
    )

    assert QianniuUiaSession._parse_logistics_carrier_line(text) == (
        "圆通速递",
        "YT7640857296270",
    )


def test_intercept_clicks_optional_confirmation_and_marks_terminal(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    session.prompt = _Prompt()
    window = object()
    monkeypatch.setattr(session, "window", lambda **_kwargs: window)
    monkeypatch.setattr(session, "_ensure_detail_window", lambda _aftersale: window)
    monkeypatch.setattr(session, "dismiss_obstructions", lambda _window: {"ok": True})
    monkeypatch.setattr(
        session,
        "read_current_state",
        lambda **_kwargs: {
            "processable": True,
            "detail_url": "https://myseller.taobao.com/detail",
        },
    )
    monkeypatch.setattr(
        session,
        "_action_target",
        lambda _window, text: {
            "ready": text == INTERCEPT_EXPRESS,
            "inViewport": True,
            "point": {"x": 100, "y": 200},
        },
    )
    monkeypatch.setattr(
        session,
        "_confirm_dialog_if_present",
        lambda _window, **_kwargs: {
            "present": True,
            "clicked": True,
            "button_text": "确认",
        },
    )

    result = session.execute_intercept(
        order_no="5127711625084043334",
        aftersale_no="405119198641780713",
    )

    assert session.prompt.mouse.clicks == [(100, 200)]
    assert result["ok"] is True
    assert result["terminal"] == "EXPRESS_INTERCEPTION_SUBMITTED"
    assert result["manual_refund_followup"] is True
    assert result["business_write_performed"] is True


def test_intercept_stops_cleanly_when_operator_already_handled(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    session.prompt = _Prompt()
    window = object()
    monkeypatch.setattr(session, "window", lambda **_kwargs: window)
    monkeypatch.setattr(session, "_ensure_detail_window", lambda _aftersale: window)
    monkeypatch.setattr(session, "dismiss_obstructions", lambda _window: {"ok": True})
    monkeypatch.setattr(
        session,
        "read_current_state",
        lambda **_kwargs: {
            "processable": False,
            "aftersale_status": "退款成功",
            "available_actions": [],
        },
    )
    monkeypatch.setattr(
        session,
        "_action_target",
        lambda *_args, **_kwargs: pytest.fail("external handling must prevent click"),
    )

    result = session.execute_intercept(
        order_no="5127711625084043334",
        aftersale_no="405119198641780713",
    )

    assert result["externally_handled"] is True
    assert result["business_write_performed"] is False
    assert session.prompt.mouse.clicks == []


def test_intercept_success_feedback_is_verified_terminal_without_confirm(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    monkeypatch.setattr(
        session,
        "_probe",
        lambda _window, _expression: {
            "present": True,
            "dialogs": 1,
            "confirms": 0,
            "button_texts": [],
            "dialog_texts": [INTERCEPT_SUCCESS_FEEDBACK],
        },
    )

    result = session._confirm_dialog_if_present(
        object(), wait_seconds=0.1, action_text=INTERCEPT_EXPRESS
    )

    assert result == {
        "present": True,
        "clicked": False,
        "button_text": "",
        "terminal_verified": True,
        "terminal_feedback": INTERCEPT_SUCCESS_FEEDBACK,
    }


def test_agree_refund_requires_confirmation(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    session.prompt = _Prompt()
    window = object()
    monkeypatch.setattr(session, "window", lambda **_kwargs: window)
    monkeypatch.setattr(session, "_ensure_detail_window", lambda _aftersale: window)
    monkeypatch.setattr(session, "dismiss_obstructions", lambda _window: {"ok": True})
    monkeypatch.setattr(
        session,
        "read_current_state",
        lambda **_kwargs: {
            "processable": True,
            "detail_url": "https://myseller.taobao.com/detail",
        },
    )
    monkeypatch.setattr(
        session,
        "_action_target",
        lambda _window, _text: {"point": {"x": 10, "y": 20}},
    )
    monkeypatch.setattr(
        session,
        "_confirm_dialog_if_present",
        lambda _window, **_kwargs: {"present": False, "clicked": False},
    )

    with pytest.raises(QianniuUiaError, match="未出现可确认弹窗"):
        session.execute_agree_refund(
            order_no="5127711625084043334",
            aftersale_no="405119198641780713",
        )


def test_logistics_hover_moves_into_balloon_and_counts_direct_nodes(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    session.prompt = _Prompt()
    window = object()
    monkeypatch.setattr(session, "window", lambda **_kwargs: window)
    monkeypatch.setattr(session, "_ensure_detail_window", lambda _aftersale: window)
    monkeypatch.setattr(session, "dismiss_obstructions", lambda _window: {"ok": True})
    monkeypatch.setattr(session, "read_current_state", lambda **_kwargs: {})
    responses = iter(
        [
            {"ready": True, "count": 1, "point": {"x": 1323, "y": 742}},
            {
                "ready": True,
                "count": 1,
                "trajectory_count": 5,
                "first_text": "第一条",
                "last_text": "第五条",
            },
        ]
    )
    monkeypatch.setattr(session, "_probe", lambda *_args: next(responses))
    monkeypatch.setattr("pdd_rpa.qianniu_uia.time.sleep", lambda _seconds: None)

    result = session.read_logistics_trajectory(
        order_no="5127711625084043334",
        aftersale_no="405119198641780713",
    )

    assert session.prompt.mouse.moves == [(1323, 742), (1323, 602)]
    assert result["logistics_trajectory_count"] == 5
    assert result["logistics_trajectory_read_complete"] is True


def _fast_clock(monkeypatch):
    clock = {"t": 100.0}

    def fake_monotonic():
        clock["t"] += 5.0
        return clock["t"]

    monkeypatch.setattr("pdd_rpa.qianniu_uia.time.monotonic", fake_monotonic)


def test_unreceived_filter_waits_for_async_tab_render(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    session.config = type("Config", (), {"launch_verify_timeout_seconds": 5})()
    window = object()
    session.prompt = _Prompt()
    responses = iter(
        [
            {"ready": False, "count": 0},
            {"ready": False, "count": 0},
            {"ready": True, "count": 1, "active": True},
            {"ready": True, "count": 1, "active": True},
        ]
    )
    monkeypatch.setattr(session, "window", lambda **_kwargs: window)
    monkeypatch.setattr(session, "dismiss_obstructions", lambda _window: {"ok": True})
    monkeypatch.setattr(session, "_probe", lambda *_args: next(responses))
    monkeypatch.setattr("pdd_rpa.qianniu_uia.time.sleep", lambda _seconds: None)

    result = session.ensure_unreceived_refund_filter()

    assert result["ok"] is True
    assert result["clicked"] is False
    assert session.prompt.mouse.clicks == []


def test_unreceived_filter_waits_for_active_state_after_click(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    session.config = type("Config", (), {"launch_verify_timeout_seconds": 5})()
    window = object()
    session.prompt = _Prompt()
    responses = iter(
        [
            {
                "ready": True,
                "count": 1,
                "active": False,
                "point": {"x": 420, "y": 180},
            },
            {"ready": False, "count": 0},
            {"ready": True, "count": 1, "active": True},
        ]
    )
    monkeypatch.setattr(session, "window", lambda **_kwargs: window)
    monkeypatch.setattr(session, "dismiss_obstructions", lambda _window: {"ok": True})
    monkeypatch.setattr(session, "_probe", lambda *_args: next(responses))
    monkeypatch.setattr("pdd_rpa.qianniu_uia.time.sleep", lambda _seconds: None)

    result = session.ensure_unreceived_refund_filter()

    assert result["ok"] is True
    assert result["clicked"] is True
    assert session.prompt.mouse.clicks == [(420, 180)]


def test_open_detail_navigates_by_dispute_url_without_button_click(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    session.config = type("Config", (), {"launch_verify_timeout_seconds": 5})()
    window = object()
    keys = []
    session.prompt = type(
        "Prompt",
        (),
        {
            "mouse": _Mouse(),
            "keyboard": type(
                "Keyboard",
                (),
                {"send_keys": lambda _self, value, **_kwargs: keys.append(value)},
            )(),
        },
    )()
    session.uia = type("Uia", (), {"_focus": lambda _self, _window: None})()
    session.browser = _Browser()
    aftersale = "405291277784151781"
    detail_url = QIANNIU_REFUND_DETAIL_URL.format(aftersale_no=aftersale)
    monkeypatch.setattr(session, "window", lambda **_kwargs: window)
    monkeypatch.setattr(session, "_current_url", lambda _window: detail_url)
    monkeypatch.setattr(
        session, "_probe", lambda *_args: pytest.fail("open_detail must not prompt")
    )
    monkeypatch.setattr("pdd_rpa.qianniu_uia.time.sleep", lambda _seconds: None)

    result = session.open_detail("5127393926066002543", aftersale)

    assert keys == []
    assert session.browser.goto_calls == [detail_url]
    assert session.prompt.mouse.clicks == []
    assert result["ok"] is True
    assert result["detail_url"] == detail_url


def test_open_detail_raises_when_navigation_never_lands_on_detail(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    session.config = type("Config", (), {"launch_verify_timeout_seconds": 5})()
    window = object()
    keys = []
    session.prompt = type(
        "Prompt",
        (),
        {
            "keyboard": type(
                "Keyboard",
                (),
                {"send_keys": lambda _self, value, **_kwargs: keys.append(value)},
            )()
        },
    )()
    session.uia = type("Uia", (), {"_focus": lambda _self, _window: None})()
    session.browser = _Browser()
    monkeypatch.setattr(session, "window", lambda **_kwargs: window)
    monkeypatch.setattr(session, "_current_url", lambda _window: QIANNIU_REFUND_LIST_URL)
    monkeypatch.setattr("pdd_rpa.qianniu_uia.time.sleep", lambda _seconds: None)
    _fast_clock(monkeypatch)

    with pytest.raises(QianniuUiaError, match="未进入目标售后单详情页"):
        session.open_detail("5127393926066002543", "405291277784151781")


def test_confirm_dialog_reclicks_until_dialog_closes(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    session.prompt = _Prompt()
    window = object()
    responses = iter(
        [
            {
                "present": True,
                "confirms": 1,
                "text": "同意退款",
                "point": {"x": 500, "y": 400},
            },
            {
                "present": True,
                "confirms": 1,
                "text": "同意退款",
                "point": {"x": 502, "y": 401},
            },
            {"present": False},
        ]
    )
    monkeypatch.setattr(session, "_probe", lambda *_args: next(responses))
    monkeypatch.setattr("pdd_rpa.qianniu_uia.time.sleep", lambda _seconds: None)

    result = session._confirm_dialog_if_present(
        window, wait_seconds=3.0, action_text=AGREE_REFUND
    )

    assert result["clicked"] is True
    assert result["click_attempts"] == 2
    assert session.prompt.mouse.clicks == [(500, 400), (502, 401)]


def test_confirm_dialog_raises_when_click_never_closes(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    session.prompt = _Prompt()
    window = object()
    monkeypatch.setattr(
        session,
        "_probe",
        lambda *_args: {
            "present": True,
            "confirms": 1,
            "text": "同意退款",
            "point": {"x": 500, "y": 400},
        },
    )
    monkeypatch.setattr("pdd_rpa.qianniu_uia.time.sleep", lambda _seconds: None)

    with pytest.raises(QianniuUiaError, match="仍未关闭"):
        session._confirm_dialog_if_present(
            window, wait_seconds=3.0, action_text=AGREE_REFUND
        )

    assert len(session.prompt.mouse.clicks) == 3


def test_agree_refund_reports_verified_refund_outcome(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    session.prompt = _Prompt()
    window = object()
    monkeypatch.setattr(session, "window", lambda **_kwargs: window)
    monkeypatch.setattr(session, "_ensure_detail_window", lambda _aftersale: window)
    monkeypatch.setattr(session, "dismiss_obstructions", lambda _window: {"ok": True})
    monkeypatch.setattr(
        session,
        "read_current_state",
        lambda **_kwargs: {
            "processable": True,
            "detail_url": "https://myseller.taobao.com/detail",
        },
    )
    monkeypatch.setattr(
        session,
        "_action_target",
        lambda _window, _text: {"point": {"x": 10, "y": 20}},
    )
    monkeypatch.setattr(
        session,
        "_confirm_dialog_if_present",
        lambda _window, **_kwargs: {
            "present": True,
            "clicked": True,
            "button_text": "同意退款",
        },
    )
    monkeypatch.setattr(
        session,
        "_probe",
        lambda _window, _expression: {"agree_remaining": 0, "refund_success": True},
    )
    monkeypatch.setattr("pdd_rpa.qianniu_uia.time.sleep", lambda _seconds: None)

    result = session.execute_agree_refund(
        order_no="5127217455017018107",
        aftersale_no="405273961532043549",
    )

    assert result["ok"] is True
    assert result["terminal"] == "REFUND_CONFIRMED"
    assert result["refund_outcome"]["verified"] is True


def test_agree_refund_raises_when_refund_never_takes_effect(monkeypatch):
    session = object.__new__(QianniuUiaSession)
    session.prompt = _Prompt()
    window = object()
    monkeypatch.setattr(session, "window", lambda **_kwargs: window)
    monkeypatch.setattr(session, "_ensure_detail_window", lambda _aftersale: window)
    monkeypatch.setattr(session, "dismiss_obstructions", lambda _window: {"ok": True})
    monkeypatch.setattr(
        session,
        "read_current_state",
        lambda **_kwargs: {
            "processable": True,
            "detail_url": "https://myseller.taobao.com/detail",
        },
    )
    monkeypatch.setattr(
        session,
        "_action_target",
        lambda _window, _text: {"point": {"x": 10, "y": 20}},
    )
    monkeypatch.setattr(
        session,
        "_confirm_dialog_if_present",
        lambda _window, **_kwargs: {
            "present": True,
            "clicked": True,
            "button_text": "同意退款",
        },
    )
    monkeypatch.setattr(
        session,
        "_probe",
        lambda _window, _expression: {"agree_remaining": 1, "refund_success": False},
    )
    monkeypatch.setattr("pdd_rpa.qianniu_uia.time.sleep", lambda _seconds: None)
    _fast_clock(monkeypatch)

    with pytest.raises(QianniuUiaError, match="未出现退款成功状态"):
        session.execute_agree_refund(
            order_no="5127217455017018107",
            aftersale_no="405273961532043549",
        )
