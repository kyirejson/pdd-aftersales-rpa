from __future__ import annotations

from types import SimpleNamespace

from pdd_rpa.aftersale_uia import PddAftersaleUiaSession


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
    def __init__(self, name, control_type, class_name, rect, *, parent=None, on_click=None):
        self.element_info = SimpleNamespace(
            name=name,
            control_type=control_type,
            class_name=class_name,
            automation_id="",
        )
        self._rect = FakeRect(*rect)
        self._parent = parent
        self._on_click = on_click

    def rectangle(self):
        return self._rect

    def parent(self):
        return self._parent

    def click_input(self):
        if self._on_click:
            self._on_click()


class FakePopupSession(PddAftersaleUiaSession):
    def __init__(self):
        self.config = {
            "aftersale_popup_max_layers": 5,
            "aftersale_popup_quiet_seconds": 0,
            "aftersale_popup_total_wait_seconds": 0.5,
        }
        self.controls = []

    def _controls(self, handle, visible_only=False):
        return list(self.controls)

    def _dismiss_message_popup(self, window):
        return {"present": False, "closed": False}

    def _focus(self, window):
        return None


class FakeWindow:
    handle = 101


def test_blank_mdl_close_image_is_clicked_and_overlay_is_cleared(monkeypatch):
    monkeypatch.setattr("pdd_rpa.aftersale_uia.time.sleep", lambda *_: None)
    session = FakePopupSession()
    modal = FakeControl(
        "", "Group", "MDL_container_5-155-0", (4, 80, 1901, 1036)
    )
    wrapper = FakeControl(
        "", "Group", "MDL_iconWrapper_5-155-0", (1260, 342, 1293, 374), parent=modal
    )

    def close_overlay():
        session.controls = []

    close_image = FakeControl(
        "",
        "Image",
        "ICN_outerWrapper_5-155-0 MDL_headerCloseIcon_5-155-0",
        (1264, 346, 1289, 370),
        parent=wrapper,
        on_click=close_overlay,
    )
    session.controls = [modal, wrapper, close_image]

    result = session._dismiss_overlay_popups(FakeWindow())

    assert result == {"closed": 1, "clear": True}
    assert session.controls == []
