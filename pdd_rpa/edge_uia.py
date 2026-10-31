from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .desktop_executor import DesktopComponentExecutor
from .pdd_page_identity import (
    AFTERSALE_DETAIL,
    AFTERSALE_LIST,
    AFTERSALE_SETUP,
    LOGIN,
    WORK_ORDER_DETAIL,
    WORK_ORDER_LIST,
    classify_pdd_surface,
)


class PddUiaError(RuntimeError):
    pass


class PddPageIdentityError(PddUiaError):
    """The active PDD page does not match the operation's page contract."""


class PddOrderNotFoundError(PddUiaError):
    """The exact order filter returned no work order on the current list."""


class PddAlreadyProcessedError(PddUiaError):
    """The work-order detail is already completed or has a refund in progress."""


class PddSidebarNavigationError(PddUiaError):
    """A verified PDD side-navigation entry could not be resolved safely."""


def classify_pdd_url(url: str) -> str:
    return {
        LOGIN: "login",
        WORK_ORDER_LIST: "list",
        WORK_ORDER_DETAIL: "detail",
    }.get(classify_pdd_surface(url), "other")


ORDER_RE = re.compile(r"^\d{6}-\d{15,}$")
TRACKING_RE = re.compile(r"^(?:YT\d{10,}|[A-Z]{1,5}\d{8,}|\d{12,})$", re.I)
TRACKING_FIND_RE = re.compile(r"(?<![A-Z0-9])(?:YT\d{10,}|[A-Z]{1,5}\d{8,}|\d{12,})(?![A-Z0-9])", re.I)
CANONICAL_WORK_ORDER_LIST_URL = (
    "https://mms.pinduoduo.com/aftersales/work_order/list?msfrom=mms_sidenav"
)


@dataclass(slots=True)
class UiaCard:
    order_no: str
    work_order_title: str
    action_name: str
    order_rect: tuple[int, int, int, int]
    action_rect: tuple[int, int, int, int]
    page_no: int = 1
    page_index: int = 0


@dataclass(slots=True)
class UiaListResult:
    handle: int
    title: str
    url: str
    total: int
    cards: list[UiaCard]


@dataclass(slots=True)
class LoginRecoveryResult:
    """Auditable result of the safe PDD login-page recovery step.

    The recovery intentionally never reads or writes credentials.  It only
    activates the already configured account-login tab, clicks login,
    dismisses a uniquely identifiable platform modal, and navigates to the
    allow-listed work-order URL.  UIA controls are preferred; the PDD login
    page has a tightly constrained window-relative fallback because Edge can
    expose that page's web controls with empty accessible names.
    """

    status: str
    shop_id: str
    shop_name: str
    previous_url: str = ""
    final_url: str = ""
    account_tab_clicked: bool = False
    login_clicked: bool = False
    popup_closed: bool = False
    manual_required: bool = False
    trace: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "shop_id": self.shop_id,
            "shop_name": self.shop_name,
            "previous_url": self.previous_url,
            "final_url": self.final_url,
            "account_tab_clicked": self.account_tab_clicked,
            "login_clicked": self.login_clicked,
            "popup_closed": self.popup_closed,
            "manual_required": self.manual_required,
            "trace": list(self.trace or []),
            "credentials_read": False,
            "credentials_written": False,
            "business_write_performed": False,
        }


class PddEdgeUiaSession:
    """Operate PDD pages through Windows UI Automation only.

    This class never selects/copies the whole page and never injects JavaScript.
    """

    def __init__(self, config: dict[str, Any]):
        try:
            import win32gui
            from pywinauto import Desktop, keyboard, mouse
        except ImportError as exc:
            raise PddUiaError("缺少 pywinauto/pywin32") from exc
        self.config = config
        self.win32gui = win32gui
        self.Desktop = Desktop
        self.keyboard = keyboard
        self.mouse = mouse
        self.last_login_recovery: dict[str, Any] | None = None
        self.last_pagination_trace: list[dict[str, Any]] = []
        self.last_collection_summary: dict[str, Any] = {}

    def assert_page_contract(
        self,
        window: Any,
        shop: dict[str, Any] | None,
        expected_surface: str,
        *,
        order_no: str = "",
        stage: str = "",
        require_ready: bool = False,
        handle_override: int | None = None,
    ) -> dict[str, Any]:
        """Fail closed unless the active tab is the exact expected PDD page.

        URL surface, shop identity and (for details) order identity are checked
        together.  Semantic list markers are optionally required immediately
        before collection/clicking so a half-loaded or wrong page cannot be
        consumed as business data.
        """

        url = self._current_url(window)
        actual_surface = classify_pdd_surface(url)
        handle = int(
            handle_override
            if handle_override is not None
            else window.handle
        )
        stage_label = stage or "未命名阶段"
        if actual_surface != expected_surface:
            raise PddPageIdentityError(
                "页面身份不符："
                f"阶段={stage_label}，预期={expected_surface}，"
                f"实际={actual_surface}，URL={url or '<空>'}；已禁止采集/点击"
            )

        shop_name = str((shop or {}).get("name") or "").strip()
        if shop_name and not (
            shop_name in str(window.window_text() or "")
            or self._has_exact_name(handle, shop_name)
        ):
            raise PddPageIdentityError(
                "页面店铺身份不符："
                f"阶段={stage_label}，预期店铺={shop_name}，"
                f"页面={actual_surface}；已禁止采集/点击"
            )

        if order_no:
            order_count = sum(
                1
                for control in self._controls(handle)
                if self._name(control) == order_no
            )
            query = parse_qs(urlparse(url).query)
            url_orders = [
                str(item or "").strip()
                for item in query.get("orderSn", [])
                if str(item or "").strip()
            ]
            # PDD's after-sale page can expose the same visible order field
            # twice in Chromium's accessibility tree.  Duplicate equal nodes
            # are safe only when the immutable detail URL independently names
            # this exact order; wrong/absent identities still fail closed.
            url_order_verified = (
                expected_surface == AFTERSALE_DETAIL
                and url_orders == [order_no]
            )
            if order_count < 1 or (order_count != 1 and not url_order_verified):
                raise PddPageIdentityError(
                    "页面订单身份不符："
                    f"阶段={stage_label}，预期订单={order_no}，"
                    f"页面精确匹配数={order_count}，"
                    f"URL订单核验={url_order_verified}；已禁止采集/点击"
                )

        if require_ready:
            if expected_surface == WORK_ORDER_LIST:
                self._pending_filter(handle)
            elif expected_surface == AFTERSALE_LIST:
                names = [self._name(control) for control in self._controls(handle)]
                has_query = "售后单查询" in names
                has_pending = any(
                    name == "待处理即将逾期" or name.startswith("即将逾期售后")
                    for name in names
                )
                if not (has_query and has_pending):
                    raise PddPageIdentityError(
                        "售后工作台页面语义未就绪："
                        f"阶段={stage_label}，售后单查询={has_query}，"
                        f"待处理即将逾期={has_pending}；已禁止采集/点击"
                    )

        return {
            "stage": stage_label,
            "surface": actual_surface,
            "url": url,
            "shop": shop_name,
            "order_no": order_no,
            "ready": True,
        }

    def _windows(self) -> list[Any]:
        pattern = self.config.get("window_title_pattern", ".*Microsoft.*Edge")
        return [
            item
            for item in self.Desktop(backend="win32").windows(title_re=pattern, visible_only=True)
            if item.friendly_class_name() == "Chrome_WidgetWin_1"
        ]

    def _ensure_window_maximized(self, window: Any) -> bool:
        """Restore and maximize the exact Edge HWND before any page action."""

        handle = int(getattr(window, "handle", 0) or 0)
        try:
            is_maximized = getattr(window, "is_maximized", None)
            if callable(is_maximized) and bool(is_maximized()):
                return True
            if handle and hasattr(self.win32gui, "IsZoomed") and bool(
                self.win32gui.IsZoomed(handle)
            ):
                return True
            is_minimized = getattr(window, "is_minimized", None)
            restore = getattr(window, "restore", None)
            if callable(is_minimized) and bool(is_minimized()) and callable(restore):
                restore()
            maximize = getattr(window, "maximize", None)
            if callable(maximize):
                maximize()
            time.sleep(0.12)
            if callable(is_maximized) and bool(is_maximized()):
                return True
            if handle and hasattr(self.win32gui, "IsZoomed") and bool(
                self.win32gui.IsZoomed(handle)
            ):
                return True
            if handle and hasattr(self.win32gui, "ShowWindow"):
                self.win32gui.ShowWindow(handle, 3)  # SW_MAXIMIZE
                time.sleep(0.12)
                if hasattr(self.win32gui, "IsZoomed"):
                    return bool(self.win32gui.IsZoomed(handle))
                return True
        except Exception:
            return False
        return False

    def _focus(self, window: Any) -> None:
        last_error: Exception | None = None
        for _ in range(3):
            try:
                if window.is_minimized():
                    window.restore()
                window.set_focus()
                if not self._ensure_window_maximized(window):
                    last_error = RuntimeError("Edge 窗口未能确认最大化")
                    time.sleep(0.25)
                    continue
            except Exception as exc:
                last_error = exc
            time.sleep(0.25)
            if (
                int(self.win32gui.GetForegroundWindow()) == int(window.handle)
                and self._ensure_window_maximized(window)
            ):
                return
        # The isolated scan worker is commonly blocked by Windows foreground
        # locking even though the registered Edge window is healthy. Reuse
        # the established desktop-action foreground bridge first, then fall
        # back to a harmless click on Edge's title bar—not on a PDD control.
        if DesktopComponentExecutor._force_foreground(int(window.handle)):
            if self._ensure_window_maximized(window):
                return
            last_error = RuntimeError("Edge 窗口已置前但未能确认最大化")
        try:
            window.click_input(coords=(5, 5))
            time.sleep(0.08)
            if (
                int(self.win32gui.GetForegroundWindow()) == int(window.handle)
                and self._ensure_window_maximized(window)
            ):
                return
            last_error = RuntimeError("标题栏点击后窗口未置前或未最大化")
        except Exception as exc:
            last_error = exc
        raise PddUiaError(f"Edge 窗口未取得前台焦点：{last_error or 'unknown'}")

    def _uia_descendants(
        self, handle: int, *, control_type: str = ""
    ) -> list[Any]:
        """Wrap valid UIA descendants one by one, skipping untyped nodes."""

        ui_window = self.Desktop(backend="uia").window(handle=handle)
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                raw_root = getattr(ui_window, "element_info", None)
                backend = getattr(ui_window, "backend", None)
                if raw_root is not None and backend is not None:
                    wrapped: list[Any] = []
                    for element_info in raw_root.descendants():
                        current_type = str(
                            getattr(element_info, "control_type", "") or ""
                        )
                        if not current_type:
                            continue
                        if control_type and current_type != control_type:
                            continue
                        try:
                            wrapped.append(backend.generic_wrapper_class(element_info))
                        except (KeyError, AttributeError, RuntimeError):
                            continue
                    return wrapped
                return ui_window.descendants(
                    **({"control_type": control_type} if control_type else {})
                )
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(0.12 * (attempt + 1))
        raise PddUiaError(
            f"UIA 控件树在页面重绘后仍不可用：{last_error or 'unknown'}"
        ) from last_error

    def _current_url(self, window: Any) -> str:
        for edit in self._uia_descendants(int(window.handle), control_type="Edit"):
            try:
                name = str(edit.element_info.name or "").strip().lower()
                if not any(token in name for token in ("地址栏", "地址和搜索栏", "address bar")):
                    continue
                value = str(edit.get_value() or "").strip()
                if value:
                    return value
            except Exception:
                continue
        return ""

    def _controls(self, handle: int, visible_only: bool = False) -> list[Any]:
        """Return usable UIA descendants, ignoring transient untyped nodes.

        PDD rebuilds parts of the detail page immediately after a form submit.
        During that short redraw UIA can expose a descendant whose
        ``control_type`` is ``None``.  ``pywinauto``'s bulk ``descendants()``
        wrapper then raises ``KeyError: None`` for the *whole* page, although
        the target controls are otherwise available.  Enumerate the raw UIA
        elements first and wrap valid entries one by one so that a single
        transient browser node cannot abort a refund stage.
        """
        controls = self._uia_descendants(handle)
        if not visible_only:
            return controls
        result = []
        for control in controls:
            try:
                if control.is_visible():
                    result.append(control)
            except Exception:
                continue
        return result

    @staticmethod
    def _name(control: Any) -> str:
        return str(control.element_info.name or "").strip()

    @staticmethod
    def _rect(control: Any) -> tuple[int, int, int, int]:
        rect = control.rectangle()
        return rect.left, rect.top, rect.right, rect.bottom

    @staticmethod
    def _tracking_values_from_text(text: str) -> list[str]:
        """Extract complete waybills even when UIA inserts invisible spaces.

        PDD occasionally exposes one visual waybill as several accessible text
        fragments.  Only ASCII letter/digit runs are joined; arbitrary page
        text is never used to manufacture a tracking number.
        """

        compact = re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", str(text or "")).upper()
        values = TRACKING_FIND_RE.findall(compact)
        if TRACKING_RE.fullmatch(compact):
            values.insert(0, compact)
        return list(dict.fromkeys(values))

    @staticmethod
    def _tracking_fragment(text: str) -> str:
        compact = re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", str(text or "")).upper()
        return compact if re.fullmatch(r"[A-Z0-9]{3,}", compact) else ""

    def _detail_tracking_candidates(
        self, controls: list[Any], order_no: str
    ) -> list[tuple[Any, str, tuple[int, int, int, int]]]:
        order_fragments = {order_no.replace("-", ""), *order_no.split("-")}
        contact_markers = (
            "客服电话",
            "客服热线",
            "联系电话",
            "联系方式",
            "手机号",
            "电话",
            "热线",
            "致电",
            "拨打",
        )
        candidates: list[tuple[Any, str, tuple[int, int, int, int]]] = []
        for control in controls:
            rect = self._rect(control)
            name = self._name(control)
            if any(marker in name for marker in contact_markers):
                continue
            for value in self._tracking_values_from_text(name):
                if value not in order_fragments:
                    candidates.append((control, value, rect))

        labels = [
            control
            for control in controls
            if self._name(control).rstrip("：:") in {"物流信息", "快递信息", "运单信息"}
        ]
        if not labels:
            return candidates
        label = max(labels, key=lambda item: self._rect(item)[1])
        label_left, label_top, _, label_bottom = self._rect(label)
        row_controls = []
        for control in controls:
            left, top, right, bottom = self._rect(control)
            name = self._name(control)
            if any(marker in name for marker in contact_markers):
                continue
            if left < label_left or top > label_bottom + 50 or bottom < label_top - 12:
                continue
            row_controls.append((left, top, right, control, name))

        # Reconstruct only close, same-line ASCII fragments. This covers a
        # waybill split into two UIA controls without joining order numbers,
        # amounts, phone numbers or logistics history from other rows.
        row_controls.sort(key=lambda item: (item[1], item[0]))
        for index, (left, top, right, control, name) in enumerate(row_controls):
            fragment = self._tracking_fragment(name)
            if not fragment:
                continue
            joined = fragment
            last_right = right
            for next_left, next_top, next_right, _next_control, next_name in row_controls[index + 1 :]:
                if abs(next_top - top) > 12 or next_left - last_right > 60:
                    break
                next_fragment = self._tracking_fragment(next_name)
                if not next_fragment:
                    continue
                joined += next_fragment
                last_right = next_right
                if TRACKING_RE.fullmatch(joined) and joined not in order_fragments:
                    candidates.append((control, joined.upper(), (left, top, last_right, bottom)))

        row = [
            item
            for item in candidates
            if item[2][0] >= label_left
            and label_top - 12 <= item[2][1] <= label_bottom + 50
        ]
        # Once an exact logistics-row label exists, never fall back to an
        # arbitrary long number elsewhere on the detail page.  Logistics
        # history commonly contains customer-service phone numbers.
        return row

    def _detail_carrier(
        self,
        controls: list[Any],
        tracking_rect: tuple[int, int, int, int],
    ) -> str:
        """Read the carrier from the same logistics row as the waybill.

        A reconstructed waybill can span multiple UIA controls, so carrier
        selection must use the candidate row rectangle, not the first numeric
        fragment's original control rectangle.
        """

        labels = [
            control
            for control in controls
            if self._name(control).rstrip("：:") in {"物流信息", "快递信息", "运单信息"}
        ]
        row_top = tracking_rect[1] - 40
        row_bottom = tracking_rect[3] + 40
        row_left = 0
        if labels:
            label = max(labels, key=lambda item: self._rect(item)[1])
            label_left, label_top, _, label_bottom = self._rect(label)
            row_left = label_left
            row_top = label_top - 12
            row_bottom = label_bottom + 80

        carriers: list[tuple[int, int, str]] = []
        for control in controls:
            name = self._name(control)
            left, top, _, bottom = self._rect(control)
            if left < row_left or top > row_bottom or bottom < row_top:
                continue
            for carrier in re.findall(r"[\u4e00-\u9fff]{2,10}(?:快递|速递)", name):
                distance = abs(top - tracking_rect[1])
                carriers.append((distance, left, carrier))
        return min(carriers, default=(0, 0, ""), key=lambda item: (item[0], item[1]))[2]

    def _has_exact_name(self, handle: int, text: str) -> bool:
        return any(self._name(control) == text for control in self._controls(handle))

    def _visible_named_controls(
        self, handle: int, text: str, control_type: str = ""
    ) -> list[Any]:
        """Return visible UIA controls matching an exact accessible name.

        Callers using a geometry fallback must validate the page, shop and
        window independently; this helper itself never performs fuzzy matching.
        """
        matches: list[Any] = []
        for control in self._controls(handle, visible_only=True):
            try:
                if self._name(control) != text:
                    continue
                actual_type = str(control.element_info.control_type or "")
                if control_type and actual_type != control_type:
                    continue
                matches.append(control)
            except Exception:
                continue
        return matches

    def _visible_class_controls(self, handle: int, class_fragment: str) -> list[Any]:
        matches: list[Any] = []
        for control in self._controls(handle, visible_only=True):
            try:
                class_name = str(control.element_info.class_name or "")
                if class_fragment and class_fragment in class_name:
                    matches.append(control)
            except Exception:
                continue
        return matches

    def _login_challenge_present(self, handle: int) -> bool:
        """Detect a verification/OTP page and stop for manual handling."""
        markers = (
            "验证码",
            "短信验证",
            "手机验证",
            "安全验证",
            "滑块",
            "图形验证",
            "二次验证",
            "captcha",
            "verification code",
        )
        for control in self._controls(handle, visible_only=True):
            try:
                name = self._name(control).lower()
            except Exception:
                continue
            if any(marker.lower() in name for marker in markers):
                return True
        return False

    def _click_login_page_relative(
        self,
        window: Any,
        shop: dict[str, Any],
        action: str,
    ) -> tuple[int, int]:
        """Click one real-tested PDD-login target when Edge hides UIA names.

        The fallback is intentionally unavailable outside the exact shop login
        window, on a non-maximized/undersized browser, or for unknown actions.
        Coordinates are ratios of the whole Edge window so they survive normal
        display scaling while remaining scoped to the established layout.
        """

        if not bool(self.config.get("login_relative_fallback_enabled", True)):
            raise PddUiaError("登录页相对位置降级已关闭")
        if classify_pdd_url(self._current_url(window)) != "login":
            raise PddUiaError("当前已不是拼多多登录页")
        shop_name = str(shop.get("name", "")).strip()
        if not shop_name or shop_name not in str(window.window_text() or ""):
            raise PddUiaError("登录窗口标题与目标店铺不一致")

        self._focus(window)
        rect = window.rectangle()
        width = int(rect.width())
        height = int(rect.height())
        min_width = int(self.config.get("login_relative_min_width", 1200))
        min_height = int(self.config.get("login_relative_min_height", 700))
        if width < min_width or height < min_height:
            raise PddUiaError(
                f"登录窗口尺寸不足：{width}x{height}，要求至少 {min_width}x{min_height}"
            )

        defaults = {
            "account_tab": (0.733, 0.378),
            "login_button": (0.702, 0.555),
        }
        if action not in defaults:
            raise PddUiaError(f"未知登录页相对点击动作：{action}")
        default_x, default_y = defaults[action]
        x_ratio = float(
            self.config.get(f"login_relative_{action}_x_ratio", default_x)
        )
        y_ratio = float(
            self.config.get(f"login_relative_{action}_y_ratio", default_y)
        )
        if not (0.0 < x_ratio < 1.0 and 0.0 < y_ratio < 1.0):
            raise PddUiaError(f"登录页相对点击比例无效：{x_ratio}, {y_ratio}")
        point = (
            int(round(rect.left + width * x_ratio)),
            int(round(rect.top + height * y_ratio)),
        )
        self.mouse.click(coords=point)
        return point

    @staticmethod
    def _canonical_work_order_url(value: Any) -> str:
        """Validate a configured work-order URL and return a key-safe constant.

        ``pywinauto.keyboard.send_keys`` treats characters such as ``+``, ``^``
        and ``{...}`` as keyboard syntax.  Never type a configurable query or
        fragment verbatim even after checking only its host/path.
        """

        target = str(value or "").strip()
        parsed = urlparse(target)
        if (
            parsed.scheme.lower() != "https"
            or parsed.netloc.lower() != "mms.pinduoduo.com"
            or parsed.path != "/aftersales/work_order/list"
            or parsed.params
            or parsed.query not in {"", "msfrom=mms_sidenav"}
            or parsed.fragment
        ):
            raise PddUiaError("目标地址不是固定的拼多多工单列表白名单地址")
        return CANONICAL_WORK_ORDER_LIST_URL

    def _allowlisted_work_order_url(self) -> str:
        target = self.config.get("list_url", CANONICAL_WORK_ORDER_LIST_URL)
        return self._canonical_work_order_url(target)

    @staticmethod
    def _title_matches_shop(window: Any, shop_name: str) -> bool:
        """Use a bounded title match only when page-level exact text is hidden."""

        expected = re.sub(r"\s+", " ", str(shop_name or "")).strip()
        title = re.sub(r"\s+", " ", str(window.window_text() or "")).strip()
        if not expected or not title:
            return False
        separators = (" - ", " — ", " – ", " | ", "_")
        return title == expected or any(
            title.startswith(expected + separator) or f"{separator}{expected}{separator}" in title
            for separator in separators
        )

    def _window_matches_shop(self, window: Any, shop: dict[str, Any]) -> bool:
        handle = int(window.handle)
        expected = str(shop.get("name") or "").strip()
        return bool(
            expected
            and (
                self._has_exact_name(handle, expected)
                or self._title_matches_shop(window, expected)
            )
        )

    def navigate_url(
        self,
        window: Any,
        url: str,
        *,
        expected_shop: dict[str, Any] | None = None,
        allowed_source_surfaces: set[str] | None = None,
    ) -> None:
        """Navigate one verified Edge window using its address bar."""
        target = self._canonical_work_order_url(url)
        self._focus(window)
        # Re-read the active tab after focus.  This closes the practical gap
        # between the caller's source-page preflight and Ctrl+L: if another tab
        # became active, no keyboard input is emitted.
        if allowed_source_surfaces is not None:
            current_url = self._current_url(window)
            source_surface = classify_pdd_surface(current_url)
            if source_surface not in allowed_source_surfaces:
                raise PddUiaError(
                    "地址栏导航前源页面已变化："
                    f"surface={source_surface}"
                )
        if expected_shop is not None and not self._window_matches_shop(
            window, expected_shop
        ):
            raise PddUiaError("地址栏导航前店铺身份已变化")
        self.keyboard.send_keys("^l")
        time.sleep(float(self.config.get("address_bar_ready_wait_seconds", 0.5)))
        self.keyboard.send_keys(target, with_spaces=True, pause=0.01)
        self.keyboard.send_keys("{ENTER}")

    def dismiss_forced_login_popup(self, window: Any) -> dict[str, Any]:
        """Close the observed post-login modal only with a unique UIA target."""
        handle = int(window.handle)
        try:
            timeout = max(
                1.0,
                min(float(self.config.get("login_popup_wait_seconds", 5)), 15.0),
            )
        except (TypeError, ValueError):
            timeout = 5.0
        deadline = time.monotonic() + timeout
        checked = False
        popup_closed = False
        close_fragment = str(
            self.config.get("login_popup_close_class_contains", "MDL_headerCloseIcon")
        )
        close_names = tuple(
            self.config.get(
                "login_popup_close_names", ["关闭", "知道了", "我知道了"]
            )
        )
        while time.monotonic() < deadline:
            close_controls = self._visible_class_controls(handle, close_fragment)
            containers = self._visible_class_controls(handle, "MDL_container")
            if len(close_controls) > 1:
                raise PddUiaError(
                    f"登录后弹窗关闭控件不唯一：{len(close_controls)}"
                )
            if len(close_controls) == 1:
                self._focus(window)
                close_controls[0].click_input()
                popup_closed = True
                checked = True
                time.sleep(0.5)
                if not self._visible_class_controls(handle, close_fragment):
                    return {
                        "popup_present": True,
                        "popup_closed": True,
                        "close_locator": {
                            "class_contains": close_fragment,
                            "control_type": str(
                                close_controls[0].element_info.control_type or ""
                            ),
                        },
                    }
                continue
            if len(containers) > 1:
                raise PddUiaError(f"登录后弹窗容器不唯一：{len(containers)}")
            if len(containers) == 1:
                checked = True
                named = [
                    control
                    for name in close_names
                    for control in self._visible_named_controls(handle, str(name))
                ]
                if len(named) == 1:
                    self._focus(window)
                    named[0].click_input()
                    popup_closed = True
                    time.sleep(0.5)
                    continue
                if len(named) > 1:
                    raise PddUiaError(
                        f"登录后弹窗文字关闭控件不唯一：{len(named)}"
                    )
                # A modal is visible but has no safe, unique close target.
                raise PddUiaError("登录后出现弹窗，但未找到唯一安全关闭控件")
            # The modal may be injected a fraction after the home page appears.
            if checked or time.monotonic() + 0.35 >= deadline:
                return {
                    "popup_present": False,
                    "popup_closed": popup_closed,
                    "close_locator": None,
                }
            time.sleep(0.25)
            checked = True
        return {
            "popup_present": bool(popup_closed),
            "popup_closed": popup_closed,
            "close_locator": {"class_contains": close_fragment}
            if popup_closed
            else None,
        }

    def recover_shop_login(
        self, shop: dict[str, Any], window: Any | None = None
    ) -> LoginRecoveryResult:
        """Recover one shop's login page without touching credentials."""
        candidates = self._shop_windows(shop)
        if window is None:
            login_windows = [
                item
                for item in candidates
                if classify_pdd_url(self._current_url(item)) == "login"
            ]
            if not login_windows:
                return LoginRecoveryResult(
                    "not_needed",
                    str(shop.get("id", "")),
                    str(shop["name"]),
                    final_url=self._current_url(candidates[0]) if len(candidates) == 1 else "",
                    trace=["no_login_page"],
                )
            if len(login_windows) != 1:
                raise PddUiaError(
                    f"{shop['name']} 的登录窗口不唯一：{len(login_windows)}"
                )
            window = login_windows[0]
        previous_url = self._current_url(window)
        if classify_pdd_url(previous_url) != "login":
            return LoginRecoveryResult(
                "not_needed",
                str(shop.get("id", "")),
                str(shop["name"]),
                previous_url=previous_url,
                final_url=previous_url,
                trace=["not_login_url"],
            )
        handle = int(window.handle)
        trace = ["login_page_detected"]
        result = LoginRecoveryResult(
            "manual_required",
            str(shop.get("id", "")),
            str(shop["name"]),
            previous_url=previous_url,
            manual_required=True,
            trace=trace,
        )

        account_tabs = self._visible_named_controls(handle, "账号登录")
        if len(account_tabs) > 1:
            raise PddUiaError(f"账号登录控件不唯一：{len(account_tabs)}")
        relative_fallback_active = False
        if len(account_tabs) == 1:
            self._focus(window)
            account_tabs[0].click_input()
            result.account_tab_clicked = True
            trace.append("click_account_login")
            time.sleep(0.5)
        else:
            try:
                point = self._click_login_page_relative(window, shop, "account_tab")
            except PddUiaError as exc:
                trace.append(f"account_login_relative_fallback_unavailable:{exc}")
                result.final_url = self._current_url(window)
                return result
            result.account_tab_clicked = True
            relative_fallback_active = True
            trace.append(f"click_account_login_relative:{point[0]},{point[1]}")
            time.sleep(float(self.config.get("login_account_form_wait_seconds", 1.0)))

        deadline = time.monotonic() + float(
            self.config.get(
                "login_relative_uia_probe_seconds" if relative_fallback_active
                else "login_button_wait_seconds",
                1.0 if relative_fallback_active else 12,
            )
        )
        login_button: Any | None = None
        while time.monotonic() < deadline:
            if self._login_challenge_present(handle):
                result.trace.append("manual_verification_required")
                result.final_url = self._current_url(window)
                return result
            buttons = self._visible_named_controls(handle, "登录", "Button")
            if len(buttons) > 1:
                raise PddUiaError(f"登录控件不唯一：{len(buttons)}")
            if len(buttons) == 1:
                login_button = buttons[0]
                break
            time.sleep(0.35)
        if login_button is not None:
            self._focus(window)
            login_button.click_input()
            trace.append("click_login")
        elif relative_fallback_active:
            if self._login_challenge_present(handle):
                trace.append("manual_verification_required")
                result.final_url = self._current_url(window)
                return result
            try:
                point = self._click_login_page_relative(window, shop, "login_button")
            except PddUiaError as exc:
                trace.append(f"login_relative_fallback_unavailable:{exc}")
                result.final_url = self._current_url(window)
                return result
            trace.append(f"click_login_relative:{point[0]},{point[1]}")
        else:
            trace.append("login_button_not_ready")
            result.final_url = self._current_url(window)
            return result
        result.login_clicked = True
        transition_deadline = time.monotonic() + float(
            self.config.get("login_transition_timeout_seconds", 30)
        )
        while time.monotonic() < transition_deadline:
            current_url = self._current_url(window)
            if self._login_challenge_present(handle):
                result.trace.append("manual_verification_required")
                result.final_url = current_url
                return result
            if classify_pdd_url(current_url) != "login":
                break
            time.sleep(0.5)
        current_url = self._current_url(window)
        if classify_pdd_url(current_url) == "login":
            result.trace.append("login_transition_timeout")
            result.final_url = current_url
            return result

        trace.append("login_success")
        popup = self.dismiss_forced_login_popup(window)
        result.popup_closed = bool(popup.get("popup_closed"))
        if popup.get("popup_present"):
            trace.append("dismiss_forced_popup")
        target_url = self._allowlisted_work_order_url()
        self.navigate_url(window, target_url)
        list_deadline = time.monotonic() + float(
            self.config.get("login_list_wait_seconds", 30)
        )
        while time.monotonic() < list_deadline:
            current_url = self._current_url(window)
            if classify_pdd_url(current_url) == "list":
                if self._has_exact_name(handle, shop["name"]) or shop["name"] in window.window_text():
                    result.status = "ready"
                    result.manual_required = False
                    result.final_url = current_url
                    trace.append("navigate_work_order_list")
                    return result
            time.sleep(0.5)
        result.trace.append("work_order_list_not_ready")
        result.final_url = self._current_url(window)
        return result

    def ensure_shop_list(self, shop: dict[str, Any]) -> dict[str, Any]:
        """Ensure a shop is logged in before a list refresh is attempted."""
        candidates = self._shop_windows(shop)
        login_windows = [
            window
            for window in candidates
            if classify_pdd_url(self._current_url(window)) == "login"
        ]
        if not login_windows:
            result = {
                "status": "not_needed",
                "shop_id": str(shop.get("id", "")),
                "shop_name": str(shop["name"]),
                "trace": ["no_login_window"],
                "credentials_read": False,
                "credentials_written": False,
                "business_write_performed": False,
            }
            self.last_login_recovery = result
            return result
        if len(login_windows) != 1:
            raise PddUiaError(f"{shop['name']} 的登录窗口不唯一：{len(login_windows)}")
        result = self.recover_shop_login(shop, login_windows[0])
        if result.status != "ready":
            raise PddUiaError(
                f"{shop['name']} 登录恢复未完成：{result.as_dict()}"
            )
        data = result.as_dict()
        self.last_login_recovery = data
        return data

    def _shop_windows(self, shop: dict[str, Any]) -> list[Any]:
        """Return visible Edge windows belonging to a configured shop.

        Edge exposes tabs inside one top-level window.  The window title normally
        includes the active tab's shop name, while a background tab is not
        represented by a separate Win32 window.  Fall back to an exact UIA shop
        name check so tab switching can recover a list tab from the same window.
        """
        windows = self._windows()
        titled = [window for window in windows if shop["name"] in window.window_text()]
        if titled:
            return titled

        matches = []
        for window in windows:
            try:
                handle = int(window.handle)
                if self._has_exact_name(handle, shop["name"]):
                    matches.append(window)
            except Exception:
                continue
        return matches

    def _activate_tab_kind(self, window: Any, shop: dict[str, Any], expected_kind: str) -> bool:
        """Cycle the active Edge tab until the requested PDD page is visible."""
        try:
            attempts = max(1, min(int(self.config.get("tab_switch_attempts", 8)), 20))
        except (TypeError, ValueError):
            attempts = 8
        try:
            wait_seconds = max(0.15, min(float(self.config.get("tab_switch_wait_seconds", 0.35)), 2.0))
        except (TypeError, ValueError):
            wait_seconds = 0.35

        seen: set[tuple[str, str]] = set()
        for _ in range(attempts + 1):
            url = self._current_url(window)
            handle = int(window.handle)
            if classify_pdd_url(url) == expected_kind and self._has_exact_name(handle, shop["name"]):
                return True
            signature = (url, window.window_text())
            if signature in seen:
                break
            seen.add(signature)
            self._focus(window)
            self.keyboard.send_keys("^{TAB}")
            time.sleep(wait_seconds)
        return False

    def _activate_tab_url(
        self, window: Any, shop: dict[str, Any], expected_url: str
    ) -> bool:
        """Activate one exact PDD tab without closing any other tab."""
        try:
            attempts = max(1, min(int(self.config.get("tab_switch_attempts", 8)), 20))
        except (TypeError, ValueError):
            attempts = 8
        try:
            wait_seconds = max(
                0.15,
                min(float(self.config.get("tab_switch_wait_seconds", 0.35)), 2.0),
            )
        except (TypeError, ValueError):
            wait_seconds = 0.35

        seen: set[tuple[str, str]] = set()
        for _ in range(attempts + 1):
            try:
                url = self._current_url(window)
                title = window.window_text()
                handle = int(window.handle)
            except Exception:
                return False
            if url == expected_url and (
                shop["name"] in title or self._has_exact_name(handle, shop["name"])
            ):
                return True
            signature = (url, title)
            if signature in seen:
                return False
            seen.add(signature)
            self._focus(window)
            self.keyboard.send_keys("^{TAB}")
            time.sleep(wait_seconds)
        return False

    def restore_shop_list_anchor(
        self, shop: dict[str, Any], preferred_window: Any | None = None
    ) -> dict[str, Any]:
        """Leave one verified shop list page open without ever closing a window.

        A retained detail or aftersale-workbench page can be the only tab in an
        Edge window.  The old scan recovery sent Ctrl+W first and checked for
        the list afterwards, which closed the whole profile window in that
        case. Recovery is now ordered the other way around: discover a
        background list tab first, otherwise navigate the existing detail or
        aftersale page to the allowlisted list URL in place.
        """
        try:
            list_window = self.window_for_shop(shop, "list")
            return {
                "closed": 0,
                "navigated": 0,
                "list_ready": bool(list_window),
                "handled": True,
                "disposition": "list_already_ready",
            }
        except Exception:
            pass

        candidates = self._shop_windows(shop)
        aftersale_windows = []
        for candidate in candidates:
            try:
                if self._is_aftersale_page(self._current_url(candidate)):
                    aftersale_windows.append(candidate)
            except Exception:
                continue
        if len(aftersale_windows) == 1:
            return self._navigate_window_to_work_order_list(
                shop,
                aftersale_windows[0],
                disposition="aftersale_navigated_to_list",
            )
        if len(aftersale_windows) > 1:
            raise PddUiaError(
                f"{shop['name']} 售后工作台窗口不唯一：{len(aftersale_windows)}"
            )

        detail_windows = []
        for candidate in candidates:
            try:
                if classify_pdd_url(self._current_url(candidate)) == "detail":
                    detail_windows.append(candidate)
            except Exception:
                continue
        target = preferred_window
        if target is not None:
            try:
                current_url = self._current_url(target)
                if classify_pdd_url(current_url) != "detail" and not self._is_aftersale_page(current_url):
                    target = None
            except Exception:
                target = None
        if target is None:
            if len(detail_windows) != 1:
                raise PddUiaError(
                    f"{shop['name']} 无可安全恢复的唯一详情窗口：{len(detail_windows)}"
                )
            target = detail_windows[0]

        return self._navigate_window_to_work_order_list(
            shop,
            target,
            disposition="detail_navigated_to_list",
        )

    @staticmethod
    def _is_aftersale_page(url: str) -> bool:
        """Recognize aftersale pages without importing the subclass module."""
        return classify_pdd_surface(url) in {
            AFTERSALE_LIST,
            AFTERSALE_DETAIL,
            AFTERSALE_SETUP,
        }

    def _navigate_window_to_work_order_list(
        self,
        shop: dict[str, Any],
        target: Any,
        *,
        disposition: str,
    ) -> dict[str, Any]:
        """Navigate one verified Edge window to the work-order list and verify it."""

        source_url = self._current_url(target)
        source_surface = classify_pdd_surface(source_url)
        allowed_sources = {
            WORK_ORDER_DETAIL,
            AFTERSALE_LIST,
            AFTERSALE_DETAIL,
            AFTERSALE_SETUP,
        }
        if source_surface not in allowed_sources or not self._window_matches_shop(
            target, shop
        ):
            raise PddUiaError(
                f"{shop['name']} 工单列表恢复前页面身份校验未通过："
                f"surface={source_surface}"
            )
        navigation_method = "sidebar"
        try:
            self._click_sidebar_entry(target, "工单管理")
        except PddSidebarNavigationError as sidebar_error:
            # Chromium occasionally exposes the aftersale document but none of
            # its left-navigation descendants through UIA.  Do not guess a
            # coordinate in that state.  The address-bar fallback is limited to
            # one exact, allowlisted PDD list URL and is allowed only while the
            # source tab still proves both its PDD surface and shop identity.
            current_url = self._current_url(target)
            parsed = urlparse(current_url)
            surface = classify_pdd_surface(current_url)
            source_is_safe = (
                parsed.scheme == "https"
                and parsed.netloc.lower() == "mms.pinduoduo.com"
                and surface in allowed_sources
            )
            shop_is_verified = self._window_matches_shop(target, shop)
            if not source_is_safe or not shop_is_verified:
                raise PddUiaError(
                    f"{shop['name']} 侧栏导航失败且地址栏回退安全校验未通过："
                    f"surface={surface}, shop_verified={shop_is_verified}"
                ) from sidebar_error
            self.navigate_url(
                target,
                self._allowlisted_work_order_url(),
                expected_shop=shop,
                allowed_source_surfaces=allowed_sources,
            )
            navigation_method = "allowlisted_address_bar"
        try:
            timeout = max(
                2.0,
                min(float(self.config.get("list_restore_timeout_seconds", 30)), 60.0),
            )
        except (TypeError, ValueError):
            timeout = 30.0
        deadline = time.monotonic() + timeout
        login_attempted = False
        while time.monotonic() < deadline:
            time.sleep(0.5)
            current_url = self._current_url(target)
            kind = classify_pdd_url(current_url)
            if kind == "login" and not login_attempted:
                login_attempted = True
                recovery = self.recover_shop_login(shop, target)
                if recovery.status != "ready":
                    raise PddUiaError(
                        f"{shop['name']} 列表恢复遇到登录页且自动登录未完成：{recovery.as_dict()}"
                    )
                current_url = self._current_url(target)
                kind = classify_pdd_url(current_url)
            if kind != "list":
                continue
            handle = int(target.handle)
            if self._window_matches_shop(target, shop):
                try:
                    self.assert_page_contract(
                        target,
                        shop,
                        WORK_ORDER_LIST,
                        stage="导航回工单管理",
                        require_ready=True,
                    )
                except PddPageIdentityError:
                    continue
                return {
                    "closed": 0,
                    "navigated": 1,
                    "list_ready": True,
                    "handled": True,
                    "disposition": disposition,
                    "navigation_method": navigation_method,
                }
        raise PddUiaError(f"{shop['name']} 导航回工单列表超时")

    def _click_sidebar_entry(self, window: Any, text: str) -> None:
        """Navigate PDD through one semantic left-menu entry.

        PDD appends a live badge count to the accessible menu name (for
        example ``售后工作台 85`` and ``工单管理5``).  Chromium also exposes
        both the ``ListItem`` wrapper and its ``Hyperlink`` child, while the
        same business entry can appear once in the shortcuts area and once in
        its real navigation group.  Exact-name/unique-count matching therefore
        reports zero or multiple controls even though the menu is visible.

        Resolve the actionable hyperlink through its stable ``nav-item``
        wrapper, prefer the entry carrying PDD's navigation automation id,
        and fail closed on a genuine score tie.  Page/shop verification stays
        in the caller and is mandatory after the click.
        """

        self._ensure_window_maximized(window)
        self._dismiss_known_pdd_overlays(window)
        handle = int(window.handle)
        deadline = time.monotonic() + max(
            3.0, min(float(self.config.get("sidebar_navigation_timeout_seconds", 15)), 30.0)
        )
        last_diagnostic = "未发现候选"
        while time.monotonic() < deadline:
            candidates: dict[
                tuple[str, tuple[int, int, int, int]],
                tuple[int, Any, str],
            ] = {}
            for control in self._controls(handle, visible_only=True):
                try:
                    if not self._sidebar_label_matches(self._name(control), text):
                        continue
                    resolved = self._sidebar_action_candidate(control)
                    if resolved is None:
                        continue
                    action, wrapper, score = resolved
                    rect = self._rect(wrapper)
                    automation_id = str(wrapper.element_info.automation_id or "")
                    key = (automation_id, rect)
                    description = (
                        f"name={self._name(control)!r},"
                        f"type={control.element_info.control_type!r},"
                        f"wrapper={wrapper.element_info.control_type!r},"
                        f"automation_id={automation_id!r},rect={rect},score={score}"
                    )
                    previous = candidates.get(key)
                    if previous is None or score > previous[0]:
                        candidates[key] = (score, action, description)
                except Exception:
                    continue

            ranked = sorted(candidates.values(), key=lambda item: item[0], reverse=True)
            if ranked:
                best_score = ranked[0][0]
                best = [item for item in ranked if item[0] == best_score]
                last_diagnostic = "; ".join(item[2] for item in ranked[:4])
            else:
                best = []
                last_diagnostic = "未发现左侧导航可点击候选"
            if len(best) == 1:
                self._focus(window)
                best[0][1].click_input()
                return
            time.sleep(0.3)
        raise PddSidebarNavigationError(
            f"左侧导航“{text}”无法唯一定位：{last_diagnostic}"
        )

    def _dismiss_known_pdd_overlays(self, window: Any) -> dict[str, Any]:
        """Close only semantically identified PDD ad/modal-local controls.

        PDD injects the same ``MDL_container`` advertising layers on both the
        work-order and aftersale surfaces.  They can cover the side navigation
        or list actions while leaving the underlying controls absent from UIA.
        This helper never clicks a free-floating ``关闭`` label: a candidate
        must live inside a sizeable modal container, and every click is followed
        by a fresh UIA scan.
        """

        surface = classify_pdd_surface(self._current_url(window))
        if surface not in {
            WORK_ORDER_LIST,
            WORK_ORDER_DETAIL,
            AFTERSALE_LIST,
            AFTERSALE_DETAIL,
            AFTERSALE_SETUP,
        }:
            return {"closed": 0, "clear": True, "surface": surface}
        handle = int(window.handle)
        max_layers = max(
            1, min(int(self.config.get("pdd_popup_max_layers", 6)), 10)
        )
        quiet_seconds = max(
            0.2, min(float(self.config.get("pdd_popup_quiet_seconds", 1.2)), 3.0)
        )
        total_wait_seconds = max(
            quiet_seconds + 0.5,
            min(float(self.config.get("pdd_popup_total_wait_seconds", 8.0)), 15.0),
        )
        dismiss_names = {
            "关闭",
            "关闭弹窗",
            "暂不",
            "暂不参与",
            "我知道了",
            "×",
            "✕",
            "\ue6b1",
        }
        business_modal_markers = (
            "是否确认",
            "确认退款",
            "同意退款",
            "退款",
            "延长收货",
            "提交",
            "确认处理",
            "处理工单",
            "售后处理",
            "订单号",
        )
        closed = 0
        quiet_since: float | None = None
        unreadable_samples = 0
        deadline = time.monotonic() + total_wait_seconds
        while time.monotonic() < deadline:
            class_candidates: list[tuple[Any, Any]] = []
            named_candidates: list[tuple[Any, Any]] = []
            blocked_business_modals: set[tuple[str, tuple[int, int, int, int]]] = set()
            unreadable_modal_candidates = 0
            for control in self._controls(handle, visible_only=True):
                try:
                    class_name = str(control.element_info.class_name or "")
                    name = self._name(control).strip()
                    parent = control
                    modal = None
                    for _depth in range(5):
                        parent = parent.parent()
                        if parent is None:
                            break
                        parent_class = str(parent.element_info.class_name or "")
                        rect = parent.rectangle()
                        control_type = str(parent.element_info.control_type or "")
                        if "MDL_container" in parent_class:
                            modal = parent
                            break
                        if (
                            name in dismiss_names
                            and control_type in {"Window", "Pane", "Group"}
                            and rect.width() >= 280
                            and rect.height() >= 140
                            and rect.left >= 120
                        ):
                            modal = parent
                            break
                    if modal is None:
                        continue
                    is_close_candidate = (
                        "MDL_headerCloseIcon" in class_name or name in dismiss_names
                    )
                    if not is_close_candidate:
                        continue
                    try:
                        modal_nodes = [modal, *modal.descendants()]
                    except Exception:
                        unreadable_modal_candidates += 1
                        continue
                    modal_text = "\n".join(
                        filter(
                            None,
                            (self._name(item).strip() for item in modal_nodes),
                        )
                    )
                    modal_key = (
                        str(modal.element_info.class_name or ""),
                        self._rect(modal),
                    )
                    if any(marker in modal_text for marker in business_modal_markers) or any(
                        ORDER_RE.fullmatch(line.strip())
                        for line in modal_text.splitlines()
                    ):
                        blocked_business_modals.add(modal_key)
                        continue
                    if "MDL_headerCloseIcon" in class_name:
                        class_candidates.append((control, modal))
                    elif name in dismiss_names:
                        named_candidates.append((control, modal))
                except Exception:
                    continue
            candidates = class_candidates or named_candidates
            if not candidates:
                if blocked_business_modals:
                    raise PddUiaError(
                        "检测到业务确认弹窗，禁止按广告弹窗自动关闭"
                    )
                if unreadable_modal_candidates:
                    unreadable_samples += 1
                    if unreadable_samples < 3:
                        time.sleep(0.2)
                        continue
                    raise PddUiaError("拼多多弹窗内容无法安全识别，已禁止自动关闭")
                unreadable_samples = 0
                if quiet_since is None:
                    quiet_since = time.monotonic()
                if time.monotonic() - quiet_since >= quiet_seconds:
                    return {"closed": closed, "clear": True, "surface": surface}
                time.sleep(0.2)
                continue
            unreadable_samples = 0
            if not class_candidates:
                named_modal_keys = {
                    (
                        str(modal.element_info.class_name or ""),
                        self._rect(modal),
                    )
                    for _control, modal in named_candidates
                }
                if len(named_modal_keys) != 1:
                    raise PddUiaError(
                        f"拼多多文字关闭弹窗不唯一：{len(named_modal_keys)}"
                    )
            if closed >= max_layers:
                raise PddUiaError(f"拼多多广告弹窗超过安全上限：{max_layers} 层")
            control, overlay = max(
                candidates,
                key=lambda item: (
                    item[1].rectangle().top,
                    item[1].rectangle().left,
                    item[0].rectangle().top,
                    item[0].rectangle().left,
                ),
            )
            self._focus(window)
            try:
                control.click_input()
            except Exception:
                rect = control.rectangle()
                self.mouse.click(
                    coords=(
                        rect.left + rect.width() // 2,
                        rect.top + rect.height() // 2,
                    )
                )
            closed += 1
            quiet_since = None
            time.sleep(0.35)
        raise PddUiaError(f"拼多多广告弹窗关闭 {closed} 层后仍未进入稳定状态")

    @staticmethod
    def _sidebar_label_matches(name: str, text: str) -> bool:
        """Match a menu label while ignoring only its numeric badge."""

        compact_name = re.sub(r"\s+", "", str(name or ""))
        compact_text = re.sub(r"\s+", "", str(text or ""))
        if not compact_name or not compact_text:
            return False
        return bool(
            re.fullmatch(
                rf"{re.escape(compact_text)}(?:\d+\+?)?",
                compact_name,
            )
        )

    def _sidebar_action_candidate(
        self, control: Any
    ) -> tuple[Any, Any, int] | None:
        """Return (click target, nav wrapper, score) for a real side-nav item."""

        control_type = str(control.element_info.control_type or "")
        if control_type not in {"Hyperlink", "ListItem", "Button", "Text"}:
            return None

        action = control
        wrapper = control
        parent = control
        side_nav_found = False
        group_content_found = False
        for _depth in range(5):
            try:
                parent = parent.parent()
            except Exception:
                parent = None
            if parent is None:
                break
            parent_type = str(parent.element_info.control_type or "")
            parent_class = str(parent.element_info.class_name or "")
            if parent_type == "ListItem" and "nav-item" in parent_class:
                wrapper = parent
            if "nav-item-group-content" in parent_class:
                group_content_found = True
            if "side-nav" in parent_class:
                side_nav_found = True
                break
        if control_type == "ListItem" and "nav-item" in str(
            control.element_info.class_name or ""
        ):
            wrapper = control
        if not side_nav_found:
            return None

        left, top, right, bottom = self._rect(wrapper)
        if left < 0 or left > 360 or top < 80 or right <= left or bottom <= top:
            return None

        wrapper_type = str(wrapper.element_info.control_type or "")
        wrapper_class = str(wrapper.element_info.class_name or "")
        automation_id = str(wrapper.element_info.automation_id or "")
        score = 0
        if automation_id.startswith("__msfe__side-nav_item_"):
            score += 100
        if wrapper_type == "ListItem":
            score += 40
        if "nav-item" in wrapper_class:
            score += 30
        if group_content_found:
            score += 20
        if control_type == "Hyperlink":
            score += 10
        return action, wrapper, score

    def restore_work_order_list_after_aftersale(
        self, shop: dict[str, Any], handle: int
    ) -> dict[str, Any]:
        """Return to 工单管理 after a whole 售后工作台 scan.

        The aftersale connector may reuse the only PDD tab in a profile, so the
        original work-order list can have been replaced by the aftersale list.
        First reuse a retained work-order list tab when one exists; otherwise
        navigate the exact aftersale worker window back in place.  No window is
        closed here.
        """

        try:
            list_window = self.window_for_shop(shop, "list")
            return {
                "closed": 0,
                "navigated": 0,
                "list_ready": bool(list_window),
                "handled": True,
                "disposition": "work_order_list_already_ready",
            }
        except Exception:
            pass

        try:
            target = self.Desktop(backend="win32").window(handle=int(handle))
            current_url = self._current_url(target)
            parsed = urlparse(current_url)
            if (
                parsed.netloc.lower() != "mms.pinduoduo.com"
                or parsed.path.rstrip("/").lower() != "/aftersales/aftersale_list"
            ):
                raise PddUiaError(
                    f"售后工作台收尾窗口地址不符：{current_url or '<空>'}"
                )
        except PddUiaError:
            raise
        except Exception as exc:
            raise PddUiaError(
                f"{shop['name']} 无法确认售后工作台收尾窗口：{exc}"
            ) from exc

        return self._navigate_window_to_work_order_list(
            shop,
            target,
            disposition="aftersale_navigated_to_work_order_list",
        )

    def close_detail_tabs(self, shop: dict[str, Any]) -> dict[str, Any]:
        """Close all active PDD detail tabs for a shop and leave its list tab active.

        A Win32 Edge window can contain many tabs, but UIA exposes the address
        bar only for the active tab.  We therefore cycle tabs, close only tabs
        whose active URL is a PDD detail URL and whose page still identifies the
        configured shop, then restore the list tab as the active tab.
        """
        try:
            attempts = max(1, min(int(self.config.get("tab_cleanup_attempts", 20)), 40))
        except (TypeError, ValueError):
            attempts = 20
        try:
            wait_seconds = max(0.15, min(float(self.config.get("tab_switch_wait_seconds", 0.35)), 2.0))
        except (TypeError, ValueError):
            wait_seconds = 0.35

        windows = self._shop_windows(shop)
        if not windows:
            return {
                "closed": 0,
                "navigated": 0,
                "list_ready": False,
                "handled": False,
                "windows_checked": 0,
            }

        closed = 0
        navigated = 0
        list_ready = False
        for window in windows:
            seen: set[tuple[str, str]] = set()
            for _ in range(attempts + 1):
                try:
                    url = self._current_url(window)
                    title = window.window_text()
                    handle = int(window.handle)
                except Exception:
                    break
                shop_visible = shop["name"] in title or self._has_exact_name(handle, shop["name"])
                kind = classify_pdd_url(url)
                if shop_visible and kind == "detail":
                    cleanup = self.close_detail(window, shop)
                    closed += int(cleanup.get("closed") or 0)
                    navigated += int(cleanup.get("navigated") or 0)
                    list_ready = bool(cleanup.get("list_ready")) or list_ready
                    time.sleep(wait_seconds)
                    seen.clear()
                    continue
                if shop_visible and kind == "list":
                    list_ready = True
                signature = (url, title)
                if signature in seen:
                    break
                seen.add(signature)
                self._focus(window)
                self.keyboard.send_keys("^{TAB}")
                time.sleep(wait_seconds)

            if list_ready:
                self._activate_tab_kind(window, shop, "list")

        return {
            "closed": closed,
            "navigated": navigated,
            "list_ready": list_ready,
            "handled": list_ready and bool(closed or navigated),
            "windows_checked": len(windows),
        }

    def window_for_shop(self, shop: dict[str, Any], expected_kind: str) -> Any:
        candidates = self._shop_windows(shop)

        def matches_identity(window: Any) -> bool:
            handle = int(window.handle)
            if self._has_exact_name(handle, shop["name"]):
                return True
            # The list/login document tree can omit the shop label while the
            # active Edge title still contains the exact full shop name.  A
            # single titled window is safer than failing as window count 0.
            return expected_kind in {"list", "login"} and shop["name"] in window.window_text()

        matches = [
            window
            for window in candidates
            if classify_pdd_url(self._current_url(window)) == expected_kind
            and matches_identity(window)
        ]
        if expected_kind == "list" and not matches and len(candidates) == 1:
            self._activate_tab_kind(candidates[0], shop, expected_kind)
            matches = [
                window
                for window in self._shop_windows(shop)
                if classify_pdd_url(self._current_url(window)) == expected_kind
                and matches_identity(window)
            ]
        if len(matches) != 1:
            raise PddUiaError(
                f"{shop['name']} 的 {expected_kind} 窗口不唯一：{len(matches)}"
            )
        expected_surface = {
            "list": WORK_ORDER_LIST,
            "detail": WORK_ORDER_DETAIL,
            "login": LOGIN,
        }.get(expected_kind)
        if expected_surface:
            self.assert_page_contract(
                matches[0],
                shop,
                expected_surface,
                stage=f"解析{expected_kind}窗口",
            )
        return matches[0]

    def detail_window(self, shop_name: str, expected_url: str, order_no: str) -> Any:
        matches = []
        for window in self._windows():
            if self._current_url(window) != expected_url or shop_name not in window.window_text():
                continue
            handle = int(window.handle)
            if self._has_exact_name(handle, shop_name) and self._has_exact_name(handle, order_no):
                matches.append(window)
        if len(matches) != 1:
            raise PddUiaError(f"目标详情窗口不唯一：{len(matches)}")
        return matches[0]

    def _pending_filter(self, handle: int) -> Any:
        candidates = []
        for control in self._controls(handle, visible_only=True):
            try:
                if not re.fullmatch(r"待处理(?:\s*\d+\+?)?", self._name(control)):
                    continue
                left, top, right, bottom = self._rect(control)
                if 300 <= left <= 600 and top < 650:
                    candidates.append(control)
            except Exception:
                continue
        if len(candidates) != 1:
            raise PddUiaError(f"待处理筛选控件不唯一：{len(candidates)}")
        return candidates[0]

    @staticmethod
    def _pending_badge_from_name(name: str) -> int | None:
        """Return the numeric badge only when it belongs to ``待处理``."""

        compact = re.sub(r"\s+", "", str(name or ""))
        matched = re.fullmatch(r"待处理[（(]?(\d+)\+?[）)]?", compact)
        return int(matched.group(1)) if matched else None

    def _pending_filter_badge_count(self, handle: int, pending: Any) -> int | None:
        """Read the live count immediately following the pending filter.

        Chromium can expose the badge as part of the filter's accessible name,
        on one of its wrappers, or as a separate numeric text node.  Only
        controls on the same row and directly beside the pending filter are
        accepted; unrelated page numbers must never make an empty queue look
        non-empty.
        """

        self.last_pending_badge_exact = None
        pending_name = self._name(pending)
        direct = self._pending_badge_from_name(pending_name)
        if direct is not None:
            self.last_pending_badge_exact = "+" not in re.sub(r"\s+", "", pending_name)
            return direct

        ancestor = pending
        for _ in range(3):
            try:
                ancestor = ancestor.parent()
                count = self._pending_badge_from_name(self._name(ancestor))
            except Exception:
                break
            if count is not None:
                self.last_pending_badge_exact = "+" not in re.sub(
                    r"\s+", "", self._name(ancestor)
                )
                return count

        left, top, right, bottom = self._rect(pending)
        pending_center_y = (top + bottom) / 2
        candidates: list[tuple[float, int, bool]] = []
        for control in self._controls(handle, visible_only=True):
            try:
                name = self._name(control)
                count = self._pending_badge_from_name(name)
                c_left, c_top, c_right, c_bottom = self._rect(control)
                center_y = (c_top + c_bottom) / 2
                if count is not None:
                    # A wrapper carrying ``待处理 12`` normally contains the
                    # exact-name child and therefore overlaps its rectangle.
                    if (
                        c_left <= right
                        and c_right >= left
                        and abs(center_y - pending_center_y) <= 24
                    ):
                        candidates.append(
                            (0.0, count, "+" not in re.sub(r"\s+", "", name))
                        )
                    continue
                compact = re.sub(r"\s+", "", name)
                numeric = re.fullmatch(r"(\d+)\+?", compact)
                if not numeric:
                    continue
                horizontal_gap = c_left - right
                if -8 <= horizontal_gap <= 80 and abs(center_y - pending_center_y) <= 16:
                    candidates.append(
                        (
                            abs(horizontal_gap) + abs(center_y - pending_center_y),
                            int(numeric.group(1)),
                            "+" not in compact,
                        )
                    )
            except Exception:
                continue
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        self.last_pending_badge_exact = candidates[0][2]
        return candidates[0][1]

    @staticmethod
    def _pending_filter_selected(control: Any) -> bool:
        try:
            state = control.get_toggle_state()
            if state is not None:
                return int(state) == 1
        except Exception:
            pass
        try:
            return bool(control.is_selected())
        except Exception:
            pass
        try:
            parent_class = str(control.parent().element_info.class_name or "")
        except Exception:
            return False
        return "CAD_beastCardChecked" in parent_class

    def _query_total(self, handle: int) -> int | None:
        controls = self._controls(handle, visible_only=True)
        # Current PDD pagination renders the total as a ListItem such as
        # “共有 15 条”; older layouts exposed a separate “共查询到” marker.
        for control in controls:
            name = self._name(control)
            matched = re.search(r"共有\s*(\d+)\s*条", name)
            if matched:
                return int(matched.group(1))
        markers = [c for c in controls if self._name(c) == "共查询到"]
        for marker in markers:
            _, top, right, bottom = self._rect(marker)
            numbers = []
            for control in controls:
                name = self._name(control)
                if not name.isdigit():
                    continue
                left, ctop, _, cbottom = self._rect(control)
                if right <= left <= right + 80 and abs(ctop - top) <= 4 and abs(cbottom - bottom) <= 4:
                    numbers.append(int(name))
            if len(numbers) == 1:
                return numbers[0]
        empty_markers = {
            "暂无工单",
            "暂无相关工单",
            "暂无数据",
            "未查询到相关工单",
            "查询结果为空",
        }
        if any(self._name(c).strip() in empty_markers for c in controls):
            return 0
        return None

    def _pagination_state(self, handle: int) -> dict[str, Any]:
        """Read PDD's UIA pagination bar without relying on coordinates."""
        controls = self._controls(handle, visible_only=True)
        page_items = []
        active_page: int | None = None
        previous = []
        next_items = []
        total_text = ""
        for control in controls:
            try:
                control_type = str(control.element_info.control_type or "")
                class_name = str(control.element_info.class_name or "")
                name = self._name(control)
                if control_type != "ListItem":
                    continue
                if "PGT_totalText" in class_name:
                    total_text = name
                if "PGT_pagerItem" in class_name:
                    if name.isdigit():
                        page = int(name)
                        page_items.append((page, control))
                        if "PGT_pagerItemActive" in class_name:
                            active_page = page
                if "PGT_prev" in class_name:
                    previous.append(control)
                if "PGT_next" in class_name:
                    next_items.append(control)
            except Exception:
                continue
        if len(previous) > 1:
            raise PddUiaError(f"上一页分页控件不唯一：{len(previous)}")
        if len(next_items) > 1:
            raise PddUiaError(f"下一页分页控件不唯一：{len(next_items)}")
        total_match = re.search(r"(?:共有|共查询到)\s*(\d+)\s*条?", total_text)
        return {
            "total": int(total_match.group(1)) if total_match else self._query_total(handle),
            # Keep None while the pagination bar is between page renders;
            # treating that transient state as page 1 causes a false loop.
            "current_page": active_page if page_items else None,
            "page_items": page_items,
            "prev": previous[0] if previous else None,
            "next": next_items[0] if next_items else None,
            "prev_disabled": bool(previous and "PGT_disabled" in str(previous[0].element_info.class_name or "")),
            "next_disabled": bool(next_items and "PGT_disabled" in str(next_items[0].element_info.class_name or "")),
        }

    def _page_order_signature(self, handle: int) -> tuple[str, ...]:
        orders = []
        for control in self._controls(handle):
            try:
                if ORDER_RE.fullmatch(self._name(control)):
                    orders.append(self._name(control))
            except Exception:
                continue
        return tuple(orders)

    def _wait_for_page_change(
        self, handle: int, old_page: int, old_signature: tuple[str, ...]
    ) -> None:
        deadline = time.monotonic() + float(
            self.config.get("page_navigation_timeout_seconds", 10)
        )
        changed_signature: tuple[str, ...] | None = None
        changed_samples = 0
        pagination_reexposed = False
        while time.monotonic() < deadline:
            time.sleep(0.35)
            state = self._pagination_state(handle)
            signature = self._page_order_signature(handle)
            current_page = state.get("current_page")
            if current_page is None:
                if old_page < 0 and self._pager_reports_first_page(state):
                    return
                # PDD temporarily removes the pager from the UIA tree while
                # the list is rendering. Re-expose it at most once during this
                # page transition; repeated Ctrl+End can interrupt rendering.
                if not pagination_reexposed:
                    self._scroll_to_pagination(handle)
                    pagination_reexposed = True
                changed_signature = None
                changed_samples = 0
                continue
            if old_page < 0 and (current_page == 1 or state.get("prev_disabled")):
                return
            if current_page != old_page:
                return
            if signature != old_signature:
                if signature == changed_signature:
                    changed_samples += 1
                else:
                    changed_signature = signature
                    changed_samples = 1
                # During PDD's render the card DOM changes before the active
                # page class. Accept a changed signature only after two stable
                # samples so the old-page transient cannot cause a loop.
                if changed_samples >= 2:
                    settled = self._pagination_state(handle)
                    if old_page < 0 and self._pager_reports_first_page(settled):
                        return
                    if settled.get("current_page") not in {None, old_page}:
                        return
                    changed_signature = None
                    changed_samples = 0
            else:
                changed_signature = None
                changed_samples = 0
        raise PddUiaError("翻页后工单列表未稳定")

    def _scroll_to_pagination(self, handle: int) -> None:
        """Bring the bottom pagination bar into the UIA visible tree."""
        window = self.Desktop(backend="win32").window(handle=handle)
        self._focus(window)
        self.keyboard.send_keys("^{END}")
        time.sleep(0.45)

    def _scroll_to_top(self, handle: int) -> None:
        window = self.Desktop(backend="win32").window(handle=handle)
        self._focus(window)
        self.keyboard.send_keys("^{HOME}")
        time.sleep(0.45)

    @staticmethod
    def _pager_absent_single_page(state: dict[str, Any]) -> bool:
        """Return true when the list has no pager because it fits on page one."""
        return (
            not state.get("page_items")
            and state.get("prev") is None
            and state.get("next") is None
            and state.get("total") is not None
        )

    @classmethod
    def _pager_reports_first_page(cls, state: dict[str, Any]) -> bool:
        if state.get("current_page") == 1 or cls._pager_absent_single_page(state):
            return True
        return bool(state.get("prev_disabled"))

    @staticmethod
    def _card_identity(card: UiaCard) -> tuple[str, str, str]:
        """Identify one work order independently from the page it appeared on."""
        return (
            str(card.order_no),
            str(card.work_order_title),
            str(card.action_name),
        )

    def _click_page_item(self, handle: int, page_no: int) -> bool:
        state = self._pagination_state(handle)
        matches = [control for page, control in state["page_items"] if page == page_no]
        if len(matches) > 1:
            raise PddUiaError(f"页码 {page_no} 控件不唯一：{len(matches)}")
        if not matches:
            return False
        if state.get("current_page") == page_no:
            return True
        if state.get("current_page") is None:
            old_page = -1
        else:
            old_page = int(state["current_page"])
        old_signature = self._page_order_signature(handle)
        self._focus(self.Desktop(backend="win32").window(handle=handle))
        matches[0].click_input()
        self._wait_for_page_change(handle, old_page, old_signature)
        return True

    def _go_to_first_page(self, handle: int, *, ensure_pagination_visible: bool = True) -> None:
        if ensure_pagination_visible:
            self._scroll_to_pagination(handle)
        state = self._pagination_state(handle)
        if state.get("current_page") == 1:
            return
        if state.get("current_page") is None:
            if self._pager_absent_single_page(state):
                return
            self._scroll_to_pagination(handle)
            state = self._pagination_state(handle)
            if state.get("current_page") == 1 or self._pager_absent_single_page(state):
                return
            if state.get("current_page") is None:
                raise PddUiaError("分页栏未稳定，无法回到工单第一页")
        # PDD normally keeps page 1 visible. Prefer that semantic control;
        # fall back to repeatedly clicking the unique previous control.
        if self._click_page_item(handle, 1):
            return
        for _ in range(100):
            state = self._pagination_state(handle)
            if state.get("current_page") == 1:
                return
            if state.get("current_page") is None:
                if self._pager_absent_single_page(state):
                    return
                self._scroll_to_pagination(handle)
                continue
            previous = state["prev"]
            if previous is None or state["prev_disabled"]:
                raise PddUiaError("无法回到工单第一页")
            old_page = int(state["current_page"])
            old_signature = self._page_order_signature(handle)
            self._focus(self.Desktop(backend="win32").window(handle=handle))
            previous.click_input()
            self._wait_for_page_change(handle, old_page, old_signature)
        raise PddUiaError("回到工单第一页超过安全尝试次数")

    def _materialize_page_cards(
        self,
        handle: int,
        page_no: int,
    ) -> list[UiaCard]:
        """Read one page while forcing virtualized rows into the UIA tree.

        PDD can virtualize long pages. A single ``descendants()`` snapshot may
        then contain only the rows currently in the viewport. Incremental
        page-down scrolling accumulates every unique order row without using
        screen coordinates; the paginator is restored afterwards.
        """
        try:
            max_scrolls = max(
                0,
                min(int(self.config.get("page_materialize_max_scrolls", 12)), 40),
            )
        except (TypeError, ValueError):
            max_scrolls = 12
        try:
            wait_seconds = max(
                0.15,
                min(float(self.config.get("page_materialize_wait_seconds", 0.35)), 3.0),
            )
        except (TypeError, ValueError):
            wait_seconds = 0.35

        trace: dict[str, Any] = {
            "page_no": page_no,
            "rounds": [],
        }
        history = list(getattr(self, "last_pagination_trace", []))
        history.append(trace)
        self.last_pagination_trace = history[-20:]
        self._scroll_to_top(handle)
        collected: dict[tuple[str, str, str], UiaCard] = {}
        no_new_rounds = 0
        empty_rounds = 0
        for scroll_index in range(max_scrolls + 1):
            page_cards = self._cards(handle, page_no=page_no)
            trace["rounds"].append(
                {
                    "scroll_index": scroll_index,
                    "cards_read": len(page_cards),
                    "unique_cards_before": len(collected),
                }
            )
            if not page_cards:
                empty_rounds += 1
                if empty_rounds >= 2:
                    break
            else:
                empty_rounds = 0
                before = len(collected)
                for card in page_cards:
                    collected.setdefault(self._card_identity(card), card)
                if len(collected) == before:
                    no_new_rounds += 1
                else:
                    no_new_rounds = 0
                # PDD's displayed total is deliberately ignored. Human agents
                # can add or remove work orders while this page is being read.
                # One lower-viewport sample with no new cards is enough.  The
                # scheduled next cycle is the recovery path for rows that are
                # concurrently added or temporarily absent; repeatedly
                # page-down scrolling the same stable page only wastes time.
                if no_new_rounds >= 1:
                    trace["stop_reason"] = "one_round_without_new_cards"
                    break
            if scroll_index >= max_scrolls:
                break
            self._focus(self.Desktop(backend="win32").window(handle=handle))
            self.keyboard.send_keys("{PGDN}")
            time.sleep(wait_seconds)

        self._scroll_to_pagination(handle)
        if not collected:
            trace["result"] = "empty"
            raise PddUiaError(f"第 {page_no} 页未读取到工单卡片")
        cards = list(collected.values())
        for index, card in enumerate(cards):
            card.page_no = page_no
            card.page_index = index
        trace["result"] = "success"
        trace["unique_cards"] = len(cards)
        return cards

    def _order_filter_controls(self, handle: int) -> tuple[Any, Any]:
        controls = self._controls(handle, visible_only=True)
        edits = [
            control for control in controls
            if str(control.element_info.control_type or "") == "Edit"
            and self._name(control) == "请输入订单编号"
        ]
        buttons = [
            control for control in controls
            if str(control.element_info.control_type or "") == "Button"
            and self._name(control) == "查询"
        ]
        if len(edits) != 1 or len(buttons) != 1:
            raise PddUiaError(f"订单筛选控件不唯一：edit={len(edits)}, query={len(buttons)}")
        return edits[0], buttons[0]

    @staticmethod
    def _invoke_with_uia(control: Any) -> bool:
        """Invoke one semantic control (or its actionable ancestor) without coordinates."""

        current = control
        for _ in range(3):
            if current is None:
                break
            try:
                current.iface_invoke.Invoke()
                return True
            except Exception:
                try:
                    current = current.parent()
                except Exception:
                    break
        return False

    def _set_order_filter(
        self,
        handle: int,
        value: str,
        *,
        window: Any | None = None,
        shop: dict[str, Any] | None = None,
        popups_cleared: bool = False,
    ) -> None:
        if window is not None and not popups_cleared:
            # A late PDD ad can appear after the caller's first cleanup.  Clear
            # it at the action boundary, then discard every previously captured
            # wrapper and resolve the filter controls again.
            self._dismiss_known_pdd_overlays(window)
            self.assert_page_contract(
                window,
                shop,
                WORK_ORDER_LIST,
                stage="点击订单查询前",
                require_ready=True,
            )
        try:
            edit, query = self._order_filter_controls(handle)
        except PddUiaError:
            # A completed multi-page scan intentionally leaves pagination at
            # the bottom of the document.  The order filter is then present
            # but off-screen and excluded by visible-only UIA inspection.
            # Restore the top once before treating the controls as missing.
            self._scroll_to_top(handle)
            time.sleep(0.5)
            edit, query = self._order_filter_controls(handle)
        current = ""
        try:
            current = str(edit.get_value() or "").strip()
        except Exception:
            pass
        if current != value:
            edit.set_edit_text(value)
        if self._invoke_with_uia(query):
            return
        if window is not None:
            # Physical fallback is allowed once, but only after another fresh
            # cleanup/identity check and a complete UIA re-resolution.
            self._focus(window)
            self._dismiss_known_pdd_overlays(window)
            self.assert_page_contract(
                window,
                shop,
                WORK_ORDER_LIST,
                stage="订单查询物理点击兜底前",
                require_ready=True,
            )
            edit, query = self._order_filter_controls(handle)
            try:
                current = str(edit.get_value() or "").strip()
            except Exception:
                current = ""
            if current != value:
                edit.set_edit_text(value)
        query.click_input()

    def _cards(self, handle: int, page_no: int = 1) -> list[UiaCard]:
        controls = self._controls(handle)
        order_controls = []
        for control in controls:
            try:
                name = self._name(control)
                left, top, right, bottom = self._rect(control)
                # UIA exposes off-screen rows with negative Y values when the
                # list is scrolled to the bottom.  The old top>=680 guard
                # therefore dropped most cards.  The order-number column and
                # strict order format are the stable identity anchors.
                if ORDER_RE.fullmatch(name) and 250 <= left <= 600:
                    order_controls.append(control)
            except Exception:
                continue
        order_controls.sort(key=lambda item: self._rect(item)[1])
        cards: list[UiaCard] = []
        for index, order_control in enumerate(order_controls):
            order_no = self._name(order_control)
            order_rect = self._rect(order_control)
            top = order_rect[1]
            next_top = self._rect(order_controls[index + 1])[1] if index + 1 < len(order_controls) else top + 124
            problems = []
            actions = []
            for control in controls:
                try:
                    name = self._name(control)
                    left, ctop, right, bottom = self._rect(control)
                    if not (top < ctop < next_top):
                        continue
                    if name in {"立即处理", "立即查看"}:
                        actions.append(control)
                    elif left >= 360 and ctop <= top + 90 and name not in {"复制", "订单问题：", "待处理"}:
                        if name and not name.isdigit() and len(name) <= 60:
                            problems.append(control)
                except Exception:
                    continue
            problem = min(problems, key=lambda item: abs(self._rect(item)[1] - (top + 39)), default=None)
            action = min(actions, key=lambda item: abs(self._rect(item)[1] - (top + 57)), default=None)
            if not problem or not action:
                continue
            cards.append(
                UiaCard(
                    order_no=order_no,
                    work_order_title=self._name(problem),
                    action_name=self._name(action),
                    order_rect=order_rect,
                    action_rect=self._rect(action),
                    page_no=page_no,
                    page_index=index,
                )
            )
        return cards

    def _collect_all_pages(
        self, handle: int, *, single_page_hint: bool = False
    ) -> tuple[int, list[UiaCard]]:
        """Collect every pending page in order, then restore page one.

        Retries are scoped to the page that failed to materialize. This avoids
        repeatedly rescanning already collected pages when a long second page
        is still being rendered by PDD.
        """
        if single_page_hint:
            cards = [
                card
                for card in self._materialize_page_cards(handle, 1)
                if card.action_name == "立即处理"
            ]
            self._scroll_to_top(handle)
            unique: dict[tuple[str, str, str], UiaCard] = {}
            for card in cards:
                unique.setdefault(self._card_identity(card), card)
            discovered = list(unique.values())
            self.last_collection_summary = {
                "discovered_cards": len(discovered),
                "pages_scanned": 1,
                "single_pass": True,
                "single_page_hint": True,
            }
            return len(discovered), discovered

        # _go_to_first_page exposes the pager once; avoid a duplicate Ctrl+End
        # before the first page is materialized.
        self._go_to_first_page(handle, ensure_pagination_visible=True)
        initial_state = self._pagination_state(handle)
        # An empty pending queue is a valid result.  PDD renders no work-order
        # cards (and sometimes no pager) in this state, so do not send page one
        # through the card retry path and report a false pagination failure.
        if initial_state.get("total") == 0:
            self.last_collection_summary = {
                "discovered_cards": 0,
                "pages_scanned": 0,
                "single_pass": True,
            }
            self._scroll_to_top(handle)
            return 0, []
        try:
            page_retry_count = max(
                1,
                min(int(self.config.get("page_retry_per_page", 2)), 5),
            )
        except (TypeError, ValueError):
            page_retry_count = 2

        all_cards: list[UiaCard] = []
        seen_pages: set[int] = set()
        for _ in range(100):
            # _go_to_first_page(), _materialize_page_cards() and
            # _wait_for_page_change() all leave the pager visible.  Scrolling
            # to it again here cost 450 ms per page and could invalidate UIA
            # controls during a redraw.
            state = self._pagination_state(handle)
            current_page = state.get("current_page")
            if current_page is None:
                if not seen_pages and self._pager_absent_single_page(state):
                    current_page = 1
                elif not seen_pages and any(
                    page == 1 for page, _ in state.get("page_items", [])
                ):
                    # _go_to_first_page has just targeted page one. Some PDD
                    # builds omit the active-page class even though the page
                    # item and disabled previous control are present.
                    current_page = 1
                else:
                    raise PddUiaError("分页栏未稳定，无法读取当前页")
            page_no = int(current_page)
            if page_no in seen_pages:
                raise PddUiaError(f"分页循环未前进，当前页：{page_no}")
            seen_pages.add(page_no)
            page_cards: list[UiaCard] | None = None
            page_error = ""
            for page_attempt in range(1, page_retry_count + 1):
                try:
                    page_cards = self._materialize_page_cards(
                        handle,
                        page_no,
                    )
                    if page_cards:
                        break
                    page_error = f"第 {page_no} 页读取到 0 条工单"
                except PddUiaError as exc:
                    page_error = str(exc)
                if page_attempt < page_retry_count:
                    # The failed materialization attempt already completed one
                    # top-to-bottom journey and left the pager visible. Retry
                    # this page directly; never revisit an earlier page.
                    time.sleep(0.5)
            if page_cards is None:
                raise PddUiaError(
                    f"分页第 {page_no} 页重试 {page_retry_count} 次仍失败：{page_error}"
                )
            all_cards.extend(
                card for card in page_cards if card.action_name == "立即处理"
            )

            # The materializer leaves the pager visible. Reacquire fresh UIA
            # controls, but do not scroll again on the same page.
            state = self._pagination_state(handle)
            next_control = state.get("next")
            if next_control is None or state.get("next_disabled"):
                break
            old_signature = self._page_order_signature(handle)
            self._focus(self.Desktop(backend="win32").window(handle=handle))
            next_control.click_input()
            self._wait_for_page_change(handle, page_no, old_signature)
        else:
            raise PddUiaError("翻页超过 100 页安全上限")

        self._go_to_first_page(handle, ensure_pagination_visible=False)
        self._scroll_to_top(handle)
        unique: dict[tuple[str, str, str], UiaCard] = {}
        for card in sorted(all_cards, key=lambda item: (item.page_no, item.page_index)):
            unique.setdefault(self._card_identity(card), card)
        # The displayed total is not read as a collection invariant. Human
        # customer-service actions can move rows between pages while this
        # single pass is running; page-independent identity prevents a moved
        # row from entering the same cycle twice. Missed rows are intentionally
        # left for the next scheduled cycle instead of rescanning old pages.
        discovered = list(unique.values())
        self.last_collection_summary = {
            "discovered_cards": len(discovered),
            "pages_scanned": len(seen_pages),
            "single_pass": True,
        }
        return len(discovered), discovered

    def refresh_pending_list(self, shop: dict[str, Any]) -> UiaListResult:
        # A refresh can expose the PDD login page instead of the list.  Recover
        # that page before resolving the list window so a stale login window is
        # never mistaken for a missing/duplicate shop window.
        candidates = self._shop_windows(shop)
        if candidates and not any(
            classify_pdd_url(self._current_url(window)) in {"list", "login"}
            for window in candidates
        ):
            self.restore_shop_list_anchor(shop)
        self.ensure_shop_list(shop)
        window = self.window_for_shop(shop, "list")
        self.assert_page_contract(
            window,
            shop,
            WORK_ORDER_LIST,
            stage="工单管理刷新前",
        )
        self._scroll_to_top(int(window.handle))
        self._focus(window)
        self.keyboard.send_keys("^r")
        self._dismiss_known_pdd_overlays(window)
        deadline = time.monotonic() + float(self.config.get("refresh_timeout_seconds", 20))
        clicked = False
        order_filter_cleared = False
        login_recovery_attempted = False
        collection: tuple[int, list[UiaCard]] | None = None
        last_collection_error = ""
        filter_unselected_samples = 0
        while time.monotonic() < deadline:
            time.sleep(0.7)
            current_url = self._current_url(window)
            if classify_pdd_url(current_url) == "login":
                if login_recovery_attempted:
                    continue
                # Some PDD sessions redirect to /login only after Ctrl+R.
                # Recover that exact shop window in-place, then resume the
                # same refresh cycle instead of failing as "list window 0".
                login_recovery_attempted = True
                self.ensure_shop_list(shop)
                continue
            if classify_pdd_url(current_url) != "list":
                continue
            handle = int(window.handle)
            if not (
                self._has_exact_name(handle, shop["name"])
                or shop["name"] in window.window_text()
            ):
                continue
            try:
                pending = self._pending_filter(handle)
            except Exception:
                continue
            pending_badge_count = self._pending_filter_badge_count(handle, pending)
            if pending_badge_count is None or pending_badge_count <= 0:
                # No badge means no pending queue on this PDD surface. Return
                # immediately before clicking, clearing filters or scrolling.
                self.last_collection_summary = {
                    "discovered_cards": 0,
                    "pages_scanned": 0,
                    "single_pass": True,
                    "pending_badge_count": pending_badge_count,
                    "skip_reason": (
                        "pending_badge_missing"
                        if pending_badge_count is None
                        else "pending_badge_zero"
                    ),
                }
                return UiaListResult(
                    handle,
                    window.window_text(),
                    self._current_url(window),
                    0,
                    [],
                )
            selected = self._pending_filter_selected(pending)
            if not selected and not clicked:
                self._focus(window)
                pending.click_input()
                clicked = True
                filter_unselected_samples = 0
                collection = None
                continue
            if not selected:
                filter_unselected_samples += 1
                if filter_unselected_samples >= 2:
                    raise PddUiaError("待处理筛选在分页采集期间未保持选中")
                continue
            filter_unselected_samples = 0
            if not order_filter_cleared:
                try:
                    edit, _ = self._order_filter_controls(handle)
                    current = str(edit.get_value() or "").strip()
                    if current:
                        self._set_order_filter(
                            handle,
                            "",
                            window=window,
                            shop=shop,
                        )
                        collection = None
                        time.sleep(0.7)
                    order_filter_cleared = True
                except Exception:
                    continue
            if collection is None:
                try:
                    self.assert_page_contract(
                        window,
                        shop,
                        WORK_ORDER_LIST,
                        stage="工单管理分页采集前",
                        require_ready=True,
                    )
                    collection = self._collect_all_pages(
                        handle,
                        single_page_hint=bool(
                            getattr(self, "last_pending_badge_exact", None) is True
                            and 0 < pending_badge_count <= 8
                        ),
                    )
                    self.last_collection_summary["pending_badge_count"] = pending_badge_count
                except PddUiaError as exc:
                    last_collection_error = str(exc)
                    raise PddUiaError(f"{shop['name']} 分页采集失败：{exc}") from exc
            total, cards = collection
            # _collect_all_pages already retries each page and restores page
            # one. Do not compare against PDD's displayed total: it can
            # change while a human agent handles a work order. Re-reading
            # page one here
            # was redundant and could consume the refresh timeout on larger
            # lists, leaving a successful collection looking like a scan loop.
            self.assert_page_contract(
                window,
                shop,
                WORK_ORDER_LIST,
                stage="工单管理分页采集完成",
                require_ready=True,
            )
            return UiaListResult(handle, window.window_text(), self._current_url(window), total, cards)
        suffix = f"：{last_collection_error}" if last_collection_error else ""
        raise PddUiaError(f"{shop['name']} 刷新并勾选待处理后未稳定{suffix}")

    def screenshot(self, handle: int, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.Desktop(backend="win32").window(handle=handle).capture_as_image().save(path)

    def _open_detail_from_list(
        self, shop: dict[str, Any], order_no: str, window: Any
    ) -> tuple[Any, str]:
        """Locate an order from an already prepared list without rescanning it."""
        handle = int(window.handle)
        self._focus(window)
        self._dismiss_known_pdd_overlays(window)
        self._set_order_filter(
            handle,
            order_no,
            window=window,
            shop=shop,
            # open_detail has just completed the same full modal-local scan.
            # Repeating its quiet-period wait here added latency without
            # observing a newer page state.  A second scan remains at the
            # actual “立即处理” boundary after the search result has rendered.
            popups_cleared=True,
        )
        runtime_config = getattr(self, "config", {})
        try:
            filter_timeout = max(
                4.0,
                min(float(runtime_config.get("order_filter_timeout_seconds", 6)), 20.0),
            )
        except (TypeError, ValueError):
            filter_timeout = 6.0
        try:
            stable_missing_required = max(
                2,
                min(int(runtime_config.get("order_filter_stable_missing_samples", 2)), 8),
            )
        except (TypeError, ValueError):
            stable_missing_required = 2
        deadline = time.monotonic() + filter_timeout
        filtered_cards: list[UiaCard] = []
        last_total: int | None = None
        zero_samples = 0
        stable_missing_samples = 0
        last_missing_signature: tuple[str, ...] | None = None
        target_ready_samples = 0
        while time.monotonic() < deadline:
            time.sleep(
                max(
                    0.15,
                    min(
                        float(runtime_config.get("order_filter_poll_seconds", 0.3)),
                        1.0,
                    ),
                )
            )
            filtered_cards = self._cards(handle)
            last_total = self._query_total(handle)
            if last_total == 0:
                zero_samples += 1
                if zero_samples >= 2:
                    raise PddOrderNotFoundError(f"订单 {order_no} 在当前待处理列表中不存在")
            else:
                zero_samples = 0
            matching_cards = [card for card in filtered_cards if card.order_no == order_no]
            if filtered_cards and not matching_cards:
                missing_signature = tuple(sorted(card.order_no for card in filtered_cards))
                if missing_signature == last_missing_signature:
                    stable_missing_samples += 1
                else:
                    last_missing_signature = missing_signature
                    stable_missing_samples = 1
                if stable_missing_samples >= stable_missing_required:
                    raise PddOrderNotFoundError(
                        f"订单 {order_no} 在当前待处理列表中不存在"
                    )
            else:
                stable_missing_samples = 0
                last_missing_signature = None
            exact_target_ready = (
                len(filtered_cards) == 1
                and filtered_cards[0].order_no == order_no
            )
            target_ready_samples = target_ready_samples + 1 if exact_target_ready else 0
            if exact_target_ready and (last_total == 1 or target_ready_samples >= 2):
                break
        if last_total == 0 or not filtered_cards:
            raise PddOrderNotFoundError(f"订单 {order_no} 在当前待处理列表中不存在")
        def resolve_immediate_action() -> Any:
            fresh_cards = [
                card for card in self._cards(handle) if card.order_no == order_no
            ]
            if not fresh_cards:
                raise PddOrderNotFoundError(
                    f"订单 {order_no} 在当前待处理列表中不存在"
                )
            if len(fresh_cards) != 1:
                raise PddUiaError(
                    f"订单 {order_no} 查询结果不唯一：{len(fresh_cards)}"
                )
            card = fresh_cards[0]
            if card.action_name == "立即查看":
                raise PddAlreadyProcessedError(f"订单 {order_no} 已处理，直接跳过")
            if card.action_name != "立即处理":
                raise PddUiaError(
                    f"订单 {order_no} 的处理控件不是立即处理：{card.action_name}"
                )
            actions = []
            for control in self._controls(handle):
                try:
                    if (
                        self._name(control) == "立即处理"
                        and self._rect(control) == card.action_rect
                    ):
                        actions.append(control)
                except Exception:
                    continue
            if len(actions) != 1:
                raise PddUiaError("立即处理 UIA 控件在点击前发生变化")
            return actions[0]

        # Ads can be injected after the query completes.  Clear them now and
        # resolve both the exact row and its action from a fresh UIA tree.
        self._dismiss_known_pdd_overlays(window)
        self.assert_page_contract(
            window,
            shop,
            WORK_ORDER_LIST,
            stage="点击立即处理前",
            require_ready=True,
        )
        action = resolve_immediate_action()
        if not self._invoke_with_uia(action):
            self._focus(window)
            self._dismiss_known_pdd_overlays(window)
            self.assert_page_contract(
                window,
                shop,
                WORK_ORDER_LIST,
                stage="立即处理物理点击兜底前",
                require_ready=True,
            )
            resolve_immediate_action().click_input()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            time.sleep(
                max(
                    0.15,
                    min(
                        float(runtime_config.get("detail_open_poll_seconds", 0.35)),
                        1.0,
                    ),
                )
            )
            url = self._current_url(window)
            if classify_pdd_url(url) == "detail" and self._has_exact_name(handle, order_no):
                self.assert_page_contract(
                    window,
                    shop,
                    WORK_ORDER_DETAIL,
                    order_no=order_no,
                    stage="工单详情打开完成",
                )
                return window, url
        try:
            if classify_pdd_url(self._current_url(window)) == "detail":
                self.close_detail(window, shop)
        except Exception:
            pass
        raise PddUiaError("点击立即处理后详情页未稳定")

    def open_detail(self, shop: dict[str, Any], order_no: str) -> tuple[Any, str]:
        """Open one order, reusing the current list tab whenever possible."""
        try:
            window = self.window_for_shop(shop, "list")
        except PddUiaError:
            snapshot = self.refresh_pending_list(shop)
            window = self.Desktop(backend="win32").window(handle=snapshot.handle)
        try:
            return self._open_detail_from_list(shop, order_no, window)
        except PddUiaError as exc:
            if "订单筛选控件不唯一" not in str(exc):
                raise
            # A list window can exist while its document is stale or still at
            # a layout that exposes no filter controls. Refresh and restore
            # the pending filter once; business skips such as not-found or
            # already-processed are deliberately not retried here.
            snapshot = self.refresh_pending_list(shop)
            window = self.Desktop(backend="win32").window(handle=snapshot.handle)
            return self._open_detail_from_list(shop, order_no, window)

    @staticmethod
    def _select_latest_logistics_text(candidates: list[tuple[str, int]]) -> str:
        """Select the actual latest forwarding update from a PDD detail page.

        PDD can append a service annotation below a forwarding update.  The
        selection must therefore use the forwarding phrase before considering
        a fallback city label, rather than simply taking the bottom-most
        bracketed text control.
        """
        normalized = [
            (str(text or "").strip(), int(top))
            for text, top in candidates
            if str(text or "").strip()
        ]
        forwarding_phrase = "\u5feb\u4ef6\u5df2\u53d1\u5f80"
        movement_phrases = (
            forwarding_phrase,
            "\u60a8\u7684\u5feb\u4ef6\u79bb\u5f00",
            "\u5feb\u4ef6\u79bb\u5f00",
            "\u5df2\u79bb\u5f00",
            "\u5feb\u4ef6\u5df2\u5230\u8fbe",
        )
        forwarding = [
            item for item in normalized if any(phrase in item[0] for phrase in movement_phrases)
        ]
        bracketed_forwarding = [
            item
            for item in forwarding
            if re.search(r"\u3010[^\u3011]+\u3011\s*\u5feb\u4ef6\u5df2\u53d1\u5f80", item[0])
        ]
        if bracketed_forwarding:
            return max(bracketed_forwarding, key=lambda item: item[1])[0]
        if forwarding:
            return max(forwarding, key=lambda item: item[1])[0]

        # A degraded UIA tree can split the full line.  Do not use arbitrary
        # brackets such as a customer-service hotline as a fallback.
        city_label = re.compile(r"\u3010\s*[^\u3011]+\u5e02\s*\u3011")
        city_candidates = [item for item in normalized if city_label.search(item[0])]
        return max(city_candidates, key=lambda item: item[1])[0] if city_candidates else ""

    def read_detail(self, handle: int, order_no: str) -> dict[str, Any]:
        window = self.Desktop(backend="win32").window(handle=handle)
        self.assert_page_contract(
            window,
            None,
            WORK_ORDER_DETAIL,
            order_no=order_no,
            stage="读取工单详情",
            handle_override=handle,
        )
        controls = self._controls(handle)
        if sum(self._name(c) == order_no for c in controls) != 1:
            raise PddUiaError("详情页订单号不唯一")
        # A cancelled-after-refund work order can retain both the current
        # outbound waybill and a historical/return waybill.  Bind the read to
        # the active “物流信息” row instead of requiring every numeric string
        # on the detail page to be unique.
        tracking_candidates = self._detail_tracking_candidates(controls, order_no)
        if not tracking_candidates:
            raise PddUiaError("详情页物流单号为空")
        _tracking_control, tracking_no, tracking_rect = min(
            tracking_candidates, key=lambda item: (item[2][1], item[2][0])
        )
        carrier = self._detail_carrier(controls, tracking_rect)
        if not carrier:
            raise PddUiaError("详情页快递公司为空")
        latest_candidates = [
            (self._name(control), self._rect(control)[1])
            for control in controls
            if any(
                phrase in self._name(control)
                for phrase in ("快件已发往", "您的快件离开", "快件离开", "已离开", "快件已到达")
            )
            or re.search(r"【[^】]+】", self._name(control))
        ]
        latest_logistics_text = self._select_latest_logistics_text(latest_candidates)
        amount = 0.0
        labels = [c for c in controls if self._name(c).startswith("退款金额：")]
        for label in labels:
            _, top, right, _ = self._rect(label)
            values = []
            for control in controls:
                name = self._name(control).replace("¥", "").strip()
                left, ctop, _, _ = self._rect(control)
                if left >= right - 5 and abs(ctop - top) <= 4 and re.fullmatch(r"\d+(?:\.\d{1,2})?", name):
                    values.append(float(name))
            if len(values) == 1:
                amount = values[0]
                break
        return {
            "order_no": order_no,
            "tracking_no": tracking_no,
            "carrier": carrier,
            "latest_logistics_text": latest_logistics_text,
            "refund_amount": amount,
        }

    def _exact_controls(self, controls: list[Any], text: str, control_type: str = "") -> list[Any]:
        matches = []
        for control in controls:
            try:
                if self._name(control) != text:
                    continue
                actual_type = str(control.element_info.control_type or "")
                if control_type and actual_type != control_type:
                    continue
                matches.append(control)
            except Exception:
                continue
        return matches

    def _associated_option_radios(self, controls: list[Any], label: Any, text: str) -> list[Any]:
        """Return the one radio control rendered beside a PDD option label."""
        try:
            label_rect = label.rectangle()
            return [
                item
                for item in controls
                if str(item.element_info.control_type or "") == "RadioButton"
                and text in self._name(item)
                and abs(item.rectangle().top - label_rect.top)
                <= max(6, label_rect.height())
            ]
        except Exception:
            return []

    def _selectable_text_matches(self, controls: list[Any], matches: list[Any], text: str, control_type: str) -> list[Any]:
        """Discard historical progress text when the same option label repeats.

        PDD keeps a service-progress line such as “消费者撤销售后申请”
        below the live radio option.  Only the live label has an adjacent
        RadioButton, which gives a durable UIA discriminator without using a
        screen coordinate.
        """
        if control_type != "Text" or len(matches) <= 1:
            return matches
        selectable = [
            label
            for label in matches
            if len(self._associated_option_radios(controls, label, text)) == 1
        ]
        return selectable if selectable else matches

    def inspect_exact(self, handle: int, text: str, control_type: str = "") -> dict[str, Any]:
        # PDD's RadioButton itself reports ``is_visible=False`` even when
        # its label is on screen.  Keep candidate text labels visible, but
        # retain the full tree to associate that label with its radio sibling.
        controls = self._controls(handle, visible_only=False)
        visible_controls = []
        for control in controls:
            try:
                if control.is_visible():
                    visible_controls.append(control)
            except Exception:
                continue
        matches = self._exact_controls(visible_controls, text, control_type)
        matches = self._selectable_text_matches(controls, matches, text, control_type)
        if len(matches) != 1:
            return {"ready": False, "reason": "target_not_unique", "count": len(matches)}
        control = matches[0]
        return {"ready": True, "count": 1, "text": text, "control_type": str(control.element_info.control_type or ""), "rect": self._rect(control)}

    def click_exact(self, handle: int, text: str, control_type: str = "") -> dict[str, Any]:
        inspected = self.inspect_exact(handle, text, control_type)
        if not inspected.get("ready"):
            raise PddUiaError(f"UIA 元素无法唯一点击：{text} {inspected}")
        controls = self._controls(handle, visible_only=False)
        visible_controls = []
        for control in controls:
            try:
                if control.is_visible():
                    visible_controls.append(control)
            except Exception:
                continue
        matches = self._exact_controls(visible_controls, text, control_type)
        matches = self._selectable_text_matches(controls, matches, text, control_type)
        if len(matches) != 1:
            raise PddUiaError(f"UIA 元素无法唯一点击：{text} count={len(matches)}")
        target = matches[0]
        click_target = "direct"
        # PDD renders radio options as an accessible Text label next to a
        # separate RadioButton.  Clicking the Text is accepted by UIA but
        # does not change the selection, so bind the label to its unique
        # sibling radio control before submitting the form.
        if control_type == "Text":
            radios = self._associated_option_radios(controls, target, text)
            if len(radios) == 1:
                target = radios[0]
                click_target = "associated_radio_button"
        target.click_input()
        return {**inspected, "executed": True, "engine": "edge_uia", "click_target": click_target}

    def detail_state(self, handle: int, order_no: str, work_order_id: str) -> dict[str, Any]:
        window = self.Desktop(backend="win32").window(handle=handle)
        self.assert_page_contract(
            window,
            None,
            WORK_ORDER_DETAIL,
            order_no=order_no,
            stage="核验工单详情状态",
            handle_override=handle,
        )
        url = self._current_url(window)
        controls = self._controls(handle)
        names = []
        for control in controls:
            try:
                names.append(self._name(control))
            except Exception:
                # A page can replace an individual UIA node while it is being
                # inspected.  The rest of the detail tree is still usable.
                continue
        # PDD's current detail page exposes a few status labels as adjacent
        # UIA Text nodes (for example "【" + "已完结" + "】").  Keep exact-name
        # checks where possible, while also recognising those split labels.
        compact_text = "".join(names)

        def has_text(value: str) -> bool:
            return value in names or value in compact_text

        visible_forms = int(
            any(
                self._name(c) in {"提交", "提交并完结"} and c.is_visible()
                for c in controls
            )
        )
        actual_id = str((parse_qs(urlparse(url).query).get("id") or [""])[0])
        return {
            "work_order_id": actual_id,
            "work_order_id_matches": actual_id == work_order_id,
            "order_matches": names.count(order_no) == 1,
            "completed": has_text("【已完结】") or has_text("已完结"),
            "refund_success": has_text("退款成功"),
            "refund_processing": has_text("退款中"),
            "merchant_agreed": (
                has_text("商家选择同意退款")
                or has_text("客服已同意退款")
                or (not visible_forms and has_text("同意消费者退款申请"))
            ),
            # For a non-eligible in-transit refund, PDD records the merchant
            # action as a progress item and then keeps a follow-up page open
            # for the consumer.  It is not a browser failure: the exact
            # progress label below proves the requested withdrawal was
            # submitted and must not be sent again.
            "withdraw_requested": "\u6d88\u8d39\u8005\u64a4\u9500" in names,
            "withdraw_followup_required": has_text("消费者未撤销售后申请提示"),
            "has_unhanded": has_text("未交给快递"),
            "has_agree_refund": has_text("同意退款") or has_text("同意消费者退款申请"),
            "has_agreed_consensus": has_text("已协商一致"),
            "visible_forms": visible_forms,
            "engine": "edge_uia",
        }

    def close_detail(
        self, window: Any, shop: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Safely dispose of a detail page while preserving a list anchor.

        Ctrl+W is allowed only after a verified list tab/window for the same
        shop has been found.  If no list anchor exists, reuse the current tab
        by navigating it back to the allowlisted work-order list instead.
        """
        current_url = self._current_url(window)
        if classify_pdd_url(current_url) != "detail":
            return {
                "closed": 0,
                "navigated": 0,
                "list_ready": classify_pdd_url(current_url) == "list",
                "handled": classify_pdd_url(current_url) == "list",
                "disposition": "not_a_detail",
            }
        if not shop or not str(shop.get("name") or "").strip():
            raise PddUiaError("关闭详情页前缺少店铺身份，已禁止 Ctrl+W")

        original_handle = int(window.handle)
        detail_url = current_url
        try:
            self.window_for_shop(shop, "list")
        except Exception:
            return self.restore_shop_list_anchor(shop, preferred_window=window)

        try:
            detail_window = self.Desktop(backend="win32").window(handle=original_handle)
            if self._current_url(detail_window) != detail_url and not self._activate_tab_url(
                detail_window, shop, detail_url
            ):
                # The exact detail is already gone and a verified list exists.
                self.window_for_shop(shop, "list")
                return {
                    "closed": 0,
                    "navigated": 0,
                    "list_ready": True,
                    "handled": True,
                    "disposition": "detail_missing_list_ready",
                }
        except Exception as exc:
            raise PddUiaError(f"{shop['name']} 无法重新激活待关闭详情页：{exc}") from exc

        self._focus(detail_window)
        self.keyboard.send_keys("^w")
        time.sleep(
            max(
                0.15,
                min(float(self.config.get("tab_close_settle_seconds", 0.3)), 1.0),
            )
        )
        # Closing a detail tab normally exposes the already-verified list tab
        # in the same Edge window.  Confirm that active surface directly;
        # enumerating and cycling every tab again is only needed as a fallback.
        try:
            if (
                classify_pdd_url(self._current_url(detail_window)) == "list"
                and (
                    shop["name"] in detail_window.window_text()
                    or self._has_exact_name(int(detail_window.handle), shop["name"])
                )
            ):
                self.assert_page_contract(
                    detail_window,
                    shop,
                    WORK_ORDER_LIST,
                    stage="工单详情关闭后列表恢复",
                )
                return {
                    "closed": 1,
                    "navigated": 0,
                    "list_ready": True,
                    "handled": True,
                    "disposition": "detail_tab_closed_list_active",
                }
        except Exception:
            pass
        try:
            list_window = self.window_for_shop(shop, "list")
        except Exception as exc:
            raise PddUiaError(
                f"{shop['name']} 详情页已关闭但列表页未能恢复：{exc}"
            ) from exc
        return {
            "closed": 1,
            "navigated": 0,
            "list_ready": bool(list_window),
            "handled": True,
            "disposition": "detail_tab_closed",
        }
