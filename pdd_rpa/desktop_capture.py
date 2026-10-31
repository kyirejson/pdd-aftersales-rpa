from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .components import validate_component


class DesktopCaptureService:
    """Yingdao-style delayed capture: record the control under the cursor without clicking it."""

    def __init__(self, evidence_dir: Path):
        self.evidence_dir = Path(evidence_dir)
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"status": "idle", "result": None}

    def start(self, application: str = "旺店通ERP旗舰版", delay_seconds: int = 3) -> dict[str, Any]:
        with self._lock:
            if self._state.get("status") == "capturing":
                raise RuntimeError("已有桌面元素捕获正在进行")
            delay = max(2, min(int(delay_seconds), 10))
            self._state = {
                "status": "capturing",
                "result": None,
                "application": application,
                "ready_at": time.time() + delay,
            }
        threading.Thread(target=self._capture_after_delay, args=(application, delay), daemon=True).start()
        return {
            "status": "capturing",
            "delay_seconds": delay,
            "instruction": f"请在 {delay} 秒内把鼠标移动到目标控件上，不需要点击",
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            if self._state.get("status") == "capturing":
                self._state = {"status": "cancelled", "result": None}
            return dict(self._state)

    def _capture_after_delay(self, application: str, delay: int) -> None:
        time.sleep(delay)
        with self._lock:
            if self._state.get("status") != "capturing":
                return
        try:
            result = self._capture_now(application)
            state = {"status": "captured", "result": result}
        except Exception as exc:
            state = {"status": "failed", "result": None, "error": str(exc)}
        with self._lock:
            if self._state.get("status") == "capturing":
                self._state = state

    def _capture_now(self, application: str) -> dict[str, Any]:
        import win32gui
        from pywinauto import Desktop

        x, y = win32gui.GetCursorPos()
        semantic = Desktop(backend="uia").from_point(x, y)
        point_window = Desktop(backend="win32").top_from_point(x, y)
        point_title = str(point_window.window_text() or "").strip()
        try:
            point_process_id = int(point_window.process_id())
        except Exception:
            point_process_id = int(getattr(semantic.element_info, "process_id", 0) or 0)

        # Search/autocomplete controls in Enterprise WeChat (and some other
        # desktop clients) are rendered in a separate top-level popup such as
        # ``SearchResultWindow2``.  It has no app title but shares the exact
        # process with the visible main window.  Store coordinates relative to
        # the main window while retaining the popup identity for execution.
        window = point_window
        if application and application not in point_title:
            candidates = []
            for candidate in Desktop(backend="win32").windows(visible_only=False):
                try:
                    if (
                        application in str(candidate.window_text() or "")
                        and int(candidate.process_id()) == point_process_id
                        and not candidate.is_minimized()
                    ):
                        candidates.append(candidate)
                except Exception:
                    continue
            if len(candidates) != 1:
                raise RuntimeError(f"鼠标所在窗口不是 {application}：{point_title or '<无标题>'}")
            window = candidates[0]
        title = str(window.window_text() or "").strip()
        rect = window.rectangle()
        if rect.width() <= 0 or rect.height() <= 0:
            raise RuntimeError("目标窗口尺寸无效")
        info = semantic.element_info
        element_rect = semantic.rectangle()
        ancestry = []
        current = semantic
        for _ in range(6):
            try:
                current_info = current.element_info
                ancestry.append(
                    {
                        "name": str(getattr(current_info, "name", "") or ""),
                        "control_type": str(getattr(current_info, "control_type", "") or ""),
                        "automation_id": str(getattr(current_info, "automation_id", "") or ""),
                        "class_name": str(getattr(current_info, "class_name", "") or ""),
                    }
                )
                current = current.parent()
            except Exception:
                break
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = self.evidence_dir / f"desktop_capture_{stamp}.png"
        template_path = self.evidence_dir / f"desktop_template_{stamp}.png"
        image = window.capture_as_image().convert("RGB")
        image.save(screenshot_path)
        relative_x, relative_y = x - rect.left, y - rect.top
        crop_box = (
            max(0, relative_x - 70), max(0, relative_y - 35),
            min(image.width, relative_x + 70), min(image.height, relative_y + 35),
        )
        image.crop(crop_box).save(template_path)
        try:
            semantic.draw_outline(colour="green", thickness=3)
        except Exception:
            pass
        return {
            "application": application,
            "window": {
                "title": title,
                "class_name": str(window.class_name() or ""),
                "process_id": int(window.process_id()),
                "reference_size": {"width": rect.width(), "height": rect.height()},
            },
            "point_window": {
                "title": point_title,
                "class_name": str(point_window.class_name() or ""),
                "process_id": point_process_id,
            },
            "element": {
                "name": str(getattr(info, "name", "") or ""),
                "control_type": str(getattr(info, "control_type", "") or ""),
                "automation_id": str(getattr(info, "automation_id", "") or ""),
                "class_name": str(getattr(info, "class_name", "") or ""),
                "rectangle": {"left": element_rect.left, "top": element_rect.top, "right": element_rect.right, "bottom": element_rect.bottom},
            },
            "relative_point": {
                "x": relative_x,
                "y": relative_y,
                "x_ratio": relative_x / rect.width(),
                "y_ratio": relative_y / rect.height(),
            },
            "ancestry": ancestry,
            "screenshot_path": str(screenshot_path),
            "template_path": str(template_path),
            "captured_at": datetime.now().isoformat(timespec="seconds"),
        }

    @staticmethod
    def component_from_capture(capture: dict[str, Any], name: str, component_type: str) -> dict[str, Any]:
        element = capture.get("element") or {}
        point = capture.get("relative_point") or {}
        locator = {
            "window": capture.get("window") or {},
            "point_window": capture.get("point_window") or {},
            "control_type": element.get("control_type", ""),
            "name": element.get("name", ""),
            "automation_id": element.get("automation_id", ""),
            "class_name": element.get("class_name", ""),
            "ancestry": capture.get("ancestry") or [],
            "relative_point": point,
            "template_path": capture.get("template_path", ""),
            "resolution_fallback": True,
        }
        return validate_component(
            {
                "component_type": component_type,
                "name": name,
                "application": capture.get("application", "桌面应用"),
                "locator": locator,
                "before_assertions": ["窗口标题一致", "窗口尺寸在容差内", "目标控件可见", "目标点未被弹窗遮挡"],
                "on_failure": "MANUAL_REVIEW",
            }
        )
