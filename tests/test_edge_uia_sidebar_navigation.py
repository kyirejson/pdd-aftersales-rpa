from __future__ import annotations

from types import SimpleNamespace

import pytest

from pdd_rpa.edge_uia import PddEdgeUiaSession, PddUiaError


class FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class FakeControl:
    def __init__(
        self,
        name: str,
        control_type: str,
        class_name: str,
        rect: tuple[int, int, int, int],
        *,
        automation_id: str = "",
        parent=None,
    ):
        self.element_info = SimpleNamespace(
            name=name,
            control_type=control_type,
            class_name=class_name,
            automation_id=automation_id,
        )
        self._rect = FakeRect(*rect)
        self._parent = parent
        self.clicked = False

    def rectangle(self):
        return self._rect

    def parent(self):
        return self._parent

    def click_input(self):
        self.clicked = True


class FakeWindow:
    handle = 101


def _nav_pair(
    name: str,
    rect: tuple[int, int, int, int],
    *,
    automation_id: str,
    grouped: bool,
):
    side_nav = FakeControl("", "Group", "side-nav", (4, 136, 216, 1000))
    parent = side_nav
    if grouped:
        parent = FakeControl(
            "", "List", "nav-item-group-content", (20, 600, 216, 850), parent=side_nav
        )
    item = FakeControl(
        name.replace(" ", ""),
        "ListItem",
        "nav-item",
        rect,
        automation_id=automation_id,
        parent=parent,
    )
    link = FakeControl(name, "Hyperlink", "", rect, parent=item)
    return item, link


class FakeSidebarSession(PddEdgeUiaSession):
    def __init__(self, controls):
        self.config = {"sidebar_navigation_timeout_seconds": 3}
        self.controls = controls
        self.focused = False

    def _controls(self, handle, visible_only=False):
        return self.controls

    def _focus(self, window):
        self.focused = True

    def _ensure_window_maximized(self, window):
        return True

    def _dismiss_known_pdd_overlays(self, window):
        return {"closed": 0, "clear": True}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("售后工作台", True),
        ("售后工作台 85", True),
        ("售后工作台85", True),
        ("售后工作台 99+", True),
        ("售后工作台公告", False),
        ("工单管理 - Microsoft Edge", False),
    ],
)
def test_sidebar_label_ignores_only_numeric_badge(name, expected):
    assert PddEdgeUiaSession._sidebar_label_matches(name, "售后工作台") is expected


def test_sidebar_navigation_prefers_stable_grouped_entry(monkeypatch):
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda *_: None)
    shortcut_item, shortcut_link = _nav_pair(
        "售后工作台 85", (20, 250, 104, 281), automation_id="", grouped=False
    )
    stable_item, stable_link = _nav_pair(
        "售后工作台 85",
        (20, 670, 104, 701),
        automation_id="__msfe__side-nav_item_211",
        grouped=True,
    )
    session = FakeSidebarSession(
        [shortcut_item, shortcut_link, stable_item, stable_link]
    )

    session._click_sidebar_entry(FakeWindow(), "售后工作台")

    assert session.focused is True
    assert stable_link.clicked is True
    assert shortcut_link.clicked is False
    assert stable_item.clicked is False


def test_sidebar_navigation_rejects_non_navigation_title(monkeypatch):
    monkeypatch.setattr("pdd_rpa.edge_uia.time.sleep", lambda *_: None)
    document = FakeControl(
        "工单管理", "Document", "", (4, 80, 1916, 1036)
    )
    heading = FakeControl("工单管理", "Text", "", (232, 150, 312, 177))
    session = FakeSidebarSession([document, heading])

    with pytest.raises(PddUiaError, match="未发现左侧导航可点击候选"):
        session._click_sidebar_entry(FakeWindow(), "工单管理")
