from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .components import validate_component
from .edge_uia import PddEdgeUiaSession, classify_pdd_url


@dataclass(slots=True)
class CaptureTarget:
    shop_id: str
    handle: int
    ready_at: float


class ElementCaptureService:
    """Capture Edge web controls from the Windows UIA tree under the cursor."""

    def __init__(self, pdd_config: dict[str, Any], shops: tuple[Any, ...]):
        self.pdd_config = pdd_config
        self.shops = shops
        self.targets: dict[str, CaptureTarget] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _shop(self, shop_id: str) -> dict[str, Any]:
        for shop in self.shops:
            if shop.id == shop_id and shop.enabled:
                return {"id": shop.id, "name": shop.name, "edge_profile": shop.edge_profile}
        raise KeyError(f"店铺不存在或已停用：{shop_id}")

    def _shop_window(self, session: PddEdgeUiaSession, shop: dict[str, Any]) -> Any:
        matches = []
        for window in session._windows():
            if shop["name"] not in window.window_text():
                continue
            kind = classify_pdd_url(session._current_url(window))
            if kind not in {"list", "detail", "login"}:
                continue
            # Login pages can omit the store name from the document tree; the
            # exact full store name in the Edge title is still required.  Once
            # on list/detail pages, retain the stronger UIA identity check.
            if kind == "login" or session._has_exact_name(int(window.handle), shop["name"]):
                matches.append(window)
        if len(matches) != 1:
            raise RuntimeError(f"店铺 Edge 窗口不唯一：{len(matches)}")
        return matches[0]

    def start(self, shop_id: str, delay_seconds: int = 3) -> dict[str, Any]:
        shop = self._shop(shop_id)
        session = PddEdgeUiaSession(self.pdd_config)
        window = self._shop_window(session, shop)
        delay = max(2, min(int(delay_seconds), 10))
        with self._lock:
            if any(state.get("status") == "capturing" for state in self.states.values()):
                raise RuntimeError("已有 Edge 元素捕获正在进行")
            self.targets[shop_id] = CaptureTarget(shop_id, int(window.handle), time.time() + delay)
            self.states[shop_id] = {"status": "capturing", "result": None, "delay_seconds": delay}
        threading.Thread(target=self._capture_after_delay, args=(shop_id, shop, delay), daemon=True).start()
        return {
            "status": "capturing",
            "shop_id": shop_id,
            "shop_name": shop["name"],
            "delay_seconds": delay,
            "instruction": f"请在 {delay} 秒内把鼠标移到目标网页控件上，不需要点击",
            "engine": "edge_uia",
        }

    def status(self, shop_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self.states.get(shop_id, {"status": "idle", "result": None}))

    def cancel(self, shop_id: str) -> dict[str, Any]:
        with self._lock:
            self.targets.pop(shop_id, None)
            self.states[shop_id] = {"status": "cancelled", "result": None}
            return dict(self.states[shop_id])

    def _capture_after_delay(self, shop_id: str, shop: dict[str, Any], delay: int) -> None:
        time.sleep(delay)
        with self._lock:
            if self.states.get(shop_id, {}).get("status") != "capturing":
                return
        try:
            result = self._capture_now(shop_id, shop)
            state = {"status": "captured", "result": result}
        except Exception as exc:
            state = {"status": "failed", "result": None, "error": str(exc)}
        with self._lock:
            if self.states.get(shop_id, {}).get("status") == "capturing":
                self.states[shop_id] = state
                self.targets.pop(shop_id, None)

    def _capture_now(self, shop_id: str, shop: dict[str, Any]) -> dict[str, Any]:
        import win32gui
        from pywinauto import Desktop

        target = self.targets[shop_id]
        x, y = win32gui.GetCursorPos()
        top = Desktop(backend="win32").top_from_point(x, y)
        if int(top.handle) != target.handle:
            raise RuntimeError("鼠标不在已绑定店铺的 Edge 窗口内")
        semantic = Desktop(backend="uia").from_point(x, y)
        info = semantic.element_info
        rect = semantic.rectangle()
        ancestry = []
        current = semantic
        for _ in range(7):
            try:
                item = current.element_info
                ancestry.append({
                    "name": str(getattr(item, "name", "") or ""),
                    "control_type": str(getattr(item, "control_type", "") or ""),
                    "automation_id": str(getattr(item, "automation_id", "") or ""),
                    "class_name": str(getattr(item, "class_name", "") or ""),
                })
                current = current.parent()
            except Exception:
                break
        try:
            semantic.draw_outline(colour="green", thickness=3)
        except Exception:
            pass
        return {
            "shop_id": shop_id,
            "shop_name": shop["name"],
            "window_title": top.window_text(),
            "element": {
                "name": str(getattr(info, "name", "") or ""),
                "control_type": str(getattr(info, "control_type", "") or ""),
                "automation_id": str(getattr(info, "automation_id", "") or ""),
                "class_name": str(getattr(info, "class_name", "") or ""),
                "rect": {"x": rect.left, "y": rect.top, "width": rect.width(), "height": rect.height()},
            },
            "ancestry": ancestry,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "engine": "edge_uia",
        }

    def capture_named(
        self, shop_id: str, text: str, control_type: str = ""
    ) -> dict[str, Any]:
        """Capture one exact visible UIA control without using screen coordinates."""
        shop = self._shop(shop_id)
        session = PddEdgeUiaSession(self.pdd_config)
        window = self._shop_window(session, shop)
        matches = session._visible_named_controls(
            int(window.handle), text, control_type
        )
        if len(matches) != 1:
            raise RuntimeError(
                f"元素“{text}”控件不唯一：{len(matches)}"
            )
        control = matches[0]
        info = control.element_info
        rect = control.rectangle()
        ancestry = []
        current = control
        for _ in range(7):
            try:
                item = current.element_info
                ancestry.append(
                    {
                        "name": str(getattr(item, "name", "") or ""),
                        "control_type": str(getattr(item, "control_type", "") or ""),
                        "automation_id": str(getattr(item, "automation_id", "") or ""),
                        "class_name": str(getattr(item, "class_name", "") or ""),
                    }
                )
                current = current.parent()
            except Exception:
                break
        return {
            "shop_id": shop_id,
            "shop_name": shop["name"],
            "window_title": window.window_text(),
            "element": {
                "name": str(getattr(info, "name", "") or ""),
                "control_type": str(getattr(info, "control_type", "") or ""),
                "automation_id": str(getattr(info, "automation_id", "") or ""),
                "class_name": str(getattr(info, "class_name", "") or ""),
                "rect": {
                    "x": rect.left,
                    "y": rect.top,
                    "width": rect.width(),
                    "height": rect.height(),
                },
            },
            "ancestry": ancestry,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "engine": "edge_uia",
        }

    @staticmethod
    def component_from_capture(
        capture: dict[str, Any], name: str, component_type: str = "ClickElement"
    ) -> dict[str, Any]:
        element = capture.get("element") or {}
        text = str(element.get("name") or capture.get("text") or "")
        control_type = str(
            element.get("control_type")
            or capture.get("uia_control_type")
            or capture.get("role")
            or ""
        )
        rect = element.get("rect") or capture.get("rect") or {}
        locator = {
            "text": text,
            "name": text,
            "uia_control_type": control_type,
            "automation_id": str(element.get("automation_id") or ""),
            "class_name": str(element.get("class_name") or capture.get("class_name") or ""),
            "ancestry": capture.get("ancestry") or [],
            "captured_rect": rect,
            "require_unique": True,
            "require_visible": True,
            "engine": "edge_uia",
        }
        return validate_component({
            "component_type": component_type,
            "name": name,
            "application": "Edge",
            "locator": locator,
            "before_assertions": ["店铺与 Profile 一致", "详情订单和工单 ID 一致", "UIA 元素唯一且可见"],
            "after_assertions": [],
            "on_failure": "MANUAL_REVIEW",
        })
