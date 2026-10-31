from types import SimpleNamespace

from pdd_rpa.edge_uia import PddEdgeUiaSession


class Rect:
    def __init__(self, left, top, right, bottom):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class Control:
    def __init__(self, name, rect):
        self.element_info = SimpleNamespace(name=name)
        self._rect = Rect(*rect)

    def rectangle(self):
        return self._rect


def test_tracking_text_removes_invisible_and_visual_spaces():
    assert PddEdgeUiaSession._tracking_values_from_text("YT0702\u200b6608 19480") == [
        "YT0702660819480"
    ]


def test_tracking_text_does_not_join_hyphenated_service_phone_number():
    assert PddEdgeUiaSession._tracking_values_from_text(
        "如有问题请致电 0510-66120702"
    ) == []


def test_detail_tracking_candidates_join_only_same_logistics_row_fragments():
    session = object.__new__(PddEdgeUiaSession)
    controls = [
        Control("物流信息：", (100, 500, 180, 525)),
        Control("中通快递", (190, 500, 260, 525)),
        Control("7913299", (280, 500, 345, 525)),
        Control("4567075", (350, 500, 415, 525)),
        Control("0510", (200, 600, 240, 625)),
        Control("260000-017330474674056", (100, 300, 330, 325)),
    ]

    candidates = session._detail_tracking_candidates(controls, "260000-017330474674056")

    assert [value for _control, value, _rect in candidates] == ["79132994567075"]


def test_detail_tracking_candidates_do_not_join_phone_fragments_outside_logistics_row():
    session = object.__new__(PddEdgeUiaSession)
    controls = [
        Control("物流信息：", (100, 500, 180, 525)),
        Control("中通快递", (190, 500, 260, 525)),
        Control("40000000000000", (280, 500, 415, 525)),
        Control("客服电话 0510-66120702", (200, 600, 430, 625)),
    ]

    candidates = session._detail_tracking_candidates(controls, "260000-017330474674056")

    assert [value for _control, value, _rect in candidates] == ["40000000000000"]


def test_detail_tracking_candidates_reject_hotline_below_real_waybill():
    session = object.__new__(PddEdgeUiaSession)
    controls = [
        Control("物流信息：", (100, 500, 180, 525)),
        Control("韵达快递", (190, 500, 260, 525)),
        Control("40000000000000", (280, 500, 430, 525)),
        Control(
            "如有异常请拨打专属电话：051485593622",
            (190, 548, 520, 575),
        ),
    ]

    candidates = session._detail_tracking_candidates(
        controls, "260000-142921188223677"
    )

    assert [value for _control, value, _rect in candidates] == [
        "40000000000000"
    ]


def test_detail_carrier_uses_logistics_row_not_exact_fragment_baseline():
    session = object.__new__(PddEdgeUiaSession)
    controls = [
        Control("物流信息：", (100, 500, 180, 525)),
        Control("中通快递", (190, 506, 260, 531)),
        Control("7913299", (280, 520, 345, 545)),
        Control("4567075", (350, 520, 415, 545)),
        Control("圆通快递", (190, 700, 260, 725)),
    ]

    assert session._detail_carrier(controls, (280, 520, 415, 545)) == "中通快递"
