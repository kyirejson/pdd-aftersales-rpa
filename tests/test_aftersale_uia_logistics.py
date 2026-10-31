from types import SimpleNamespace

import pytest

from pdd_rpa.aftersale_uia import PddAftersaleUiaSession
from pdd_rpa.edge_uia import PddUiaError


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


def session_with(controls):
    session = object.__new__(PddAftersaleUiaSession)
    session._controls = lambda _handle: list(controls)
    return session


def test_expanded_logistics_pairs_event_with_time_and_uses_latest_timestamp():
    controls = [
        Control("物流轨迹", (342, 100, 407, 124)),
        Control("韵达快递", (360, 130, 430, 150)),
        Control("40000000000000", (360, 155, 500, 175)),
        Control("商家已发货", (370, 200, 700, 225)),
        Control("2026-08-24 13:30:17", (370, 240, 560, 260)),
        Control("已到达苏州分拨中心", (370, 300, 760, 325)),
        Control("2026-08-25 02:05:26", (370, 340, 560, 360)),
        Control("快件运输中", (370, 400, 700, 425)),
        Control("2026-08-24 15:21:07", (370, 440, 560, 460)),
        Control("备注：", (342, 900, 407, 924)),
    ]

    result = session_with(controls)._read_expanded_logistics(101)

    assert result["logistics_read_complete"] is True
    assert result["logistics_trajectory_count"] == 3
    assert result["latest_logistics_text"] == "已到达苏州分拨中心"
    assert result["logistics_latest_signed"] is False
    assert [row["timestamp"] for row in result["logistics_trace_rows"]] == [
        "2026-08-25 02:05:26",
        "2026-08-24 15:21:07",
        "2026-08-24 13:30:17",
    ]


def test_expanded_logistics_detects_signed_only_from_latest_event():
    controls = [
        Control("物流轨迹", (342, 100, 407, 124)),
        Control("派送中", (370, 200, 700, 225)),
        Control("2026-08-24 13:30:17", (370, 240, 560, 260)),
        Control("已签收，驿站代收", (370, 300, 760, 325)),
        Control("2026-08-25 02:05:26", (370, 340, 560, 360)),
        Control("备注", (342, 900, 407, 924)),
    ]

    result = session_with(controls)._read_expanded_logistics(101)

    assert result["latest_logistics_text"] == "已签收，驿站代收"
    assert result["logistics_latest_signed"] is True


def test_expanded_logistics_fails_closed_when_timestamp_has_no_event():
    controls = [
        Control("物流轨迹", (342, 100, 407, 124)),
        Control("2026-08-25 02:05:26", (370, 340, 560, 360)),
        Control("备注", (342, 900, 407, 924)),
    ]

    with pytest.raises(PddUiaError, match="没有匹配到对应轨迹内容"):
        session_with(controls)._read_expanded_logistics(101)
