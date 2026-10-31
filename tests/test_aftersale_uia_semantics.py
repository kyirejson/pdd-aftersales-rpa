from __future__ import annotations

from types import SimpleNamespace

from pdd_rpa.aftersale_uia import PddAftersaleUiaSession


class Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class Control:
    def __init__(self, name: str, rect: tuple[int, int, int, int]):
        self.element_info = SimpleNamespace(
            name=name,
            automation_id="",
            rich_text="",
            control_type="Text",
        )
        self._rect = Rect(*rect)

    def rectangle(self):
        return self._rect


def test_pending_soon_overdue_card_parses_semantic_fields_and_excludes_countdown_reason():
    order_no = "260000-115319462101787"
    controls = [
        Control(order_no, (300, 400, 500, 425)),
        Control("退款", (780, 405, 840, 430)),
        Control("已发货", (900, 405, 970, 430)),
        Control("售后编号：AS123456789", (520, 405, 720, 430)),
        Control("售后状态：待商家处理", (980, 405, 1160, 430)),
        Control("申请时间：2026-08-24 10:20:30", (1100, 440, 1360, 465)),
        Control("申请原因：0天23时4分24秒", (1380, 405, 1570, 430)),
        Control("¥12.34", (650, 405, 720, 430)),
        Control("查看详情", (1580, 405, 1660, 430)),
    ]
    session = PddAftersaleUiaSession.__new__(PddAftersaleUiaSession)
    session._controls = lambda _handle: controls

    cards = session._visible_cards(101)

    assert len(cards) == 1
    card = cards[0]
    assert card.aftersale_no == "AS123456789"
    assert card.status == "待商家处理"
    assert card.apply_time == "2026-08-24 10:20:30"
    assert card.urgency_text == "申请原因：0天23时4分24秒"
    assert card.refund_reason == ""
    assert card.refund_amount == 12.34


def test_read_detail_returns_only_stable_semantic_aftersale_fields(monkeypatch):
    order_no = "260000-115319462101787"
    detail_url = (
        "https://mms.pinduoduo.com/aftersales-ssr/detail?id=AS123456789"
    )
    controls = [
        Control(order_no, (200, 100, 430, 125)),
        Control("售后类型：", (200, 150, 300, 175)),
        Control("退货退款", (310, 150, 390, 175)),
        Control("发货状态：", (200, 185, 300, 210)),
        Control("已发货", (310, 185, 380, 210)),
        Control("申请原因：", (200, 220, 300, 245)),
        Control("不想要了", (310, 220, 420, 245)),
        Control("售后状态：", (200, 255, 300, 280)),
        Control("待商家处理", (310, 255, 430, 280)),
        Control("申请时间：", (200, 290, 300, 315)),
        Control("2026-08-24 10:20:30", (310, 290, 500, 315)),
        Control("售后申请说明：", (200, 325, 330, 350)),
        Control("包装破损", (340, 325, 450, 350)),
        Control("退货物流公司：", (200, 360, 330, 385)),
        Control("圆通速递", (340, 360, 450, 385)),
        Control("退货快递单号：", (200, 395, 330, 420)),
        Control("YT4068262410", (340, 395, 500, 420)),
        Control("退款金额：", (200, 430, 300, 455)),
        Control("12.34", (310, 430, 380, 455)),
        Control("延长收货", (600, 500, 700, 530)),
    ]
    session = PddAftersaleUiaSession.__new__(PddAftersaleUiaSession)
    session.keyboard = SimpleNamespace(send_keys=lambda *_args, **_kwargs: None)
    session._current_url = lambda _window: detail_url
    session.assert_page_contract = lambda *args, **kwargs: None
    session._focus = lambda _window: None
    session._controls = lambda _handle: controls
    monkeypatch.setattr("pdd_rpa.aftersale_uia.time.sleep", lambda *_: None)

    facts = session.read_detail(
        SimpleNamespace(handle=101),
        order_no,
        detail_url,
        {"name": "测试店铺"},
    )

    assert facts["aftersale_no"] == "AS123456789"
    assert facts["aftersale_status"] == "待商家处理"
    assert facts["aftersale_apply_time"] == "2026-08-24 10:20:30"
    assert facts["aftersale_description"] == "包装破损"
    assert facts["return_carrier"] == "圆通速递"
    assert facts["return_tracking_no"] == "YT4068262410"
    assert facts["has_extend_receipt"] is True
