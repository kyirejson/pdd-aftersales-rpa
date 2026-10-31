from __future__ import annotations

import re
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .windows_ocr import WindowsOcrError, recognize_image


class DesktopExecutionError(RuntimeError):
    pass


class DesktopComponentExecutor:
    # WDT is a hybrid desktop app.  Newer builds expose some controls through
    # UIA/Win32, while the result grid may still be custom painted.  These
    # roles let old captured components use semantic lookup first and retain
    # their guarded proportional point as a fallback.
    _WDT_ROLES = {
        "CMP-WDT_ORDER_MANAGEMENT": "order_management",
        "CMP-WDT_WAYBILL_INPUT": "tracking_input",
        "CMP-WDT_SEARCH": "search_button",
        "CMP-WDT_ORDER_ID": "wdt_order_id",
        "CMP-WDT_PLATFORM": "wdt_platform",
        "CMP-WDT_ORIGINAL_ORDER": "wdt_original_order",
        "CMP-WDT_ORDER_STATUS": "wdt_order_status",
        "CMP-WDT_WAYBILL": "wdt_waybill",
        "CMP-WDT-WAREHOUSE": "wdt_warehouse",
        "CMP-WDT-LOGISTICS-COMPANY": "wdt_logistics_company",
        "CMP-WDT-GOODS-LIST-TAB": "wdt_goods_list_tab",
        "CMP-WDT-PRODUCT-NAMES": "wdt_product_names",
    }

    _WDT_FIELD_HEADERS = {
        "wdt_order_id": ("订单编号", "订单号"),
        "wdt_platform": ("平台类型", "平台"),
        "wdt_original_order": ("原始单号", "原始订单号"),
        "wdt_order_status": ("订单状态", "状态"),
        "wdt_waybill": ("物流单号", "运单号"),
        "wdt_logistics_company": ("物流公司", "快递公司"),
    }

    _SEARCH_NAMES = ("搜索", "查询", "检索", "执行查询", "搜索订单")
    _SEARCH_EXCLUDED_NAMES = {"清除", "清空", "重置", "取消", "删除"}
    _LOGISTICS_COMPANY_MARKERS = ("圆通", "中通", "顺丰", "韵达", "申通", "极兔", "邮政")

    def __init__(self, evidence_dir: Path):
        try:
            import win32clipboard
            from pywinauto import Desktop, keyboard, mouse
        except ImportError as exc:
            raise DesktopExecutionError("缺少桌面自动化依赖") from exc
        self.evidence_dir = Path(evidence_dir)
        self.win32clipboard = win32clipboard
        self.Desktop = Desktop
        self.keyboard = keyboard
        self.mouse = mouse
        self._wdt_window_cache: Any | None = None

    def _clipboard_get(self) -> str:
        for attempt in range(10):
            try:
                self.win32clipboard.OpenClipboard()
                value = self.win32clipboard.GetClipboardData(self.win32clipboard.CF_UNICODETEXT) if self.win32clipboard.IsClipboardFormatAvailable(self.win32clipboard.CF_UNICODETEXT) else ""
                self.win32clipboard.CloseClipboard()
                return str(value)
            except Exception:
                try: self.win32clipboard.CloseClipboard()
                except Exception: pass
                if attempt == 9: raise
                time.sleep(0.05)
        return ""

    def _clipboard_set(self, value: str) -> None:
        for attempt in range(10):
            try:
                self.win32clipboard.OpenClipboard()
                self.win32clipboard.EmptyClipboard()
                # ``SetClipboardText`` defaults to the active ANSI code page
                # in some pywin32 builds.  That cannot restore Chinese or
                # hidden Unicode characters copied from a browser sheet.
                # Always use the same Unicode clipboard format we read.
                set_data = getattr(self.win32clipboard, "SetClipboardData", None)
                if callable(set_data):
                    set_data(self.win32clipboard.CF_UNICODETEXT, str(value))
                else:
                    self.win32clipboard.SetClipboardText(str(value))
                self.win32clipboard.CloseClipboard()
                return
            except Exception:
                try: self.win32clipboard.CloseClipboard()
                except Exception: pass
                if attempt == 9: raise
                time.sleep(0.05)

    def _input_wdt_tracking_number(
        self,
        *,
        target: Any | None,
        point: tuple[int, int] | None,
        value: str,
        config: dict[str, Any],
        context: dict[str, Any],
    ) -> str:
        """Enter a WDT waybill without touching the Windows clipboard.

        旺店通的自绘筛选框不稳定暴露 UIA Value。旧实现为了回读
        输入内容使用 Ctrl+C 并恢复系统剪贴板，会把搜索前后的剪贴板
        竞争错误误报为“输入物流单号失败”。这里只负责聚焦和直接
        键入；搜索后由唯一结果行的物流单号与拼多多单号一致性完成
        最终校验。
        """
        normalized = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9]{6,64}", normalized):
            raise DesktopExecutionError("旺店通物流单号格式异常，拒绝输入")
        if target is not None:
            try:
                target.click_input()
            except Exception as exc:
                raise DesktopExecutionError("旺店通物流单号输入框无法聚焦") from exc
        elif point is not None:
            self.mouse.click(coords=point)
        else:
            raise DesktopExecutionError("旺店通物流单号输入框缺少捕获位置")
        time.sleep(max(0.0, float(config.get("focus_wait_seconds", 0.08))))
        self.keyboard.send_keys("^a")
        self.keyboard.send_keys(
            normalized,
            pause=max(0.0, float(config.get("key_interval_seconds", 0.012))),
        )
        time.sleep(max(0.0, float(config.get("after_wait_seconds", 0.10))))
        # The value typed into the filter is only a query request.  It must
        # never satisfy the later result assertion by itself; the result-row
        # reader populates ``wdt_waybill`` after WDT returns a unique row.
        context.pop("wdt_waybill", None)
        context["_wdt_tracking_input_value"] = normalized
        context["_wdt_tracking_input_mode"] = "direct_keys_result_verified"
        context["_wdt_tracking_input_verified"] = False
        return normalized

    @contextmanager
    def _preserve_clipboard(self):
        previous = self._clipboard_get()
        try:
            yield
        finally:
            try:
                self._clipboard_set(previous)
            except Exception:
                # Restoring the operator's clipboard is ancillary.  Windows
                # can temporarily reject SetClipboardData while WDT repaints
                # its custom grid.  That race must never turn a successfully
                # read order status into WDT_WORKER_FAILED.
                pass

    @staticmethod
    def _wrapper_info(wrapper: Any) -> dict[str, str]:
        try:
            info = wrapper.element_info
            return {
                "name": str(getattr(info, "name", "") or "").strip(),
                "control_type": str(getattr(info, "control_type", "") or "").strip(),
                "automation_id": str(getattr(info, "automation_id", "") or "").strip(),
                "class_name": str(getattr(info, "class_name", "") or "").strip(),
            }
        except Exception:
            return {"name": "", "control_type": "", "automation_id": "", "class_name": ""}

    @classmethod
    def _wrapper_text(cls, wrapper: Any) -> str:
        info = cls._wrapper_info(wrapper)
        if info["name"]:
            return info["name"]
        try:
            return str(wrapper.window_text() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _wrapper_visible(wrapper: Any) -> bool:
        try:
            return bool(wrapper.is_visible())
        except Exception:
            return True

    @staticmethod
    def _wrapper_enabled(wrapper: Any) -> bool:
        try:
            return bool(wrapper.is_enabled())
        except Exception:
            return True

    @staticmethod
    def _wrapper_area(wrapper: Any) -> int:
        try:
            rect = wrapper.rectangle()
            return max(0, int(rect.width())) * max(0, int(rect.height()))
        except Exception:
            return 0

    @staticmethod
    def _dedupe_windows(windows: list[Any]) -> list[Any]:
        """Remove duplicate UIA/Win32 wrappers for the same native HWND."""
        unique: list[Any] = []
        seen: set[int] = set()
        for window in windows:
            try:
                handle = int(window.handle)
            except Exception:
                handle = id(window)
            if handle in seen:
                continue
            seen.add(handle)
            unique.append(window)
        return unique

    @classmethod
    def _choose_wdt_window(cls, candidates: list[Any], title: str) -> Any:
        """Choose one deterministic live WDT window when stale duplicates exist.

        WDT can leave a minimized/off-screen owner window while its visible
        workbench is active.  Requiring title uniqueness made every order
        fail even though only one candidate was usable.  We deduplicate HWNDs,
        discard minimized/off-screen candidates when a normal one exists, and
        prefer the foreground/large window.  If two equally usable windows
        remain, fail closed instead of guessing.
        """
        candidates = cls._dedupe_windows(candidates)
        if not candidates:
            raise DesktopExecutionError(f"旺店通窗口定位失败：{title}，数量=0")

        usable: list[Any] = []
        for item in candidates:
            try:
                rect = item.rectangle()
                area = max(0, int(rect.width())) * max(0, int(rect.height()))
                minimized = bool(item.is_minimized())
                visible = bool(item.is_visible())
            except Exception:
                area, minimized, visible = 0, False, True
            if visible and not minimized and area > 0:
                usable.append(item)
        if usable:
            candidates = usable
        if len(candidates) == 1:
            return candidates[0]

        foreground = 0
        try:
            import ctypes

            foreground = int(ctypes.windll.user32.GetForegroundWindow())
        except Exception:
            pass
        foreground_matches = [item for item in candidates if int(getattr(item, "handle", 0) or 0) == foreground]
        if len(foreground_matches) == 1:
            return foreground_matches[0]

        scored: list[tuple[int, Any]] = []
        for item in candidates:
            try:
                rect = item.rectangle()
                area = max(0, int(rect.width())) * max(0, int(rect.height()))
            except Exception:
                area = 0
            scored.append((area, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1]
        raise DesktopExecutionError(f"旺店通窗口定位不唯一：{title}，数量={len(candidates)}")

    @staticmethod
    def _force_foreground(handle: int) -> bool:
        """Best-effort foreground activation for a child WDT worker.

        Windows may reject ``SetForegroundWindow`` from an isolated worker
        because the foreground-lock timeout belongs to another process.  A
        short thread-input attach bridges that restriction without relying on
        a screen coordinate or changing the WDT business state.
        """
        try:
            import ctypes

            user32 = ctypes.windll.user32
            target = int(handle)
            if not user32.IsWindow(target):
                return False
            user32.ShowWindow(target, 9)  # SW_RESTORE
            foreground = int(user32.GetForegroundWindow())
            target_pid = ctypes.c_ulong()
            foreground_pid = ctypes.c_ulong()
            target_thread = int(user32.GetWindowThreadProcessId(target, ctypes.byref(target_pid)))
            foreground_thread = int(
                user32.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid))
            ) if foreground else 0
            current_thread = int(user32.GetCurrentThreadId())
            attached = False
            if foreground_thread and target_thread and foreground_thread != target_thread:
                attached = bool(user32.AttachThreadInput(foreground_thread, target_thread, True))
            try:
                user32.BringWindowToTop(target)
                user32.SetForegroundWindow(target)
                user32.SetFocus(target)
            finally:
                if attached:
                    user32.AttachThreadInput(foreground_thread, target_thread, False)
            return int(user32.GetForegroundWindow()) == target
        except Exception:
            return False

    def _focus_window(self, window: Any) -> None:
        if window.is_minimized():
            window.restore()
        expected_handle = int(getattr(window, "handle", 0) or 0)
        try:
            window.set_focus()
            time.sleep(0.04)
            try:
                import ctypes

                if int(ctypes.windll.user32.GetForegroundWindow()) == expected_handle:
                    return
            except Exception:
                # If foreground verification itself is unavailable, retain
                # pywinauto's successful activation as the best available
                # result.  The later point-window assertion still protects
                # every real click.
                return
            first_error: Exception | None = RuntimeError("窗口未成为前台窗口")
        except Exception as exc:
            first_error = exc
        if self._force_foreground(expected_handle):
            return
        # click_input targets the native HWND and can grant foreground
        # permission even when SetForegroundWindow is blocked. The point is
        # the title bar corner, never a business control.
        try:
            window.click_input(coords=(5, 5))
            time.sleep(0.04)
            try:
                import ctypes

                if int(ctypes.windll.user32.GetForegroundWindow()) == expected_handle:
                    return
            except Exception:
                return
            raise RuntimeError("标题栏点击后窗口仍未成为前台窗口")
        except Exception as second_error:
            raise DesktopExecutionError(
                f"桌面窗口无法激活：{first_error or second_error}"
            ) from second_error

    @staticmethod
    def _visual_search_point(window: Any) -> tuple[int, int] | None:
        """Find WDT's blue query button when its CEF surface has no UIA nodes.

        This is a bounded visual fallback, not a captured screen coordinate:
        it searches the filter strip for the saturated-blue action rectangle,
        while explicitly excluding the pale-blue 清除 button.  The point is
        recalculated from the current window image on every query.
        """
        try:
            image = window.capture_as_image().convert("RGB")
            width, height = image.size
            pixels = image.load()
            x0, x1 = int(width * 0.55), int(width * 0.99)
            y0, y1 = int(height * 0.11), int(height * 0.27)
            step = 2
            points: set[tuple[int, int]] = set()
            for y in range(y0, min(y1, height), step):
                for x in range(x0, min(x1, width), step):
                    red, green, blue = pixels[x, y]
                    if red < 125 and 55 < green < 185 and blue > 150 and blue - red > 55:
                        points.add((x // step, y // step))
            components: list[tuple[int, int, int, int, int]] = []
            while points:
                seed = points.pop()
                stack = [seed]
                min_x = max_x = seed[0]
                min_y = max_y = seed[1]
                count = 0
                while stack:
                    px, py = stack.pop()
                    count += 1
                    min_x, max_x = min(min_x, px), max(max_x, px)
                    min_y, max_y = min(min_y, py), max(max_y, py)
                    for neighbor in ((px - 1, py), (px + 1, py), (px, py - 1), (px, py + 1)):
                        if neighbor in points:
                            points.remove(neighbor)
                            stack.append(neighbor)
                box_w = (max_x - min_x + 1) * step
                box_h = (max_y - min_y + 1) * step
                if 45 <= box_w <= 240 and 18 <= box_h <= 70 and count >= 80:
                    components.append((count, min_x * step, min_y * step, box_w, box_h))
            if not components:
                return None
            # The query button is the largest saturated-blue rectangle in the
            # right side of the filter row; dropdown arrows are part of it.
            _, left, top, box_w, box_h = max(components, key=lambda item: item[0])
            rect = window.rectangle()
            return rect.left + left + box_w // 2, rect.top + top + box_h // 2
        except Exception:
            return None

    @staticmethod
    def _visual_wdt_goods_tab_point(window: Any) -> tuple[int, int] | None:
        """Locate WDT's lower blue detail-tab strip from the current image.

        Its vertical position changes with the height of the upper result
        pane, so a ratio captured from one order can land on the summary bar.
        The strip itself is a long saturated-blue band and is recalculated on
        every order.
        """
        try:
            image = window.capture_as_image().convert("RGB")
            width, height = image.size
            pixels = image.load()
            x_limit = max(1, int(width * 0.78))
            candidates: list[tuple[int, int]] = []
            for y in range(int(height * 0.55), int(height * 0.94)):
                blue_count = 0
                for x in range(0, x_limit, 4):
                    red, green, blue = pixels[x, y]
                    if blue > 185 and red < 95 and 65 < green < 175:
                        blue_count += 1
                if blue_count >= max(30, int((x_limit / 4) * 0.20)):
                    candidates.append((blue_count, y))
            if not candidates:
                return None
            peak_y = max(candidates, key=lambda item: item[0])[1]
            band = [y for _, y in candidates if abs(y - peak_y) <= 18]
            center_y = int(round(sum(band) / len(band))) if band else peak_y
            rect = window.rectangle()
            return rect.left + int(round(width * 0.026)), rect.top + center_y
        except Exception:
            return None

    def _scan_wdt_row_values(
        self,
        component: dict[str, Any],
        window: Any,
        stop_when: Callable[[str], bool] | None = None,
    ) -> list[str]:
        """Copy cells from the visible WDT result row without fixed columns.

        The WDT grid is custom-painted and its horizontal columns move with
        the window/scroll position.  A captured x coordinate can therefore
        read the adjacent column (for example the carrier instead of the
        waybill).  Clicking the same row at a small set of image-relative x
        positions and copying the selected cell gives us the current cell
        values while remaining independent of the original screen geometry.
        """
        locator = component.get("locator") or {}
        point = locator.get("relative_point") or {}
        config = component.get("config") or {}
        rect = window.rectangle()
        y_ratio = float(point.get("y_ratio", config.get("row_y_ratio", 0.298)))
        y = rect.top + int(round(y_ratio * rect.height()))
        base_x = rect.left + int(round(float(point.get("x_ratio", 0.5)) * rect.width()))
        # WDT's painted cells expose their copy hotspot slightly to the right
        # of the captured text centre (roughly 20–30 px in the current grid).
        # Prefer that hotspot once, then retain the guarded neighbour fallback.
        role = self._semantic_role(component)
        role_offset = {"wdt_order_status": 24, "wdt_waybill": 24}.get(role, 0)
        base_x += int(config.get("preferred_offset_px", role_offset))
        # The captured point is the centre of the column.  The old fallback
        # walked one direction through 25 points before trying the other
        # direction; every point moved the real mouse and copied the cell,
        # which is exactly the visible "卡顿" users see over the WDT grid.
        # Try the captured point first, then the nearest neighbours.  Keep the
        # span configurable for an unusual layout, but use a small bounded
        # default so a bad capture cannot turn into a long mouse sweep.
        span = max(24, int(config.get("scan_span_px", 48)))
        step = max(6, int(config.get("scan_step_px", 8)))
        offsets = [0]
        for delta in range(step, span + 1, step):
            offsets.extend((-delta, delta))
        values: list[str] = []
        seen: set[str] = set()
        for offset in offsets:
            x = max(rect.left + 20, min(rect.right - 20, base_x + offset))
            try:
                before_sequence = self.win32clipboard.GetClipboardSequenceNumber()
                self.mouse.click(coords=(x, y))
                time.sleep(float(config.get("cell_click_wait_seconds", 0.05)))
                self.keyboard.send_keys("^c")
                copy_deadline = time.monotonic() + max(
                    0.08, float(config.get("cell_copy_wait_seconds", 0.20))
                )
                while (
                    self.win32clipboard.GetClipboardSequenceNumber() == before_sequence
                    and time.monotonic() < copy_deadline
                ):
                    time.sleep(0.02)
                if self.win32clipboard.GetClipboardSequenceNumber() == before_sequence:
                    continue
                value = self._clipboard_get().strip()
            except Exception:
                continue
            if not value or value in seen:
                continue
            seen.add(value)
            values.append(value)
            if stop_when is not None and stop_when(value):
                break
        return values

    def _read_wdt_grid_role(self, component: dict[str, Any], context: dict[str, Any], window: Any) -> str:
        """Resolve a WDT row field from copied cell values when UIA is absent."""
        role = self._semantic_role(component)
        expected_tracking = str(context.get("tracking_no") or "").strip()
        allowed = [str(item).strip() for item in (component.get("config") or {}).get("allowed_statuses", [])]
        allowed += ["未审核", "已取消", "已审核", "待客审", "已完成", "已发货", "待处理"]

        def matches(value: str) -> bool:
            normalized = value.replace(" ", "")
            if role == "wdt_waybill":
                return bool(expected_tracking and expected_tracking in normalized)
            if role == "wdt_order_id":
                return bool(re.search(r"\b[A-Za-z]{1,4}\d{8,}\b", value))
            if role == "wdt_platform":
                expected = str((component.get("config") or {}).get("expected_platform", "拼多多")).strip()
                return bool(expected and expected in normalized)
            if role == "wdt_original_order":
                expected = str(context.get("order_no") or "").strip()
                return bool(expected and expected in normalized)
            if role == "wdt_order_status":
                return any(status and status in normalized for status in allowed)
            if role == "wdt_logistics_company":
                return self._looks_like_logistics_company(normalized)
            if role == "wdt_warehouse":
                return self._looks_like_wdt_warehouse(normalized)
            return False

        # Stop as soon as the expected field value is copied; reading every
        # neighboring point made one WDT lookup take 20+ seconds for no added
        # confidence.
        values = self._scan_wdt_row_values(
            component,
            window,
            stop_when=matches,
        )
        if not values:
            return ""
        if role == "wdt_waybill":
            expected = expected_tracking
            if expected:
                for value in values:
                    if expected in value.replace(" ", ""):
                        return expected
            return ""
        if role == "wdt_order_status":
            for value in values:
                normalized = value.replace(" ", "")
                for status in allowed:
                    if status and status in normalized:
                        return status
            return ""
        if role == "wdt_platform":
            expected = str((component.get("config") or {}).get("expected_platform", "拼多多")).strip()
            return expected if any(expected in value.replace(" ", "") for value in values) else ""
        if role == "wdt_original_order":
            expected = str(context.get("order_no") or "").strip()
            if expected and any(expected in value.replace(" ", "") for value in values):
                return expected
            return ""
        if role == "wdt_order_id":
            matches = [match for value in values for match in re.findall(r"\b[A-Za-z]{1,4}\d{8,}\b", value)]
            return matches[0] if len(matches) == 1 else ""
        if role == "wdt_logistics_company":
            for value in values:
                if self._looks_like_logistics_company(value):
                    return value
            return ""
        if role == "wdt_warehouse":
            for value in values:
                if self._looks_like_wdt_warehouse(value):
                    return re.sub(r"\s+", "", value)
            return ""
        return ""

    def _wait_for_wdt_query_result(
        self,
        component: dict[str, Any],
        context: dict[str, Any],
    ) -> bool:
        """Wait until WDT's result row contains this query's exact waybill.

        A non-empty grid is not sufficient proof: while WDT is repainting, the
        previous query's row can remain visible for several seconds. Poll the
        actual waybill cell and accept only the tracking number entered for
        this execution.
        """

        config = dict(component.get("config") or {})
        expected = str(context.get("tracking_no") or "").strip()
        if not expected:
            raise DesktopExecutionError("等待旺店通结果时物流单号为空")
        try:
            minimum_wait = max(
                0.0, min(float(config.get("minimum_wait_seconds", 0.5)), 3.0)
            )
            max_wait = max(
                1.0, min(float(config.get("max_wait_seconds", 12.0)), 30.0)
            )
            interval = max(
                0.1, min(float(config.get("poll_interval_seconds", 0.35)), 2.0)
            )
        except (TypeError, ValueError) as exc:
            raise DesktopExecutionError("旺店通结果同步等待参数无效") from exc

        window, _, _ = self._resolve_window(component)
        point = dict(
            config.get("tracking_cell_relative_point")
            or {"x_ratio": 1850 / 1920, "y_ratio": 307 / 1040}
        )
        probe = {
            "locator": {"relative_point": point},
            "config": {
                "scan_span_px": int(config.get("tracking_scan_span_px", 24)),
                "scan_step_px": int(config.get("tracking_scan_step_px", 8)),
                "cell_click_wait_seconds": float(
                    config.get("tracking_cell_click_wait_seconds", 0.05)
                ),
                "cell_copy_wait_seconds": float(
                    config.get("tracking_cell_copy_wait_seconds", 0.20)
                ),
            },
        }
        started = time.monotonic()
        deadline = started + max_wait
        last_values: list[str] = []
        while True:
            now = time.monotonic()
            try:
                visual_count = int(self._visual_grid_count(window, config) or 0)
            except Exception:
                # A custom-painted redraw may briefly invalidate the bitmap.
                # The exact cell read below is the authoritative condition.
                visual_count = 0
            if visual_count > 0:
                context["_wdt_result_visual_count_seen"] = visual_count
            if now - started >= minimum_wait:
                try:
                    with self._preserve_clipboard():
                        last_values = self._scan_wdt_row_values(
                            probe,
                            window,
                            stop_when=lambda value: expected in value.replace(" ", ""),
                        )
                except Exception:
                    # Clipboard/cell access can race the repaint. Retry without
                    # clicking Search again, which avoids duplicate queries.
                    last_values = []
                if any(expected in value.replace(" ", "") for value in last_values):
                    context["_wdt_result_tracking_synced"] = expected
                    context["_wdt_result_sync_wait_seconds"] = round(
                        time.monotonic() - started, 3
                    )
                    return True
            if time.monotonic() >= deadline:
                observed = " | ".join(last_values[-3:]) or "<空或旧结果>"
                raise DesktopExecutionError(
                    f"旺店通查询后等待 {max_wait:g} 秒仍未同步到物流单号 "
                    f"{expected}；最后回读：{observed}"
                )
            time.sleep(interval)

    @classmethod
    def _looks_like_logistics_company(cls, value: str) -> bool:
        """Accept a copied logistics-company cell, never a warehouse cell.

        The WDT result grid is custom drawn.  A stale capture previously
        landed one column left on ``仓库`` and returned values such as
        ``示例自营仓``.  A carrier routing value must name a supported carrier;
        anything else is deliberately rejected before routing.
        """
        normalized = re.sub(r"\s+", "", str(value or ""))
        return bool(normalized and any(marker in normalized for marker in cls._LOGISTICS_COMPANY_MARKERS))

    @staticmethod
    def _looks_like_wdt_warehouse(value: str) -> bool:
        """Accept only a plausible warehouse cell, never the adjacent carrier cell."""
        normalized = re.sub(r"\s+", "", str(value or ""))
        return bool(normalized and "仓" in normalized and not any(
            marker in normalized
            for marker in DesktopComponentExecutor._LOGISTICS_COMPANY_MARKERS
        ))

    def _resolve_window(self, component: dict[str, Any], *, activate: bool = True):
        locator = component.get("locator") or {}
        window_locator = locator.get("window") or component.get("config", {}).get("window") or {}
        title = str(window_locator.get("title") or "旺店通ERP旗舰版")
        title_contains = str(window_locator.get("title_contains") or "").strip()
        window = self._wdt_window_cache
        if window is None or not self._window_responsive(int(getattr(window, "handle", 0) or 0)):
            if title_contains:
                candidates = [
                    item
                    for item in self.Desktop(backend="win32").windows(visible_only=True)
                    if title_contains in str(item.window_text() or "")
                ]
            else:
                candidates = self.Desktop(backend="win32").windows(
                    title_re=f"^{re.escape(title)}$", visible_only=True
                )
            expected_class = str(window_locator.get("class_name") or "").strip()
            if expected_class:
                candidates = [item for item in candidates if str(item.class_name() or "") == expected_class]
            window = self._choose_wdt_window(candidates, title_contains or title)
            self._wdt_window_cache = window
        self._match_reference_geometry(window, window_locator)
        try:
            import ctypes
            foreground = int(ctypes.windll.user32.GetForegroundWindow())
        except Exception:
            foreground = 0
        if activate and foreground != int(window.handle):
            self._focus_window(window)
            time.sleep(0.12)
        return window, locator, window_locator

    @staticmethod
    def _match_reference_geometry(window: Any, window_locator: dict[str, Any]) -> bool:
        """Restore a captured WDT work-area size before coordinate fallbacks.

        WDT persists its last non-maximized size.  Restore each component to
        its own captured reference size; captures are not all 1920x1040.  The
        later point/window and template checks remain authoritative before any
        click or clipboard read.
        """
        reference = window_locator.get("reference_size") or {}
        try:
            width = int(reference.get("width", 0) or 0)
            height = int(reference.get("height", 0) or 0)
            rect = window.rectangle()
            current_width = int(rect.width())
            current_height = int(rect.height())
        except Exception:
            return False
        if width < 320 or height < 240:
            return False
        width_delta = abs(current_width - width) / max(current_width, 1)
        height_delta = abs(current_height - height) / max(current_height, 1)
        if width_delta <= 0.05 and height_delta <= 0.05:
            return True
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            handle = int(getattr(window, "handle", 0) or 0)
            if not handle or not user32.IsWindow(handle):
                return False
            work = wintypes.RECT()
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0):
                work_width = int(work.right - work.left)
                work_height = int(work.bottom - work.top)
                left, top = int(work.left), int(work.top)
            else:
                work_width = int(user32.GetSystemMetrics(0))
                work_height = int(user32.GetSystemMetrics(1))
                left = top = 0
            # The old capture used the full desktop height while Windows now
            # reports a taskbar-adjusted work area. Use whichever live target
            # is physically available; relative points are verified later.
            target_width = min(width, work_width)
            target_height = min(height, work_height)
            for _attempt in range(3):
                user32.ShowWindow(handle, 9)  # SW_RESTORE
                if not user32.SetWindowPos(
                    handle, 0, left, top, target_width, target_height, 0x0040
                ):
                    time.sleep(0.12)
                    continue
                stable: list[tuple[int, int]] = []
                for _sample in range(4):
                    time.sleep(0.12)
                    live = window.rectangle()
                    size = (int(live.width()), int(live.height()))
                    stable.append(size)
                    if len(stable) >= 2 and stable[-1] == stable[-2]:
                        break
                final_width, final_height = stable[-1]
                if (
                    abs(final_width - target_width) / max(final_width, 1) <= 0.03
                    and abs(final_height - target_height) / max(final_height, 1) <= 0.03
                ):
                    return True
            return False
        except Exception:
            # The existing resolution guard will fail closed if resizing was
            # not available or the WDT shell rejected it.
            return False

    def _semantic_role(self, component: dict[str, Any]) -> str:
        config = component.get("config") or {}
        role = str(config.get("semantic_role") or "").strip()
        return role or self._WDT_ROLES.get(str(component.get("component_id") or ""), "")

    def _uia_controls(self, window: Any) -> list[Any]:
        controls: list[Any] = []
        seen: set[int] = set()
        for backend in ("uia", "win32"):
            try:
                root = self.Desktop(backend=backend).window(handle=window.handle)
                for item in root.descendants():
                    try:
                        key = int(getattr(item, "handle", 0) or 0) or id(item)
                    except Exception:
                        key = id(item)
                    if key in seen or not self._wrapper_visible(item):
                        continue
                    seen.add(key)
                    controls.append(item)
            except Exception:
                # Custom WDT surfaces can make one backend throw while the
                # other remains usable.
                continue
        return controls

    def _semantic_spec(self, component: dict[str, Any]) -> dict[str, Any]:
        locator = component.get("locator") or {}
        config = component.get("config") or {}
        role = self._semantic_role(component)
        explicit = dict(locator.get("semantic") or config.get("semantic") or {})
        names = list(explicit.get("names") or [])
        control_types = list(explicit.get("control_types") or [])
        if role == "order_management":
            names = names or ["订单管理"]
            control_types = control_types or ["Button", "TabItem", "MenuItem", "Text", "Pane"]
        elif role == "search_button":
            names = names or list(self._SEARCH_NAMES)
            control_types = control_types or ["Button", "SplitButton", "Hyperlink", "Text", "Pane", "Custom"]
        elif role == "tracking_input":
            # Use the nearby 物流单号 label when multiple Edit controls exist.
            names = names or list(explicit.get("input_names") or [])
            control_types = control_types or ["Edit", "ComboBox"]
        elif role == "wdt_goods_list_tab":
            names = names or ["货品列表"]
            control_types = control_types or ["Button", "TabItem", "Text", "Pane", "Custom"]
        # Do not treat a column header as the value of a result row.  Field
        # roles become semantic only when a future capture explicitly stores
        # a control locator; otherwise the existing clipboard read remains the
        # safe fallback.
        if not names and not control_types:
            return {}
        return {
            "role": role,
            "names": tuple(str(item).strip() for item in names if str(item).strip()),
            "control_types": tuple(str(item).strip() for item in control_types if str(item).strip()),
            "automation_id": str(explicit.get("automation_id") or locator.get("automation_id") or "").strip(),
            "class_name": str(explicit.get("class_name") or "").strip(),
        }

    @staticmethod
    def _normalize_control_text(value: Any) -> str:
        return re.sub(r"\s+", "", str(value or "").strip())

    def _search_row_candidates(self, controls: list[Any], candidates: list[Any]) -> list[Any]:
        """Prefer a uniquely labelled query control beside the waybill input.

        WDT often renders a clear button next to the query button. The old
        captured component had only a pane and a relative point, so a small
        layout change could land on 清除. Bind the search control to the
        logistics-number field's row before considering any fallback.
        """
        candidates = [
            item for item in candidates
            if self._normalize_control_text(self._wrapper_text(item))
            not in self._SEARCH_EXCLUDED_NAMES
        ]
        if len(candidates) <= 1:
            return candidates
        labels = [
            item for item in controls
            if self._normalize_control_text(self._wrapper_text(item)) in {"物流单号", "运单号"}
        ]
        edits = [
            item for item in controls
            if self._wrapper_info(item)["control_type"] in {"Edit", "ComboBox"}
        ]
        if len(labels) != 1:
            return []
        try:
            label_rect = labels[0].rectangle()
            row_edits = [
                item for item in edits
                if item.rectangle().left >= label_rect.right - 10
                and abs(item.rectangle().top - label_rect.top) < max(40, label_rect.height() * 3)
            ]
            if len(row_edits) != 1:
                return []
            edit_rect = row_edits[0].rectangle()
            row = [
                item for item in candidates
                if item.rectangle().left >= edit_rect.right - 10
                and abs(item.rectangle().top - edit_rect.top) < max(40, edit_rect.height() * 3)
            ]
            return row if len(row) == 1 else []
        except Exception:
            return []

    def _find_semantic_target(self, component: dict[str, Any], window: Any) -> Any | None:
        spec = self._semantic_spec(component)
        if not spec:
            return None
        controls = self._uia_controls(window)
        if not controls:
            return None
        window_area = max(1, self._wrapper_area(window))
        names = {self._normalize_control_text(name) for name in spec["names"]}
        types = set(spec["control_types"])
        candidates: list[Any] = []
        for item in controls:
            info = self._wrapper_info(item)
            text = self._wrapper_text(item)
            if not text and not info["automation_id"]:
                continue
            if self._wrapper_area(item) >= window_area * 0.85:
                continue
            if spec["automation_id"] and info["automation_id"] != spec["automation_id"]:
                continue
            if spec["class_name"] and info["class_name"] != spec["class_name"]:
                continue
            if types and info["control_type"] not in types:
                continue
            if names and self._normalize_control_text(text) not in names:
                continue
            candidates.append(item)

        if spec["role"] == "search_button":
            candidates = self._search_row_candidates(controls, candidates)

        if spec["role"] == "tracking_input" and not spec["names"]:
            # Ignore the global list of Edit controls; use the nearby label
            # to bind the logistics-number field.
            candidates = []
            labels = [item for item in controls if self._wrapper_text(item) in {"物流单号", "运单号"}]
            edits = [
                item for item in controls
                if self._wrapper_info(item)["control_type"] in {"Edit", "ComboBox"}
            ]
            if len(labels) == 1:
                try:
                    label_rect = labels[0].rectangle()
                    candidates = [
                        item for item in edits
                        if item.rectangle().left >= label_rect.right - 10
                        and abs(item.rectangle().top - label_rect.top) < max(30, label_rect.height() * 2)
                    ]
                except Exception:
                    candidates = []
            elif len(edits) == 1:
                candidates = edits

        # Never pick the first match: duplicate tabs/buttons are common in
        # WDT.  Ambiguity falls through to the old guarded point strategy.
        if len(candidates) != 1 or not self._wrapper_enabled(candidates[0]):
            return None
        return candidates[0]

    def _semantic_grid_row(self, window: Any, context: dict[str, Any]) -> dict[str, Any] | None:
        """Find a row containing the requested order/tracking value when UIA exposes a grid.

        The result is deliberately conservative.  A custom-painted grid may
        expose both a cell and its parent row; containment removes the child
        duplicate.  Multiple unrelated matches are returned as ambiguous so
        the workflow fails closed instead of choosing a row by position.
        """
        target_values = [
            str(context.get("tracking_no") or "").strip(),
            str(context.get("order_no") or "").strip(),
        ]
        target_values = [value for value in target_values if value]
        if not target_values:
            return None
        matches: list[tuple[Any, str, Any]] = []
        for item in self._uia_controls(window):
            info = self._wrapper_info(item)
            if info["control_type"] not in {"DataItem", "ListItem", "Row", "Custom", "Group"}:
                continue
            text = self._wrapper_text(item)
            if not text or not any(value in text for value in target_values):
                continue
            try:
                rect = item.rectangle()
            except Exception:
                continue
            matches.append((item, text, rect))
        if not matches:
            return None

        def area(rect: Any) -> int:
            try:
                return max(0, int(rect.width())) * max(0, int(rect.height()))
            except Exception:
                return 0

        def contains(outer: Any, inner: Any) -> bool:
            try:
                return (
                    outer.left <= inner.left
                    and outer.top <= inner.top
                    and outer.right >= inner.right
                    and outer.bottom >= inner.bottom
                )
            except Exception:
                return False

        top_level = []
        for candidate in matches:
            if any(
                other is not candidate
                and area(other[2]) > area(candidate[2])
                and contains(other[2], candidate[2])
                for other in matches
            ):
                continue
            top_level.append(candidate)
        if len(top_level) != 1:
            return {"count": len(top_level), "ambiguous": True}
        item, text, rect = top_level[0]
        self._active_semantic_row_text = text
        try:
            rect_data = {
                "left": int(rect.left), "top": int(rect.top),
                "right": int(rect.right), "bottom": int(rect.bottom),
            }
        except Exception:
            rect_data = {}
        return {"count": 1, "ambiguous": False, "text": text, "rect": rect_data}

    def _semantic_row_value(self, component: dict[str, Any], context: dict[str, Any]) -> str:
        """Extract a requested field from a UIA row's combined text.

        This is used only when the grid exposed a unique semantic row.  It is
        intentionally conservative and returns an empty string when the row
        does not contain an unambiguous known value, allowing the guarded
        clipboard/coordinate reader to handle custom-painted grids.
        """
        text = str(getattr(self, "_active_semantic_row_text", "") or "")
        if not text:
            return ""
        role = self._semantic_role(component)
        if role == "wdt_platform":
            for value in ("拼多多", "淘宝", "天猫", "京东"):
                if value in text:
                    return value
            return ""
        if role == "wdt_original_order":
            expected = str(context.get("order_no") or "").strip()
            if expected and expected in text:
                return expected
            values = re.findall(r"\d{6}-\d{15,}", text)
            return values[0] if len(values) == 1 else ""
        if role == "wdt_waybill":
            expected = str(context.get("tracking_no") or "").strip()
            return expected if expected and expected in text else ""
        if role == "wdt_order_status":
            statuses = ("未审核", "已取消", "已审核", "待客审", "已完成", "已发货", "待处理")
            values = [value for value in statuses if value in text]
            return values[0] if len(values) == 1 else ""
        if role == "wdt_logistics_company":
            for value in ("圆通", "中通", "顺丰", "韵达", "申通", "极兔", "邮政"):
                if value in text:
                    return value
            return ""
        if role == "wdt_warehouse":
            # A semantic grid row may expose the whole row as one string.
            # Preserve any single warehouse value so the routing gate can
            # explicitly hold unconfigured warehouses for review instead of
            # treating them as a transient read failure.
            values = list(
                dict.fromkeys(
                    re.findall(r"(?<!仓)[\u4e00-\u9fffA-Za-z0-9_-]{1,24}仓(?!库)", text)
                )
            )
            return values[0] if len(values) == 1 else ""
        if role == "wdt_order_id":
            values = re.findall(r"\b[A-Za-z]{1,4}\d{8,}\b", text)
            return values[0] if len(values) == 1 else ""
        return ""

    def _read_wdt_product_names(self, component: dict[str, Any], window: Any) -> str:
        """Read visible product-name cells from WDT's lower goods grid.

        The grid is custom-painted, so UIA usually exposes no cell objects.
        We first accept visible UIA text when available, then copy the same
        captured column row-by-row.  Scanning stops after consecutive blanks;
        no horizontal sweep or fixed absolute screen coordinate is used.
        """
        config = component.get("config") or {}
        keywords = tuple(
            str(item).strip()
            for item in config.get("product_keywords", [])
            if str(item).strip()
        )
        visible: list[str] = []
        for item in self._uia_controls(window):
            text = str(self._wrapper_text(item) or "").strip()
            if text and any(keyword in text for keyword in keywords):
                visible.append(text)
        if visible:
            return "\n".join(dict.fromkeys(visible))

        locator = component.get("locator") or {}
        point = locator.get("relative_point") or {}
        rect = window.rectangle()
        x_ratio = float(point.get("x_ratio", config.get("column_x_ratio", 0.18)))
        tab_point = self._visual_wdt_goods_tab_point(window)
        if tab_point:
            first_y = tab_point[1] + int(round(float(config.get("first_row_offset_ratio", 0.055)) * rect.height()))
        else:
            y_ratio = float(point.get("y_ratio", config.get("first_row_y_ratio", 0.865)))
            first_y = rect.top + int(round(y_ratio * rect.height()))
        row_step_ratio = float(config.get("row_step_ratio", 26 / 1040))
        max_rows = max(1, min(int(config.get("max_visible_rows", 12)), 30))
        blank_limit = max(1, min(int(config.get("blank_row_limit", 2)), 5))
        x = rect.left + int(round(x_ratio * rect.width()))
        names: list[str] = []
        blank_rows = 0
        for row_index in range(max_rows):
            y = first_y + int(round(row_index * row_step_ratio * rect.height()))
            if y >= rect.bottom - 10:
                break
            sentinel = f"__WDT_GOODS_{time.time_ns()}__"
            try:
                self._clipboard_set(sentinel)
                self.mouse.click(coords=(x, y))
                time.sleep(float(config.get("cell_click_wait_seconds", 0.04)))
                self.keyboard.send_keys("^c")
                time.sleep(float(config.get("cell_copy_wait_seconds", 0.06)))
                value = self._clipboard_get().strip()
            except Exception:
                value = ""
            if not value or value == sentinel:
                blank_rows += 1
                if names and blank_rows >= blank_limit:
                    break
                continue
            blank_rows = 0
            if value not in names:
                names.append(value)
        return "\n".join(names)

    def _safe_top_window_from_point(self, x: int, y: int) -> Any:
        """Read the native window at a captured point through a redraw safely.

        Qt/WeCom popups can destroy and recreate a child window while a search
        result is closing.  ``pywinauto.top_from_point`` then occasionally
        raises ``ChildWindowFromPoint: handle invalid`` even though the main
        client is still healthy.  Retry the same read briefly, then use the
        Win32 root-window lookup.  The caller still verifies the exact
        captured main/popup identity before any coordinate action occurs.
        """

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                candidate = self.Desktop(backend="win32").top_from_point(x, y)
                # Force an HWND round trip now, while we can still retry a
                # transient child that disappeared during UI redraw.
                _ = int(candidate.handle)
                return candidate
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.08)
        try:
            import win32con
            import win32gui

            child = int(win32gui.WindowFromPoint((int(x), int(y))))
            root = int(win32gui.GetAncestor(child, win32con.GA_ROOT)) if child else 0
            if root:
                candidate = self.Desktop(backend="win32").window(handle=root)
                _ = int(candidate.handle)
                return candidate
        except Exception as exc:
            last_error = exc
        raise DesktopExecutionError(
            f"无法读取桌面组件目标窗口：{last_error or 'unknown native window error'}"
        )

    def _resolve(self, component: dict[str, Any]):
        window, locator, window_locator = self._resolve_window(component)
        self._last_resolved_target = self._find_semantic_target(component, window)
        if self._last_resolved_target is not None:
            return window, None
        if self._semantic_role(component) == "search_button":
            visual_point = self._visual_search_point(window)
            if visual_point:
                return window, visual_point
            # A captured coordinate fallback is unsafe here: the query and
            # clear controls are adjacent and WDT moves them as filters change.
            raise DesktopExecutionError("旺店通搜索控件无法语义或视觉唯一定位，已拒绝坐标兜底")
        if component.get("component_type") == "ReadDesktopGridRow":
            # The grid reader uses a semantic row when available and its own
            # image-relative geometry otherwise; it does not click a point.
            return window, None
        # Coordinate fallback uses live relative geometry. A moderate window
        # resize is safe because the subsequent point-window and visual
        # template checks still prove the target; only a tiny or heavily
        # distorted window is rejected.
        rect = window.rectangle()
        reference = window_locator.get("reference_size") or {}
        if reference:
            reference_width = int(reference.get("width", rect.width()))
            reference_height = int(reference.get("height", rect.height()))
            live_width, live_height = int(rect.width()), int(rect.height())
            reference_ratio = reference_width / max(reference_height, 1)
            live_ratio = live_width / max(live_height, 1)
            ratio_delta = abs(live_ratio - reference_ratio) / max(reference_ratio, 0.01)
            width_delta = abs(live_width - reference_width) / max(reference_width, 1)
            height_delta = abs(live_height - reference_height) / max(reference_height, 1)
            matches_capture = width_delta <= 0.05 and height_delta <= 0.05
            # A component captured at 896x648 is valid at that exact size.
            # Only apply a lower bound when the live window has drifted away
            # from its capture; visual and point-window checks still run next.
            min_width = max(480, min(1200, int(reference_width * 0.75)))
            min_height = max(360, min(700, int(reference_height * 0.75)))
            if (
                not matches_capture
                and (
                    live_width < min_width
                    or live_height < min_height
                    or ratio_delta > 0.12
                )
            ):
                raise DesktopExecutionError(
                    "旺店通窗口自动恢复后仍不适合坐标兜底："
                    f"当前={live_width}x{live_height}，"
                    f"参考={reference_width}x{reference_height}，"
                    f"宽高比偏差={ratio_delta:.1%}"
                )
        if not locator.get("resolution_fallback", True):
            raise DesktopExecutionError("桌面组件未允许坐标兜底")
        self._verify_coordinate_template(component, window, locator, window_locator)
        point = locator.get("relative_point") or {}
        if not point:
            return window, None
        x = rect.left + int(round(float(point.get("x_ratio", 0)) * rect.width()))
        y = rect.top + int(round(float(point.get("y_ratio", 0)) * rect.height()))
        top = self._safe_top_window_from_point(x, y)
        if not self._point_window_matches(component, window, top):
            raise DesktopExecutionError("目标点被其他窗口或弹窗遮挡")
        return window, (x, y)

    @staticmethod
    def _point_window_matches(component: dict[str, Any], main_window: Any, top_window: Any) -> bool:
        """Allow the captured same-process popup, but never an arbitrary overlay."""

        try:
            if int(top_window.handle) == int(main_window.handle):
                return True
        except Exception:
            return False
        locator = component.get("locator") or {}
        expected = locator.get("point_window") or {}
        expected_class = str(expected.get("class_name") or "").strip()
        expected_title = str(expected.get("title") or "").strip()
        expected_title_contains = str(expected.get("title_contains") or "").strip()
        try:
            same_process = int(top_window.process_id()) == int(main_window.process_id())
            same_class = not expected_class or str(top_window.class_name() or "") == expected_class
            actual_title = str(top_window.window_text() or "").strip()
            same_title = (
                expected_title_contains in actual_title
                if expected_title_contains
                else (not expected_title or actual_title == expected_title)
            )
        except Exception:
            return False
        return bool(same_process and same_class and same_title)

    @staticmethod
    def _image_similarity(expected: Any, actual: Any) -> float:
        """Return a bounded pixel similarity for two RGB crops."""

        try:
            from PIL import ImageChops, ImageStat

            if expected.size != actual.size:
                actual = actual.resize(expected.size)
            diff = ImageChops.difference(expected.convert("RGB"), actual.convert("RGB"))
            mean_error = sum(ImageStat.Stat(diff).mean) / (3.0 * 255.0)
            return max(0.0, min(1.0, 1.0 - float(mean_error)))
        except Exception as exc:
            raise DesktopExecutionError(f"桌面组件模板校验失败：{exc}") from exc

    def _verify_coordinate_template(
        self,
        component: dict[str, Any],
        window: Any,
        locator: dict[str, Any],
        window_locator: dict[str, Any],
    ) -> None:
        """Require a captured button appearance before a point fallback.

        Desktop chat clients can expose their whole custom-painted surface as
        one UIA window.  When a component opts into this guard, the local
        screen around its captured point must still resemble the saved button
        template; a mismatch stops before any external message is sent.
        """

        config = dict(component.get("config") or {})
        if not config.get("require_template_match"):
            return
        template_path = Path(str(locator.get("template_path") or "").strip())
        if not template_path.is_file():
            raise DesktopExecutionError("桌面发送组件缺少可用的模板校验图")
        point = dict(locator.get("relative_point") or {})
        reference = dict(window_locator.get("reference_size") or {})
        try:
            from PIL import Image

            expected = Image.open(template_path).convert("RGB")
            image = window.capture_as_image().convert("RGB")
            reference_width = max(1.0, float(reference.get("width") or image.width))
            reference_height = max(1.0, float(reference.get("height") or image.height))
            point_x = (
                float(point["x"])
                if point.get("x") is not None
                else float(point.get("x_ratio", 0.0)) * reference_width
            )
            point_y = (
                float(point["y"])
                if point.get("y") is not None
                else float(point.get("y_ratio", 0.0)) * reference_height
            )
            # DesktopCaptureService crops x±70/y±35 and clamps at window
            # edges. Recreate that exact region at the current scale.
            source_left = max(0.0, point_x - 70.0)
            source_top = max(0.0, point_y - 35.0)
            source_right = min(reference_width, point_x + 70.0)
            source_bottom = min(reference_height, point_y + 35.0)
            left = int(round(source_left / reference_width * image.width))
            top = int(round(source_top / reference_height * image.height))
            right = int(round(source_right / reference_width * image.width))
            bottom = int(round(source_bottom / reference_height * image.height))
            if right <= left or bottom <= top:
                raise ValueError("模板区域尺寸无效")
            radius = max(0, min(int(config.get("template_search_radius_px", 0)), 12))
            minimum = max(0.5, min(float(config.get("template_min_similarity", 0.8)), 0.99))
        except (TypeError, ValueError) as exc:
            raise DesktopExecutionError(f"桌面发送组件模板参数无效：{exc}") from exc
        except FileNotFoundError as exc:
            raise DesktopExecutionError("桌面发送组件模板文件不存在") from exc

        crop_width = right - left
        crop_height = bottom - top
        best_similarity = 0.0
        for offset_y in range(-radius, radius + 1):
            for offset_x in range(-radius, radius + 1):
                candidate_left = max(0, min(image.width - crop_width, left + offset_x))
                candidate_top = max(0, min(image.height - crop_height, top + offset_y))
                candidate = image.crop(
                    (
                        candidate_left,
                        candidate_top,
                        candidate_left + crop_width,
                        candidate_top + crop_height,
                    )
                )
                best_similarity = max(best_similarity, self._image_similarity(expected, candidate))
        if best_similarity < minimum:
            raise DesktopExecutionError(
                f"桌面发送组件外观校验未通过：相似度 {best_similarity:.3f} < {minimum:.3f}"
            )

    @staticmethod
    def _window_responsive(handle: int) -> bool:
        """Probe a native window without touching the UIA/COM tree."""
        try:
            import ctypes

            user32 = ctypes.windll.user32
            if not user32.IsWindow(int(handle)):
                return False
            return not bool(user32.IsHungAppWindow(int(handle)))
        except Exception:
            # The probe is a guard only.  If the optional Win32 API is not
            # available, let the normal component assertions decide.
            return True

    def _wait_for_window_responsive(self, window: Any, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, min(float(seconds), 30.0))
        while True:
            if self._window_responsive(int(window.handle)):
                return
            if time.monotonic() >= deadline:
                raise DesktopExecutionError("旺店通点击后窗口无响应")
            time.sleep(0.1)

    def _read_captured_ocr(self, component: dict[str, Any], window: Any) -> str:
        """OCR a small captured desktop region without changing the client UI.

        Enterprise WeChat and WeChat render their chat panes as custom
        surfaces on many builds, so a selected header/message bubble may have
        neither UIA text nor clipboard copy support.  The capture point still
        gives us a constrained, auditable region.  OCR is local Windows OCR;
        there is no cloud upload or screen-coordinate guess involved.
        """

        config = dict(component.get("config") or {})
        locator = dict(component.get("locator") or {})
        dynamic_region = str(config.get("ocr_dynamic_region") or "").strip().lower()
        point = dict(locator.get("relative_point") or {})
        x_ratio = y_ratio = 0.0
        try:
            x_ratio = float(point.get("x_ratio"))
            y_ratio = float(point.get("y_ratio"))
        except (TypeError, ValueError) as exc:
            raise DesktopExecutionError("OCR 文本组件缺少捕获相对位置") from exc
        if not (0 <= x_ratio <= 1 and 0 <= y_ratio <= 1):
            raise DesktopExecutionError("OCR 文本组件的捕获相对位置无效")
        try:
            image = window.capture_as_image().convert("RGB")
        except Exception as exc:
            raise DesktopExecutionError(f"无法截取 OCR 文本组件窗口：{exc}") from exc
        if image.width < 2 or image.height < 2:
            raise DesktopExecutionError("OCR 文本组件窗口截图无效")

        try:
            width = max(120, min(int(config.get("ocr_width_px", 640)), image.width))
            height = max(50, min(int(config.get("ocr_height_px", 180)), image.height))
            # A small, tightly captured outgoing chat bubble benefits from a
            # larger local upscale.  Keep the cap bounded so headers still
            # remain fast while allowing six-times rendering for dense
            # waybill digits.
            scale = max(1, min(int(config.get("ocr_scale", 2)), 6))
            timeout = max(2.0, min(float(config.get("ocr_timeout_seconds", 12)), 25.0))
        except (TypeError, ValueError) as exc:
            raise DesktopExecutionError("OCR 文本组件参数无效") from exc
        if dynamic_region == "wechat_sent_bubble":
            crop = self._wechat_sent_bubble_crop(image, config)
        elif dynamic_region == "wechat_group_header":
            # Only the right-side chat title bar may prove that a WeChat group
            # is already open.  The former centred 720px crop included the
            # left session list, so a pinned target group could be mistaken for
            # the currently opened conversation while the chat pane was blank.
            bounds = (0.34, 0.03, 0.82, 0.17)
            crop = image.crop(tuple(int(size * ratio) for size, ratio in zip(
                (image.width, image.height, image.width, image.height), bounds
            )))
        elif dynamic_region == "qq_chat_recent":
            # QQNT's message list ends above its toolbar/editor boundary at
            # roughly 67% of the inspected 960x640 window.  Excluding the
            # editor is mandatory: unsent compose text must never count as a
            # delivered interception message.
            bounds = (0.30, 0.20, 0.81, 0.67)
            crop = image.crop(tuple(int(size * ratio) for size, ratio in zip(
                (image.width, image.height, image.width, image.height), bounds
            )))
        elif dynamic_region in {"right_chat_recent", "right_chat_recent_wide"}:
            # Enterprise WeChat paints the conversation as a custom surface.
            # Restrict OCR to the newest chat history and stop above the
            # compose divider.  Including the composer here is unsafe: a
            # missed Send click leaves the expected text in the editor and
            # must never be accepted as an outgoing-message echo.
            if dynamic_region == "right_chat_recent_wide":
                bounds = (0.28, 0.20, 0.90, 0.755)
            else:
                bounds = (0.34, 0.30, 0.82, 0.755)
            crop = image.crop(tuple(int(size * ratio) for size, ratio in zip(
                (image.width, image.height, image.width, image.height), bounds
            )))
        else:
            center_x = int(round(x_ratio * image.width))
            center_y = int(round(y_ratio * image.height))
            left = max(0, min(image.width - width, center_x - width // 2))
            top = max(0, min(image.height - height, center_y - height // 2))
            crop = image.crop((left, top, left + width, top + height))
        if scale > 1:
            # Chat bubbles use anti-aliased small fonts.  Nearest-neighbour
            # enlargement can merge adjacent waybill digits (for example
            # “64” into “M”), producing a false negative after a successful
            # send.  Lanczos keeps the glyph edges legible to Windows OCR.
            try:
                from PIL import Image

                resample = Image.Resampling.LANCZOS
            except (ImportError, AttributeError):  # pragma: no cover - Pillow is bundled on Windows
                resample = None
            resize_args: dict[str, Any] = {}
            if resample is not None:
                resize_args["resample"] = resample
            crop = crop.resize((crop.width * scale, crop.height * scale), **resize_args)

        # Outgoing WeChat bubbles have dark text on a green fill.  Windows
        # OCR can confuse the two adjacent 5 glyphs with a Chinese glyph when
        # fed the coloured surface.  A component may opt into a local binary
        # preprocessing pass; this never leaves the machine and is used only
        # for a tight sent-message crop.
        if str(config.get("ocr_preprocess") or "").strip().lower() == "threshold":
            try:
                from PIL import ImageOps

                threshold = max(0, min(int(config.get("ocr_threshold", 180)), 255))
                crop = ImageOps.grayscale(crop).point(
                    lambda pixel: 0 if pixel < threshold else 255
                )
            except (ImportError, TypeError, ValueError) as exc:
                raise DesktopExecutionError("OCR 二值化参数无效") from exc

        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        ocr_dir = self.evidence_dir / "ocr_temp"
        ocr_dir.mkdir(parents=True, exist_ok=True)
        image_path = ocr_dir / f"ocr_{time.time_ns()}.png"
        try:
            crop.save(image_path)
            payload = recognize_image(image_path, timeout_seconds=timeout)
        except WindowsOcrError as exc:
            raise DesktopExecutionError(f"Windows OCR 读取失败：{exc}") from exc
        except Exception as exc:
            raise DesktopExecutionError(f"OCR 文本组件读取失败：{exc}") from exc
        finally:
            try:
                image_path.unlink(missing_ok=True)
            except Exception:
                pass
        value = str(payload.get("text") or "").strip()
        if not value:
            raise DesktopExecutionError("Windows OCR 未读到组件文本")
        return value

    @staticmethod
    def _wechat_sent_bubble_crop(image: Any, config: dict[str, Any]) -> Any:
        """Return the newest right-side green WeChat bubble from a screenshot."""
        width, height = image.size
        if width < 100 or height < 100:
            raise DesktopExecutionError("微信窗口截图尺寸无效")
        try:
            min_x_ratio = float(config.get("ocr_bubble_min_x_ratio", 0.50))
            min_y_ratio = float(config.get("ocr_bubble_min_y_ratio", 0.16))
            max_y_ratio = float(config.get("ocr_bubble_max_y_ratio", 0.86))
        except (TypeError, ValueError) as exc:
            raise DesktopExecutionError("微信消息气泡识别参数无效") from exc
        min_x = max(0, min(width - 1, int(width * min_x_ratio)))
        min_y = max(0, min(height - 1, int(height * min_y_ratio)))
        max_y = max(min_y + 1, min(height, int(height * max_y_ratio)))
        step = 2
        pixels = image.load()

        def is_bubble_green(x: int, y: int) -> bool:
            red, green, blue = pixels[x, y]
            return green >= 170 and green - red >= 45 and green - blue >= 40

        visited: set[tuple[int, int]] = set()
        candidates: list[tuple[int, int, int, int, int]] = []
        for y in range(min_y, max_y, step):
            for x in range(min_x, width, step):
                point = (x, y)
                if point in visited or not is_bubble_green(x, y):
                    continue
                stack = [point]
                visited.add(point)
                left = right = x
                top = bottom = y
                size = 0
                while stack:
                    current_x, current_y = stack.pop()
                    size += 1
                    left = min(left, current_x)
                    right = max(right, current_x)
                    top = min(top, current_y)
                    bottom = max(bottom, current_y)
                    for next_x, next_y in (
                        (current_x - step, current_y),
                        (current_x + step, current_y),
                        (current_x, current_y - step),
                        (current_x, current_y + step),
                    ):
                        next_point = (next_x, next_y)
                        if (
                            next_x < min_x
                            or next_x >= width
                            or next_y < min_y
                            or next_y >= max_y
                            or next_point in visited
                            or not is_bubble_green(next_x, next_y)
                        ):
                            continue
                        visited.add(next_point)
                        stack.append(next_point)
                if (
                    size >= 120
                    and right - left >= max(60, int(width * 0.08))
                    and bottom - top >= 12
                ):
                    candidates.append((left, top, right, bottom, size))
        if not candidates:
            raise DesktopExecutionError("未定位到微信已发送消息气泡")
        left, top, right, bottom, _ = max(candidates, key=lambda item: (item[3], item[4]))
        margin_x = max(14, int(width * 0.015))
        margin_y = max(12, int(height * 0.018))
        return image.crop(
            (
                max(0, left - margin_x),
                max(0, top - margin_y),
                min(width, right + margin_x + step),
                min(height, bottom + margin_y + step),
            )
        )

    @staticmethod
    def _visual_grid_count(window: Any, config: dict[str, Any] | None = None) -> int:
        """Count visible WDT result rows from one image without moving the mouse.

        The result table is custom-painted, so UIA does not reliably expose
        cells. A visible WDT row can be either selected (yellow/red fill) or
        unselected (white fill with only its text visible).  The old capture
        recognised only the first form, so a real white result row was
        incorrectly treated as no query result.  This remains pixel-only: it
        checks both fill colour and row-text ink at a few vertical probes,
        without OCR or mouse movement.
        """
        config = config or {}
        image = window.capture_as_image().convert("RGB")
        first_y = int(float(config.get("first_row_y_ratio", 0.298)) * image.height)
        row_height = int(float(config.get("row_height_ratio", 0.0615)) * image.height)
        left = int(float(config.get("left_ratio", 0.02)) * image.width)
        right = int(float(config.get("right_ratio", 0.94)) * image.width)
        half_height = int(config.get("half_height", 10))
        threshold = int(config.get("color_distance", 35))
        min_chroma = int(config.get("min_chroma", 8))
        minimum = float(config.get("min_colored_ratio", 0.3))
        minimum_text = float(config.get("min_text_ratio", 0.006))
        # ``first_row_y`` is the captured top/anchor of the first data row,
        # not its text baseline. Probing at offset 0 also crosses WDT's
        # adjacent ``合计`` footer for the next candidate row, which can make
        # one result look like two. Stay inside the row's text band instead.
        raw_offsets = config.get("row_probe_offsets", (24, 28))
        if not isinstance(raw_offsets, (list, tuple)):
            raw_offsets = (24, 28)
        probe_offsets = tuple(dict.fromkeys(int(value) for value in raw_offsets)) or (0,)
        count = 0
        for index in range(int(config.get("max_rows", 6))):
            row_visible = False
            for probe_offset in probe_offsets:
                y = first_y + index * row_height + probe_offset
                crop = image.crop(
                    (left, max(0, y - half_height), right, min(image.height, y + half_height + 1))
                )
                pixels = list(crop.getdata())
                colored = sum(
                    1
                    for red, green, blue in pixels
                    if (255 - red) + (255 - green) + (255 - blue) > threshold
                    and max(red, green, blue) - min(red, green, blue) >= min_chroma
                )
                text_ink = sum(
                    1
                    for red, green, blue in pixels
                    if min(red, green, blue) < 180
                    or blue > red + 35 and blue > green + 10
                )
                size = max(len(pixels), 1)
                if colored / size >= minimum or text_ink / size >= minimum_text:
                    row_visible = True
                    break
            if not row_visible:
                break
            count += 1
        return count

    @staticmethod
    def _normalise_chat_text(value: Any) -> str:
        """Compare desktop-chat OCR without layout whitespace."""

        # Windows OCR may emit full-width punctuation, zero-width markers,
        # and a space between every Chinese glyph.  NFKC keeps the comparison
        # stable without changing the visible group name used for the final
        # header verification.
        text = unicodedata.normalize("NFKC", str(value or "").strip())
        return re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", text)

    @staticmethod
    def _bounded_ocr_edit_distance(left: str, right: str, limit: int) -> int:
        """Return an edit distance capped at ``limit + 1``.

        Search-result OCR is allowed a small, bounded amount of glyph drift.
        The cap keeps this helper cheap and prevents a long unrelated group
        name from becoming a fuzzy match.
        """

        left = str(left or "")
        right = str(right or "")
        limit = max(0, int(limit))
        if abs(len(left) - len(right)) > limit:
            return limit + 1
        previous = list(range(len(right) + 1))
        for row_index, char_left in enumerate(left, 1):
            current = [row_index]
            row_minimum = row_index
            for column_index, char_right in enumerate(right, 1):
                value = min(
                    previous[column_index] + 1,
                    current[column_index - 1] + 1,
                    previous[column_index - 1] + (char_left != char_right),
                )
                current.append(value)
                row_minimum = min(row_minimum, value)
            if row_minimum > limit:
                return limit + 1
            previous = current
        return previous[-1]

    @classmethod
    def _chat_group_text_matches(cls, expected_group: str, observed: Any) -> bool:
        """Match an exact group name while tolerating OCR bracket glyphs.

        The searchable Chinese/name segments remain mandatory.  This is only
        used after the candidate has already been constrained to a chat-result
        row; it does not make a network/global-search item selectable.
        """

        expected = cls._normalise_chat_text(expected_group)
        actual = cls._normalise_chat_text(observed)
        if not expected or not actual:
            return False
        # WeCom appends the live member count to the visible title, e.g.
        # ``目标群（32）``.  It is presentation metadata, not part of the
        # configured group identity.
        actual = re.sub(r"[（(]\d+[）)]$", "", actual)
        if expected in actual:
            return True
        expected_tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{3,}", expected)
        observed_tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{3,}", actual)
        if not expected_tokens or not observed_tokens:
            return False

        def within_ocr_tolerance(left: str, right: str) -> bool:
            # Keep short names strict (one glyph at most), while allowing a
            # second drift in longer carrier/service-group names.  The final
            # header readback remains mandatory before any message is sent.
            tolerance = 1 if len(left) <= 4 else min(2, max(1, len(left) // 4))
            return DesktopComponentExecutor._bounded_ocr_edit_distance(
                left, right, tolerance
            ) <= tolerance

        # Each meaningful Chinese/alphanumeric segment must map to one full
        # observed segment with at most one glyph error.  This is much tighter
        # than accepting a partial phrase and keeps an unrelated group from
        # being selected merely because it shares “快递” or “中通”.
        return all(
            any(
                within_ocr_tolerance(expected_token, observed_token)
                for observed_token in observed_tokens
            )
            for expected_token in expected_tokens
        )

    @classmethod
    def _is_wecom_global_search_separator(cls, value: Any) -> bool:
        """Recognise Enterprise WeChat's ``进入全局搜索`` action row."""

        text = cls._normalise_chat_text(value)
        # The shortcut is rendered on the global-search action itself and is
        # more stable than Chinese OCR (the live machine read 全局搜索 as
        # ``全到索``).  Treating it as a boundary prevents clicking that row.
        if "CTRL+ALT+F" in text.upper():
            return True
        if "全局搜索" in text:
            return True
        # OCR frequently turns the final ``索`` into a neighbouring glyph.
        # Requiring both the global-search marker and a search glyph keeps
        # this conservative while still protecting the network-search area.
        if "全局" in text and "搜" in text:
            return True
        chinese_prefix = "".join(re.findall(r"[\u4e00-\u9fff]", text))[:6]
        return (
            chinese_prefix.startswith("进入")
            and cls._bounded_ocr_edit_distance(
                chinese_prefix, "进入全局搜索", 2
            ) <= 2
        )

    @classmethod
    def _wecom_exact_search_candidate_from_ocr(
        cls,
        payload: dict[str, Any],
        expected_group: str,
        popup_width: int,
        popup_height: int,
        *,
        scale: int = 1,
        max_chat_result_y_ratio: float = 0.72,
    ) -> dict[str, Any] | None:
        """Select only one exact Enterprise WeChat group-list result.

        ``SearchResultWindow2`` contains both a chat-group row and an
        ``进入全局搜索`` action.  The latter can open a separate global-search
        window and must never be used as a group-navigation fallback.
        """

        if not cls._normalise_chat_text(expected_group) or popup_width <= 0 or popup_height <= 0:
            return None
        try:
            factor = max(1, int(scale))
            max_ratio = max(0.25, min(float(max_chat_result_y_ratio), 0.9))
        except (TypeError, ValueError):
            return None
        rows: list[dict[str, Any]] = []
        for line in list(payload.get("lines") or []):
            words = list(line.get("words") or []) if isinstance(line, dict) else []
            values: list[tuple[float, float, float, float, str]] = []
            for word in words:
                if not isinstance(word, dict):
                    continue
                try:
                    left = float(word.get("left")) / factor
                    top = float(word.get("top")) / factor
                    width = float(word.get("width")) / factor
                    height = float(word.get("height")) / factor
                except (TypeError, ValueError):
                    continue
                if width > 0 and height > 0:
                    values.append((left, top, left + width, top + height, str(word.get("text") or "")))
            if not values:
                continue
            text = str(line.get("text") or "") or "".join(item[4] for item in values)
            rows.append(
                {
                    "text": text,
                    "left": min(item[0] for item in values),
                    "top": min(item[1] for item in values),
                    "right": max(item[2] for item in values),
                    "bottom": max(item[3] for item in values),
                }
            )
        # Windows OCR can split a single title row into two lines when the
        # trailing WeCom member count is rendered in a different colour.  The
        # live ``示例品类 G`` result was returned as the name plus a separate
        # ``（32）`` fragment at the same y-coordinate.  Merge only vertically
        # overlapping OCR lines so one visual result remains one candidate.
        merged_rows: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda item: (float(item["top"]), float(item["left"]))):
            match = next(
                (
                    item
                    for item in merged_rows
                    if min(float(item["bottom"]), float(row["bottom"]))
                    - max(float(item["top"]), float(row["top"]))
                    >= 3
                ),
                None,
            )
            if match is None:
                merged_rows.append(dict(row))
                continue
            pieces = [
                (float(match["left"]), str(match["text"])),
                (float(row["left"]), str(row["text"])),
            ]
            match["text"] = "".join(text for _left, text in sorted(pieces))
            match["left"] = min(float(match["left"]), float(row["left"]))
            match["top"] = min(float(match["top"]), float(row["top"]))
            match["right"] = max(float(match["right"]), float(row["right"]))
            match["bottom"] = max(float(match["bottom"]), float(row["bottom"]))
        rows = merged_rows
        separator_tops = [
            float(row["top"])
            for row in rows
            if cls._is_wecom_global_search_separator(row["text"])
        ]
        global_search_top = min(separator_tops) if separator_tops else None
        eligible_rows: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        for row in rows:
            center_x = (float(row["left"]) + float(row["right"])) / 2
            center_y = (float(row["top"]) + float(row["bottom"])) / 2
            if not (0 <= center_x < popup_width and 0 <= center_y < popup_height):
                continue
            if global_search_top is not None:
                if center_y >= global_search_top - 3:
                    continue
            elif center_y > popup_height * max_ratio:
                continue
            text = str(row["text"])
            normalised = cls._normalise_chat_text(text)
            # ``群聊`` is the section heading, not a selectable result.  The
            # global-search row has already been excluded by its y position,
            # but keep the text guard for OCR layouts without a separator.
            if not normalised or normalised in {"群聊", "群组", "联系人"}:
                continue
            if cls._is_wecom_global_search_separator(text):
                continue
            candidate = {
                "point_in_popup": (int(round(center_x)), int(round(center_y))),
                "observed_text": text,
                "row": {
                    "left": round(float(row["left"]), 1),
                    "top": round(float(row["top"]), 1),
                    "right": round(float(row["right"]), 1),
                    "bottom": round(float(row["bottom"]), 1),
                },
                "global_search_top": round(global_search_top, 1) if global_search_top is not None else None,
            }
            eligible_rows.append(candidate)
            if cls._chat_group_text_matches(expected_group, text):
                candidates.append(candidate)

        # Prefer a textual match whenever one exists.  WeCom renders the
        # exact group title and a second description line for the same result;
        # both lines can contain a short name such as ``示例供应商 B``.  Treating
        # those two OCR lines as two independent results makes the match look
        # ambiguous even though they are one card.  The topmost textual line
        # is the card title and is safe to select: it is still above the
        # global-search boundary, and the caller must read the opened group
        # header exactly before any input or send action is allowed.
        if len(candidates) == 1:
            candidates[0]["match_mode"] = "ocr_text"
            return candidates[0]
        if len(candidates) > 1:
            candidates.sort(key=lambda item: float(item["row"]["top"]))
            candidates[0]["match_mode"] = "ocr_topmost_text"
            return candidates[0]
        # If OCR mangles the short Chinese group name, a single eligible chat
        # row is still safe to try under the same mandatory header readback.
        if not candidates and len(eligible_rows) == 1:
            eligible_rows[0]["match_mode"] = "unique_chat_row"
            return eligible_rows[0]
        return None

    def _wecom_search_popup(self, window: Any, component: dict[str, Any]) -> Any:
        """Resolve the visible Enterprise WeChat result popup by native identity."""

        locator = dict(component.get("locator") or {})
        expected = dict(locator.get("point_window") or {})
        expected_title = str(expected.get("title") or "SearchResultWindow2").strip()
        expected_class = str(expected.get("class_name") or "SearchResultWindow2").strip()
        try:
            process_id = int(window.process_id())
        except Exception as exc:
            raise DesktopExecutionError("无法读取企业微信主窗口进程") from exc
        candidates: list[Any] = []
        try:
            for item in self.Desktop(backend="win32").windows(visible_only=True):
                try:
                    if int(item.process_id()) != process_id:
                        continue
                    if expected_title and str(item.window_text() or "").strip() != expected_title:
                        continue
                    if expected_class and str(item.class_name() or "").strip() != expected_class:
                        continue
                    rect = item.rectangle()
                    if rect.width() < 160 or rect.height() < 50:
                        continue
                    candidates.append(item)
                except Exception:
                    continue
        except Exception as exc:
            raise DesktopExecutionError("无法枚举企业微信搜索结果弹层") from exc
        candidates = self._dedupe_windows(candidates)
        if len(candidates) != 1:
            raise DesktopExecutionError(
                f"企业微信聊天搜索弹层不唯一：{len(candidates)}；已拒绝点击全局搜索结果"
            )
        return candidates[0]

    @staticmethod
    def _wecom_search_image_variant(image: Any, scale: int, enhanced: bool) -> Any:
        """Prepare one local OCR variant of a WeCom search popup screenshot."""

        try:
            from PIL import Image, ImageEnhance, ImageOps
        except ImportError as exc:  # pragma: no cover - Pillow is a runtime dependency
            raise DesktopExecutionError("企业微信搜索 OCR 缺少 Pillow 依赖") from exc

        result = image.convert("RGB")
        factor = max(1, min(int(scale), 4))
        if factor > 1:
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:  # pragma: no cover - old Pillow fallback
                resample = Image.LANCZOS
            result = result.resize((result.width * factor, result.height * factor), resample=resample)
        if enhanced:
            # Autocontrast helps the light-gray popup text without hard
            # thresholding away thin Chinese strokes.
            gray = ImageOps.autocontrast(ImageOps.grayscale(result))
            result = ImageEnhance.Contrast(gray).enhance(1.35).convert("RGB")
        return result

    def _locate_wecom_exact_group_search_result_once(
        self, component: dict[str, Any], context: dict[str, Any], window: Any
    ) -> dict[str, Any]:
        """OCR one Enterprise WeChat group row and forbid global-search clicks."""

        config = dict(component.get("config") or {})
        variable = str(config.get("value_variable") or "group_name")
        expected_group = str(context.get(variable) or "").strip()
        if not expected_group:
            raise DesktopExecutionError("企业微信聊天搜索缺少群名")
        popup = self._wecom_search_popup(window, component)
        image_path: Path | None = None
        try:
            image = popup.capture_as_image().convert("RGB")
            popup_rect = popup.rectangle()
            if image.width < 160 or image.height < 50:
                raise DesktopExecutionError("企业微信聊天搜索弹层截图尺寸无效")
            scale = max(1, min(int(config.get("ocr_scale", 3)), 4))
            image = self._wecom_search_image_variant(
                image,
                scale,
                bool(config.get("ocr_enhanced", False)),
            )
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            ocr_dir = self.evidence_dir / "ocr_temp"
            ocr_dir.mkdir(parents=True, exist_ok=True)
            image_path = ocr_dir / f"wecom_search_{time.time_ns()}.png"
            image.save(image_path)
            payload = recognize_image(
                image_path,
                timeout_seconds=max(2.0, min(float(config.get("ocr_timeout_seconds", 12)), 25.0)),
            )
        except WindowsOcrError as exc:
            raise DesktopExecutionError(f"企业微信聊天搜索 OCR 失败：{exc}") from exc
        except DesktopExecutionError:
            raise
        except Exception as exc:
            raise DesktopExecutionError(f"无法识别企业微信聊天搜索结果：{exc}") from exc
        finally:
            if image_path is not None:
                try:
                    image_path.unlink(missing_ok=True)
                except Exception:
                    pass
        candidate = self._wecom_exact_search_candidate_from_ocr(
            payload,
            expected_group,
            image.width // scale,
            image.height // scale,
            scale=scale,
            max_chat_result_y_ratio=float(config.get("chat_result_max_y_ratio", 0.72)),
        )
        if candidate is None:
            raise DesktopExecutionError(
                f"企业微信未找到聊天列表中与群名完全一致的结果：{expected_group}；已拒绝点击全局搜索"
            )
        popup_x, popup_y = candidate.pop("point_in_popup")
        candidate["point"] = (int(popup_rect.left) + popup_x, int(popup_rect.top) + popup_y)
        candidate["expected_group"] = expected_group
        return candidate

    def _locate_wecom_exact_group_search_result(
        self, component: dict[str, Any], context: dict[str, Any], window: Any
    ) -> dict[str, Any]:
        """Retry the guarded popup OCR before failing closed.

        Enterprise WeChat redraws ``SearchResultWindow2`` asynchronously.  A
        transient empty/low-quality OCR frame is retried with an enhanced
        local image, but the same popup geometry and global-search exclusion
        remain in force for every attempt.
        """

        base_config = dict(component.get("config") or {})
        try:
            retry_count = max(0, min(int(base_config.get("search_retry_times", 2)), 3))
        except (TypeError, ValueError):
            retry_count = 2
        try:
            retry_wait = max(
                0.15,
                min(float(base_config.get("search_retry_wait_seconds", 0.35)), 2.0),
            )
        except (TypeError, ValueError):
            retry_wait = 0.35

        last_error: DesktopExecutionError | None = None
        for attempt in range(retry_count + 1):
            attempt_component = component
            if attempt:
                attempt_component = dict(component)
                attempt_config = dict(base_config)
                attempt_config["ocr_enhanced"] = True
                attempt_component["config"] = attempt_config
            try:
                return self._locate_wecom_exact_group_search_result_once(
                    attempt_component,
                    context,
                    window,
                )
            except DesktopExecutionError as exc:
                last_error = exc
                if attempt < retry_count:
                    time.sleep(retry_wait)
        if last_error is not None:
            raise last_error
        raise DesktopExecutionError("企业微信聊天搜索结果定位失败")

    def execute(self, component: dict[str, Any], context: dict[str, Any]) -> Any:
        component_type = component["component_type"]
        config = component.get("config") or {}
        if component_type == "ReadDesktopText" and self._semantic_role(component) == "wdt_product_names":
            window, _, _ = self._resolve_window(component)
            with self._preserve_clipboard():
                value = self._read_wdt_product_names(component, window)
            if not value:
                raise DesktopExecutionError("旺店通货品列表未读取到货品名称")
            output = str(config.get("output_variable", "wdt_product_names"))
            context[output] = value
            return value
        if component_type == "ReadDesktopText":
            semantic_value = self._semantic_row_value(component, context)
            if semantic_value:
                output = str(config.get("output_variable", ""))
                if output:
                    context[output] = semantic_value
                return semantic_value
        if component_type == "ActivateDesktopWindow":
            self._resolve(component)
            return True
        if component_type == "WaitDesktopState":
            # WDT used to sleep a fixed 15 seconds after every search.  Poll
            # the painted result area instead: return as soon as a row is
            # visible, and let the following count assertion handle a real
            # zero-result query.  No cursor movement occurs in this loop.
            if component.get("component_id") == "CMP-WDT-WAIT-RESULT" or config.get("poll_for_grid"):
                return self._wait_for_wdt_query_result(component, context)
            time.sleep(max(0.2, min(float(config.get("seconds", 1)), 30)))
            return True
        if component_type == "AssertDesktopQueryResult":
            return self._assert_query(context, config)
        if component_type == "ReadDesktopGridRow":
            self._active_semantic_row_text = ""
            window, _ = self._resolve(component)
            semantic_row = self._semantic_grid_row(window, context)
            if semantic_row is not None:
                count = int(semantic_row.get("count", 0) or 0)
                context[str(config.get("output_variable", "wdt_result_count"))] = count
                if count == 1 and not semantic_row.get("ambiguous"):
                    context["_wdt_semantic_row"] = semantic_row
                return count
            count = 0
            for attempt in range(4):
                count = self._visual_grid_count(window, config)
                if count or attempt == 3:
                    break
                time.sleep(0.5)
            context[str(config.get("output_variable", "wdt_result_count"))] = count
            return count
        semantic_role = self._semantic_role(component)
        if component_type == "ClickDesktopElement" and semantic_role == "wdt_goods_list_tab":
            window, _, _ = self._resolve_window(component)
            point = self._visual_wdt_goods_tab_point(window)
            if not point:
                raise DesktopExecutionError("旺店通货品列表标签无法视觉定位")
            self.mouse.click(coords=point)
            time.sleep(float(config.get("after_wait_seconds", 0.15)))
            return True
        if (
            component_type == "ClickDesktopElement"
            and semantic_role == "wecom_exact_group_search_result"
        ):
            window, _, _ = self._resolve_window(component)
            candidate = self._locate_wecom_exact_group_search_result(
                component, context, window
            )
            self.mouse.click(coords=candidate["point"])
            context[f"_{semantic_role}_match"] = candidate
            time.sleep(float(config.get("after_wait_seconds", 0.5)))
            return True
        window, point = self._resolve(component)
        target = getattr(self, "_last_resolved_target", None)
        if point is None and target is None:
            raise DesktopExecutionError(f"组件缺少捕获位置：{component['name']}")
        if component_type == "ReadDesktopText" and str(config.get("read_strategy") or "").lower() == "ocr":
            value = self._read_captured_ocr(component, window)
            output = str(config.get("output_variable", ""))
            if output:
                context[output] = value
            return value
        # A click does not need clipboard access.  Keeping the click outside
        # _preserve_clipboard avoids a clipboard restore racing with the
        # application's search-result redraw (the failure observed in WDT).
        if component_type == "ClickDesktopElement":
            if target is not None:
                clicked = False
                try:
                    target.click_input()
                    clicked = True
                except Exception:
                    try:
                        target.invoke()
                        clicked = True
                    except Exception:
                        pass
                if not clicked:
                    raise DesktopExecutionError("语义控件无法点击")
            else:
                self.mouse.click(coords=point)
            time.sleep(float(config.get("after_wait_seconds", 0.5)))
            if config.get("wait_for_window_ready"):
                self._wait_for_window_responsive(
                    window, float(config.get("response_timeout_seconds", 5))
                )
            return True

        # WDT's custom-painted waybill input must not use Ctrl+V. Type the
        # value directly; after search, the result-row reader independently
        # verifies that WDT returned the same waybill.
        if component_type == "InputDesktopText" and (
            semantic_role == "tracking_input"
            or str(config.get("input_strategy") or "").lower()
            == "direct_keys_no_clipboard"
        ):
            variable = str(config.get("value_variable", ""))
            value = str(context.get(variable, ""))
            if not value:
                raise DesktopExecutionError(f"输入变量为空：{variable}")
            return self._input_wdt_tracking_number(
                target=target,
                point=point,
                value=value,
                config=config,
                context=context,
            )

        with self._preserve_clipboard():
            if component_type == "InputDesktopText":
                variable = str(config.get("value_variable", ""))
                value = str(context.get(variable, ""))
                if not value:
                    raise DesktopExecutionError(f"输入变量为空：{variable}")
                if target is not None:
                    try:
                        target.click_input()
                    except Exception as exc:
                        raise DesktopExecutionError("语义输入框无法聚焦") from exc
                else:
                    self.mouse.click(coords=point)
                self.keyboard.send_keys("^a")
                self._clipboard_set(value)
                self.keyboard.send_keys("^v")
                self.keyboard.send_keys("^a")
                self.keyboard.send_keys("^c")
                time.sleep(0.15)
                if self._clipboard_get().strip() != value:
                    raise DesktopExecutionError("输入框回读与目标文本不一致")
                return value
            if component_type == "ReadDesktopText":
                # The custom-painted WDT grid does not expose individual
                # cells through UIA. Bind every WDT result-row field to a
                # narrow guarded scan around its current column centre.
                if target is None and self._semantic_role(component) in {
                    "wdt_order_id",
                    "wdt_platform",
                    "wdt_original_order",
                    "wdt_waybill",
                    "wdt_order_status",
                    "wdt_warehouse",
                    "wdt_logistics_company",
                }:
                    value = self._read_wdt_grid_role(component, context, window)
                    if value:
                        output = str(config.get("output_variable", ""))
                        if output:
                            context[output] = value
                        return value
                if target is not None:
                    value = ""
                    try:
                        value = str(target.window_text() or "").strip()
                    except Exception:
                        try:
                            value = str(target.get_value() or "").strip()
                        except Exception:
                            value = ""
                    if value:
                        output = str(config.get("output_variable", ""))
                        if output:
                            context[output] = value
                        return value
                value = ""
                for attempt in range(3):
                    sentinel = f"__EMPTY_{time.time_ns()}__"
                    self._clipboard_set(sentinel)
                    self.mouse.click(coords=point)
                    time.sleep(0.2 + attempt * 0.15)
                    self.keyboard.send_keys("^c")
                    time.sleep(0.2)
                    copied = self._clipboard_get().strip()
                    if copied != sentinel and copied:
                        value = copied
                        break
                output = str(config.get("output_variable", ""))
                if output:
                    context[output] = value
                return value
        raise DesktopExecutionError(f"尚未实现桌面组件：{component_type}")

    @staticmethod
    def _assert_query(context: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        visual_count = int(context.get("wdt_result_count", 0) or 0)
        expected_tracking = str(context.get("tracking_no", "")).strip()
        actual_tracking = str(context.get("wdt_waybill", "")).strip()
        if not expected_tracking or actual_tracking != expected_tracking:
            raise DesktopExecutionError("旺店通物流单号与拼多多不一致")
        expected_platform = str(config.get("expected_platform", "")).strip()
        actual_platform = str(context.get("wdt_platform", "")).strip()
        if expected_platform and actual_platform != expected_platform:
            raise DesktopExecutionError(
                f"旺店通平台与预期不一致：{actual_platform or '空'}"
            )
        require_original_order = bool(config.get("require_original_order_match"))
        expected_order = re.sub(r"\s+", "", str(context.get("order_no", "")))
        actual_original_order = re.sub(
            r"\s+", "", str(context.get("wdt_original_order", ""))
        )
        if require_original_order and (
            not expected_order or expected_order not in actual_original_order
        ):
            raise DesktopExecutionError(
                "旺店通原始订单号与拼多多不一致："
                f"期望={expected_order or '空'}，实际={actual_original_order or '空'}"
            )

        # WDT's custom-painted table sometimes cannot be counted from pixels
        # even though the exact queried waybill, platform, order id and carrier
        # have all been read from the same row.  The in-transit flow is allowed
        # to use that exact-row proof only for a visual zero; a visual count of
        # two or more still always fails closed.
        exact_tracking_fallback = bool(config.get("allow_exact_tracking_fallback")) and (
            visual_count == 0
            and bool(actual_platform)
            and bool(str(context.get("wdt_order_id", "")).strip())
            and bool(str(context.get("wdt_logistics_company", "")).strip())
            and (not require_original_order or expected_order in actual_original_order)
        )
        if visual_count != 1 and not exact_tracking_fallback:
            raise DesktopExecutionError(f"旺店通稳定后查询结果数量不是1：{visual_count}")
        status = str(context.get("wdt_order_status", "")).strip()
        skip_status_check = bool(config.get("skip_status_check"))
        # 已发货无轨迹必须按订单状态白名单决定是否退款；在途无理由
        # 只依赖唯一订单、平台、示例自营仓和物流公司完成拦截路由，不读取
        # 也不使用订单状态。不能让这个共享断言把在途分支提前截断。
        if not skip_status_check and not status:
            raise DesktopExecutionError("旺店通订单状态为空，无法判断是否可退款")
        allowed = {str(value).strip() for value in config.get("allowed_statuses", [])}
        interception_statuses = {
            str(value).strip()
            for value in config.get("interception_statuses", [])
            if str(value).strip()
        }
        if not skip_status_check and status in interception_statuses:
            context["wdt_eligible"] = False
            context["wdt_requires_interception"] = True
            return {
                "eligible": False,
                "requires_interception": True,
                "order_status": status,
                "visual_result_count": visual_count,
            }
        if not skip_status_check and status not in allowed:
            raise DesktopExecutionError(f"旺店通订单状态不允许退款：{status}")
        context["wdt_eligible"] = True
        context["wdt_requires_interception"] = False
        return {
            "eligible": True,
            "order_status": status,
            "unique_result_verified_by": (
                "tracking_input_platform_original_order_carrier"
                if exact_tracking_fallback
                else (
                    "visual_result_count_tracking_input_platform_original_order"
                    if expected_platform and require_original_order
                    else (
                        "visual_result_count_tracking_input_platform"
                        if expected_platform
                        else "visual_result_count_tracking_input"
                    )
                )
            ),
            "visual_result_count": visual_count,
        }
