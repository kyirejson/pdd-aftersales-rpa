from __future__ import annotations

from types import SimpleNamespace

import pytest

from pdd_rpa.edge_uia import PddEdgeUiaSession, PddUiaError


LIST_URL = (
    "https://mms.pinduoduo.com/aftersales/work_order/list?msfrom=mms_sidenav"
)


class FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class FakeControl:
    def __init__(
        self,
        name: str,
        control_type: str,
        class_name: str,
        rect: tuple[int, int, int, int],
        *,
        parent=None,
        on_click=None,
    ):
        self.element_info = SimpleNamespace(
            name=name,
            control_type=control_type,
            class_name=class_name,
            automation_id="",
        )
        self._rect = FakeRect(*rect)
        self._parent = parent
        self._children = []
        self._on_click = on_click
        self.clicked = False
        if parent is not None:
            parent._children.append(self)

    def rectangle(self):
        return self._rect

    def parent(self):
        return self._parent

    def descendants(self):
        result = []
        pending = list(self._children)
        while pending:
            item = pending.pop(0)
            result.append(item)
            pending.extend(item._children)
        return result

    def click_input(self):
        self.clicked = True
        if self._on_click:
            self._on_click()


class FakePopupSession(PddEdgeUiaSession):
    def __init__(self):
        self.config = {
            "pdd_popup_max_layers": 6,
            "pdd_popup_quiet_seconds": 0.2,
            "pdd_popup_total_wait_seconds": 0.7,
        }
        self.controls = []

    def _current_url(self, window):
        return LIST_URL

    def _controls(self, handle, visible_only=False):
        return list(self.controls)

    def _focus(self, window):
        return None


class FakeWindow:
    handle = 101


def _modal(session: FakePopupSession, *, business_text: str = ""):
    modal = FakeControl(
        "", "Group", "MDL_container_5-155-0", (400, 180, 1500, 850)
    )
    if business_text:
        FakeControl(
            business_text,
            "Text",
            "",
            (520, 300, 1100, 350),
            parent=modal,
        )
    wrapper = FakeControl(
        "",
        "Group",
        "MDL_iconWrapper_5-155-0",
        (1420, 205, 1460, 245),
        parent=modal,
    )

    def close_overlay():
        session.controls = []

    close = FakeControl(
        "",
        "Image",
        "MDL_headerCloseIcon_5-155-0",
        (1425, 210, 1455, 240),
        parent=wrapper,
        on_click=close_overlay,
    )
    session.controls = [modal, *modal.descendants()]
    return close


def test_known_mdl_ad_is_closed_and_uia_tree_is_rescanned(monkeypatch):
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda *_: None)
    session = FakePopupSession()
    close = _modal(session)

    result = session._dismiss_known_pdd_overlays(FakeWindow())

    assert result["closed"] == 1
    assert result["clear"] is True
    assert close.clicked is True
    assert session.controls == []


def test_business_confirmation_mdl_is_never_closed(monkeypatch):
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda *_: None)
    session = FakePopupSession()
    close = _modal(session, business_text="是否确认同意退款并提交")

    with pytest.raises(PddUiaError, match="业务确认弹窗"):
        session._dismiss_known_pdd_overlays(FakeWindow())

    assert close.clicked is False
    assert session.controls

