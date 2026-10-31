from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from .aftersale_workbench import (
    AGREE_RECALL_AFTER_REFUND,
    AGREE_RETURN_AFTER_REFUND,
    AGREE_REFUND,
    EXTEND_RECEIPT,
    REJECT_REFUND,
)
from .edge_uia import (
    ORDER_RE,
    TRACKING_RE,
    PddEdgeUiaSession,
    PddOrderNotFoundError,
    PddSidebarNavigationError,
    PddUiaError,
)
from .pdd_page_identity import (
    AFTERSALE_DETAIL,
    AFTERSALE_LIST,
    WORK_ORDER_DETAIL,
    WORK_ORDER_LIST,
    classify_pdd_surface,
)


AFTERSALE_LIST_URL = (
    "https://mms.pinduoduo.com/aftersales/aftersale_list?msfrom=mms_sidenav"
)
AFTERSALE_LIST_PATH = "/aftersales/aftersale_list"
AFTERSALE_DETAIL_PATH = "/aftersales-ssr/detail"
AFTERSALE_SETUP_PATH = "/aftersales/setup"
AMOUNT_RE = re.compile(r"^¥?\s*(\d+(?:\.\d{1,2})?)$")
AFTERSALE_NO_RE = re.compile(
    r"(?:售后编号|售后单号|售后ID)[：:\s#]*([A-Za-z0-9_-]{6,64})",
    re.I,
)
AFTERSALE_QUERY_ID_RE = re.compile(
    r"[?&](?:id|afterSaleId|aftersaleId|refundId)=([A-Za-z0-9_-]{6,64})",
    re.I,
)
DATETIME_RE = re.compile(
    r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)"
)
LOGISTICS_TIMESTAMP_RE = re.compile(
    r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?"
)
LOGISTICS_NON_EVENT_TEXTS = {
    "发货物流公司:",
    "发货物流公司：",
    "发货快递单号:",
    "发货快递单号：",
    "查看全部",
    "去修改物流信息",
}
AFTERSALE_STATUS_NAMES = (
    "待商家处理",
    "待买家处理",
    "待平台处理",
    "平台处理中",
    "待买家发货",
    "待商家收货",
    "等待买家退货",
    "待快递退回后退款",
    "商家处理中",
    "买家已发货",
    "退货中",
    "已同意退款",
    "退款成功",
    "退款关闭",
    "售后关闭",
    "已拒绝退款",
    "已完成",
)

# PDD currently uses both words for the same merchant-side pending state.
# Keep one canonical value in task facts/terminal checks while accepting the
# wording that is actually rendered in headings such as
# “买家申请退款，待卖家处理”.
AFTERSALE_STATUS_ALIASES = {
    "待卖家处理": "待商家处理",
}

AFTERSALE_ACTION_TEXT = {
    AGREE_REFUND: "同意退款",
    AGREE_RETURN_AFTER_REFUND: "同意快递退回后退款",
    AGREE_RECALL_AFTER_REFUND: "同意召回后退款",
    REJECT_REFUND: "驳回退款",
}
ACTIONABLE_CONTROL_TYPES = {"Button", "Hyperlink", "RadioButton", "CheckBox"}
REJECT_COOLDOWN_REASON_CODE = "PDD_REJECT_COOLDOWN"
REJECT_NEGOTIATION_COOLDOWN_REASON_CODE = "PDD_REJECT_NEGOTIATION_COOLDOWN"
REJECT_COOLDOWN_SECONDS = 30 * 60
DEFAULT_DIRECT_REJECT_DESCRIPTION = (
    "您好，退货请申请【退货退款】，原因选“7天无理由退货”或“退运费”，"
    "请勿随意选择理由以免影响退款进度和信用评级，谢谢，祝您生活愉快~"
)


@dataclass(slots=True)
class AftersaleCard:
    order_no: str
    refund_type: str
    shipping_status: str = ""
    refund_reason: str = ""
    refund_amount: float = 0.0
    aftersale_no: str = ""
    status: str = ""
    apply_time: str = ""
    urgency_text: str = ""
    page_no: int = 1
    page_index: int = 0


@dataclass(slots=True)
class AftersaleListResult:
    handle: int
    title: str
    url: str
    cards: list[AftersaleCard]

    @property
    def total(self) -> int:
        return len(self.cards)


@dataclass(slots=True)
class _StrictModalScope:
    """One exact UIA modal root and its stable identity.

    A screen rectangle is intentionally only metadata.  Chromium keeps
    background DOM nodes in the UIA tree while a modal covers them, and those
    nodes can still report ``is_visible()``.  Every scoped lookup therefore
    has to prove a real ancestor relationship with ``root_signature`` rather
    than merely falling inside ``rect``.
    """

    root: Any
    root_signature: tuple[Any, ...]
    rect: tuple[int, int, int, int]


class PddAftersaleSearchResultsClosed(PddOrderNotFoundError):
    """No searched row still belongs to the queued shipped-refund branch.

    The historic class name is retained for worker compatibility.  Both
    ``退款关闭`` and ``退款成功`` are terminal no-op statuses: neither result
    may be opened merely because another aftersale application shares the same
    original order number.  The same no-op applies when a row changed shipping
    status or aftersale type between scanning and processing; that is a normal
    concurrent state change, not a worker crash.
    """


def _ignored_refund_status_marker(value: Any) -> str:
    """Return the terminal status that makes an aftersale result a no-op."""

    normalized = "".join(str(value or "").split())
    return next(
        (marker for marker in ("退款关闭", "退款成功") if marker in normalized),
        "",
    )


def classify_aftersale_url(url: str) -> str:
    surface = classify_pdd_surface(url)
    return surface if surface in {AFTERSALE_LIST, AFTERSALE_DETAIL} else "other"


class PddAftersaleUiaSession(PddEdgeUiaSession):
    """UIA-only automation for PDD's aftersale workbench.

    It intentionally lives beside the work-order session: the page paths,
    card structure and terminal actions are different and must not share the
    old work-order locators by accident.
    """

    def _configured_list_url(self) -> str:
        value = str(self.config.get("aftersale_list_url") or AFTERSALE_LIST_URL).strip()
        if classify_aftersale_url(value) != "aftersale_list":
            raise PddUiaError("售后工作台目标地址不在拼多多售后白名单内")
        return value

    @staticmethod
    def _is_aftersale_setup_url(value: str) -> bool:
        parsed = urlparse(str(value or "").strip())
        return bool(
            parsed.scheme.lower() == "https"
            and parsed.hostname == "mms.pinduoduo.com"
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
            and parsed.path == AFTERSALE_SETUP_PATH
            and not parsed.params
            and parsed.query in {"", "tab=3"}
            and not parsed.fragment
        )

    def _refund_aftersale_top_tab_target(self, handle: int) -> Any | None:
        """Resolve only the real top ``退款/售后`` tab on the setup page."""

        matches: list[Any] = []
        for control in self._controls(handle, visible_only=True):
            try:
                if self._name(control) != "退款/售后":
                    continue
                parent = control.parent()
                parent_class = str(parent.element_info.class_name or "")
                rect = self._rect(control)
                if (
                    "TAB_tabItem_" in parent_class
                    and "TAB_top_" in parent_class
                    and 120 <= rect[1] <= 300
                ):
                    matches.append(control)
            except Exception:
                continue
        if len(matches) > 1:
            raise PddUiaError(f"退款/售后顶部页签不唯一：{len(matches)}")
        return matches[0] if matches else None

    def _dismiss_message_popup(self, window: Any) -> dict[str, Any]:
        """Close only PDD's right-side 新消息 panel, never the Edge window."""

        handle = int(window.handle)
        controls = self._controls(handle)
        headers = [
            item
            for item in controls
            if self._name(item).startswith("新消息 (")
        ]
        if not headers:
            return {"present": False, "closed": False}
        close_icons = []
        for item in controls:
            try:
                rect = item.rectangle()
                if (
                    self._name(item) == "\ue6b1"
                    and str(item.element_info.control_type or "") == "Text"
                    and rect.left >= max(header.rectangle().left for header in headers) + 250
                    and 120 <= rect.top <= 230
                ):
                    close_icons.append(item)
            except Exception:
                continue
        if len(close_icons) != 1:
            raise PddUiaError(f"右上角消息弹窗关闭控件不唯一：{len(close_icons)}")
        rect = close_icons[0].rectangle()
        self._focus(window)
        # The icon is exposed as non-invokable Text. Locate it semantically,
        # then issue a real mouse click at that live UIA rectangle.
        self.mouse.click(coords=(rect.left + rect.width() // 2, rect.top + rect.height() // 2))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.2)
            if not any(
                self._name(item).startswith("新消息 (")
                for item in self._controls(handle)
            ):
                return {"present": True, "closed": True}
        raise PddUiaError("右上角消息弹窗点击关闭后仍然存在")

    def _navigate_aftersale_list(
        self, window: Any, shop: dict[str, Any] | None = None
    ) -> None:
        sidebar_error: Exception | None = None
        try:
            self._click_sidebar_entry(window, "售后工作台")
        except PddSidebarNavigationError as exc:
            sidebar_error = exc
        try:
            self._ensure_aftersale_list_surface(window, shop)
        except PddUiaError as exc:
            if sidebar_error is not None:
                raise PddUiaError(
                    f"售后工作台侧栏导航失败，退款/售后页面恢复也失败：{exc}"
                ) from sidebar_error
            raise

    def _ensure_aftersale_list_surface(
        self,
        window: Any,
        shop: dict[str, Any] | None,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        """Ensure PDD is on ``退款/售后``, never the adjacent setup page."""

        if not shop:
            raise PddUiaError("切换退款/售后前缺少店铺身份")
        target_url = self._configured_list_url()
        timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(self.config.get("aftersale_list_wait_seconds", 30))
        )
        deadline = time.monotonic() + max(2.0, timeout)
        tab_clicked = False
        address_fallback_used = False
        while time.monotonic() < deadline:
            url = self._current_url(window)
            if (
                classify_aftersale_url(url) == "aftersale_list"
                and self._window_matches_shop(window, shop)
            ):
                return
            parsed = urlparse(url)
            source_surface = classify_pdd_surface(url)
            safe_source = (
                parsed.scheme.lower() == "https"
                and parsed.netloc.lower() == "mms.pinduoduo.com"
                and (
                    source_surface
                    in {
                        WORK_ORDER_LIST,
                        WORK_ORDER_DETAIL,
                        AFTERSALE_LIST,
                        AFTERSALE_DETAIL,
                    }
                    or self._is_aftersale_setup_url(url)
                )
                and self._window_matches_shop(window, shop)
            )
            if not safe_source:
                time.sleep(0.25)
                continue

            # The left “售后工作台” entry can land on /aftersales/setup.
            # Prefer the real top tab requested by the operator.
            if not tab_clicked:
                target = self._refund_aftersale_top_tab_target(int(window.handle))
                if target is not None:
                    self._invoke_action_target(window, target, "退款/售后")
                    tab_clicked = True
                    time.sleep(0.5)
                    continue

            # If Chromium omitted the tab from UIA, use only the configured,
            # allowlisted PDD list URL after rechecking shop and source page.
            if not address_fallback_used and (
                tab_clicked
                or self._is_aftersale_setup_url(url)
                or source_surface in {WORK_ORDER_LIST, WORK_ORDER_DETAIL}
            ):
                self._focus(window)
                if not self._window_matches_shop(window, shop):
                    raise PddUiaError("退款/售后地址跳转前店铺身份发生变化")
                self.keyboard.send_keys("^l")
                time.sleep(
                    float(self.config.get("address_bar_ready_wait_seconds", 0.5))
                )
                self.keyboard.send_keys(target_url, with_spaces=True, pause=0.01)
                self.keyboard.send_keys("{ENTER}")
                address_fallback_used = True
                time.sleep(0.5)
                continue
            time.sleep(0.25)
        raise PddUiaError(
            "退款/售后页面恢复超时；当前未能确认 /aftersales/aftersale_list"
        )

    def _dismiss_overlay_popups(self, window: Any) -> dict[str, Any]:
        """Close modal-local dismiss controls and wait for late ads to settle."""

        handle = int(window.handle)
        closed = 0
        try:
            message = self._dismiss_message_popup(window)
            closed += int(bool(message.get("closed")))
        except PddUiaError:
            # A changing notification panel must not hide a real modal scan.
            pass
        max_layers = max(1, min(int(self.config.get("aftersale_popup_max_layers", 5)), 8))
        quiet_seconds = max(
            0.0,
            min(float(self.config.get("aftersale_popup_quiet_seconds", 1.2)), 3.0),
        )
        total_wait_seconds = max(
            quiet_seconds + 0.5,
            min(float(self.config.get("aftersale_popup_total_wait_seconds", 8.0)), 15.0),
        )
        dismiss_names = {
            "关闭", "关闭弹窗", "暂不", "暂不参与", "我知道了", "×", "✕", ""
        }
        deadline = time.monotonic() + total_wait_seconds
        quiet_since: float | None = None
        while time.monotonic() < deadline:
            # PDD's operational/appeal ads expose a blank Image as the close
            # control.  Its stable semantic identity is the
            # MDL_headerCloseIcon class inside an MDL_container; no accessible
            # “关闭”/“×” name exists.  Handle that real structure first and
            # rebuild the UIA tree after every layer because the platform can
            # show 0-3 ads sequentially.
            class_candidates = []
            for control in self._controls(handle, visible_only=True):
                try:
                    class_name = str(control.element_info.class_name or "")
                    if "MDL_headerCloseIcon" not in class_name:
                        continue
                    parent = control
                    modal = None
                    for _depth in range(5):
                        parent = parent.parent()
                        if parent is None:
                            break
                        parent_class = str(parent.element_info.class_name or "")
                        if "MDL_container" in parent_class:
                            modal = parent
                            break
                    if modal is not None:
                        class_candidates.append((control, modal))
                except Exception:
                    continue
            if class_candidates:
                if closed >= max_layers:
                    raise PddUiaError(f"售后工作台广告弹窗超过安全上限：{max_layers} 层")
                # A single active overlay is expected.  If Chromium exposes a
                # hidden lower layer as visible too, prefer the close icon
                # drawn furthest down/right (the current top modal) and then
                # rescan instead of clicking every captured rectangle.
                control, _modal = max(
                    class_candidates,
                    key=lambda item: (
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
                continue

            candidates: list[tuple[Any, Any]] = []
            for control in self._controls(handle, visible_only=True):
                try:
                    if self._name(control).strip() not in dismiss_names:
                        continue
                    parent = control.parent()
                    overlay = None
                    for _depth in range(4):
                        if parent is None:
                            break
                        rect = parent.rectangle()
                        control_type = str(parent.element_info.control_type or "")
                        if (
                            control_type in {"Window", "Pane", "Group"}
                            and rect.width() >= 280
                            and rect.height() >= 140
                            and rect.left >= 120
                        ):
                            overlay = parent
                            break
                        parent = parent.parent()
                    if overlay is not None:
                        candidates.append((control, overlay))
                except Exception:
                    continue
            if not candidates:
                if quiet_since is None:
                    quiet_since = time.monotonic()
                if time.monotonic() - quiet_since >= quiet_seconds:
                    break
                time.sleep(0.2)
                continue
            if closed >= max_layers:
                raise PddUiaError(f"售后工作台广告弹窗超过安全上限：{max_layers} 层")
            # The topmost modal normally has the greatest top/left position.
            control, _overlay = max(
                candidates,
                key=lambda item: (item[1].rectangle().top, item[1].rectangle().left),
            )
            self._focus(window)
            try:
                control.click_input()
            except Exception:
                rect = control.rectangle()
                self.mouse.click(coords=(rect.left + rect.width() // 2, rect.top + rect.height() // 2))
            closed += 1
            quiet_since = None
            time.sleep(0.35)

        remaining_class_closers = []
        for control in self._controls(handle, visible_only=True):
            try:
                if "MDL_headerCloseIcon" in str(
                    control.element_info.class_name or ""
                ):
                    remaining_class_closers.append(control)
            except Exception:
                continue
        if remaining_class_closers:
            raise PddUiaError(
                f"售后工作台广告弹窗关闭 {closed} 层后仍有遮挡："
                f"{len(remaining_class_closers)}"
            )
        return {"closed": closed, "clear": True}

    def _pending_filter_selected(self, control: Any) -> bool:
        """Use the real quick-filter card state; the Text itself has no selection pattern."""

        current = control
        for _ in range(4):
            try:
                class_name = str(current.element_info.class_name or "")
                if "CAD_beastCardChecked" in class_name:
                    return True
                current = current.parent()
                if current is None:
                    break
            except Exception:
                break
        return False

    def _pending_filter_selected_by_url(self, window: Any) -> bool:
        """Treat PDD's canonical pending-filter URL as selected state.

        On the real aftersale workbench the selected quick-filter card does not
        consistently expose ``CAD_beastCardChecked`` through UIA.  PDD does,
        however, update the active list URL to ``searchType=4``.  Missing that
        signal caused the already-selected card to be clicked again, which
        opened another workbench tab and left the next order on a stale list
        anchor.
        """

        try:
            url = self._current_url(window)
            if classify_aftersale_url(url) != AFTERSALE_LIST:
                return False
            values = parse_qs(urlparse(url).query).get("searchType", [])
            return any(str(value).strip() == "4" for value in values)
        except Exception:
            return False

    def _activate_aftersale_tab(
        self,
        window: Any,
        shop: dict[str, Any],
        kind: str,
        *,
        attempts: int | None = None,
        continue_after_repeat: bool = False,
    ) -> bool:
        """Activate a shop tab of the requested aftersale kind.

        Normal navigation stops once Chromium cycles back to an already-seen
        tab.  Cleanup is intentionally more persistent: a shop can have
        several identical list tabs left by an older run, so stopping at the
        first repeated title/URL can miss the retained list anchor entirely.
        """

        configured_attempts = max(
            1, min(int(self.config.get("tab_switch_attempts", 8)), 20)
        )
        attempts = configured_attempts if attempts is None else max(1, min(int(attempts), 40))
        wait_seconds = max(
            0.15, min(float(self.config.get("tab_switch_wait_seconds", 0.35)), 2.0)
        )
        seen: set[tuple[str, str]] = set()
        for _ in range(attempts + 1):
            url = self._current_url(window)
            handle = int(window.handle)
            if (
                classify_aftersale_url(url) == kind
                and (shop["name"] in window.window_text() or self._has_exact_name(handle, shop["name"]))
            ):
                # The UIA address-bar read can lag one Ctrl+Tab behind while
                # Chromium finishes the switch, so a freshly cycled window can
                # still report the previous tab's URL one iteration late.  A
                # match consumed at that moment returns with the wrong tab
                # active and the caller's page-contract assertion then fails
                # with 预期=列表/实际=详情.  Re-read once after a short settle
                # and only accept a match that stays stable.
                time.sleep(wait_seconds)
                if (
                    classify_aftersale_url(self._current_url(window)) == kind
                    and (
                        shop["name"] in window.window_text()
                        or self._has_exact_name(handle, shop["name"])
                    )
                ):
                    return True
                continue
            signature = (url, window.window_text())
            if signature in seen and not continue_after_repeat:
                return False
            seen.add(signature)
            self._focus(window)
            self.keyboard.send_keys("^{TAB}")
            time.sleep(wait_seconds)
        return False

    def _list_window(
        self,
        shop: dict[str, Any],
        *,
        navigate: bool = True,
        tab_attempts: int | None = None,
        continue_after_repeat: bool = False,
    ) -> Any:
        self.ensure_shop_list(shop)
        windows = self._shop_windows(shop)
        matches = [
            w
            for w in windows
            if self._activate_aftersale_tab(
                w,
                shop,
                "aftersale_list",
                attempts=tab_attempts,
                continue_after_repeat=continue_after_repeat,
            )
        ]
        unique = {int(w.handle): w for w in matches}
        if len(unique) == 1:
            window = next(iter(unique.values()))
            self.assert_page_contract(
                window,
                shop,
                AFTERSALE_LIST,
                stage="解析售后工作台列表窗口",
            )
            return window
        if len(unique) > 1:
            raise PddUiaError(f"{shop['name']} 的售后工作台窗口不唯一：{len(unique)}")
        if not navigate:
            raise PddUiaError(f"{shop['name']} 未找到售后工作台列表页")
        if len(windows) != 1:
            raise PddUiaError(f"{shop['name']} 无法确定售后工作台导航窗口：{len(windows)}")
        window = windows[0]
        self._navigate_aftersale_list(window, shop)
        deadline = time.monotonic() + float(
            self.config.get("aftersale_list_wait_seconds", 30)
        )
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if (
                classify_aftersale_url(self._current_url(window)) == "aftersale_list"
                # The merchant name is consistently proven either by the
                # active Edge title or a UIA node.  The recovery method above
                # already uses this same shop contract; requiring only a
                # UIA text node here made a correctly recovered 退款/售后 page
                # wait until timeout for some stores (including 烨成).
                and self._window_matches_shop(window, shop)
            ):
                self.assert_page_contract(
                    window,
                    shop,
                    AFTERSALE_LIST,
                    stage="导航至售后工作台",
                )
                return window
        raise PddUiaError(f"{shop['name']} 售后工作台列表加载超时")

    def _unique_named(self, handle: int, text: str, *, visible: bool = False) -> Any | None:
        matches = []
        for control in self._controls(handle, visible_only=visible):
            try:
                if self._name(control) == text:
                    matches.append(control)
            except Exception:
                continue
        if len(matches) > 1:
            # Prefer one actionable UIA wrapper when a label and its radio
            # expose the same accessible name.
            actionable = [
                item
                for item in matches
                if str(item.element_info.control_type or "")
                in {"Button", "Hyperlink", "RadioButton", "CheckBox"}
            ]
            if len(actionable) == 1:
                return actionable[0]
            raise PddUiaError(f"售后页面控件“{text}”不唯一：{len(matches)}")
        return matches[0] if matches else None

    @staticmethod
    def _aftersale_pending_badge_from_name(name: str) -> int | None:
        compact = re.sub(r"\s+", "", str(name or ""))
        for pattern in (
            r"待处理即将逾期[（(]?(\d+)\+?[）)]?",
            r"即将逾期售后[（(]?(\d+)\+?[）)]?",
        ):
            matched = re.fullmatch(pattern, compact)
            if matched:
                return int(matched.group(1))
        return None

    def _aftersale_pending_badge_count(self, handle: int) -> int | None:
        """Read only the number belonging to the quick pending filter."""

        self.last_pending_badge_exact = None
        controls = self._controls(handle, visible_only=True)
        named: list[tuple[Any, str]] = []
        for control in controls:
            try:
                named.append((control, self._name(control)))
            except Exception:
                continue

        # Prefer a direct body-filter count; the header shortcut is accepted
        # only when the body count is unavailable.
        direct: list[tuple[int, int, bool]] = []
        for control, name in named:
            count = self._aftersale_pending_badge_from_name(name)
            if count is None:
                continue
            compact = re.sub(r"\s+", "", name)
            direct.append(
                (
                    0 if compact.startswith("待处理即将逾期") else 10,
                    count,
                    "+" not in compact,
                )
            )
        if direct:
            direct.sort(key=lambda item: item[0])
            self.last_pending_badge_exact = direct[0][2]
            return direct[0][1]

        labels = [
            control
            for control, name in named
            if re.sub(r"\s+", "", name) == "待处理即将逾期"
        ]
        adjacent: list[tuple[float, int, bool]] = []
        for label in labels:
            ancestor = label
            for _ in range(3):
                try:
                    ancestor = ancestor.parent()
                    ancestor_name = self._name(ancestor)
                    count = self._aftersale_pending_badge_from_name(ancestor_name)
                except Exception:
                    break
                if count is not None:
                    self.last_pending_badge_exact = "+" not in re.sub(
                        r"\s+", "", ancestor_name
                    )
                    return count
            try:
                left, top, right, bottom = self._rect(label)
            except Exception:
                continue
            center_y = (top + bottom) / 2
            for control, name in named:
                numeric = re.fullmatch(r"(\d+)\+?", re.sub(r"\s+", "", name))
                if not numeric:
                    continue
                try:
                    c_left, c_top, _c_right, c_bottom = self._rect(control)
                except Exception:
                    continue
                c_center_y = (c_top + c_bottom) / 2
                horizontal_gap = c_left - right
                if -8 <= horizontal_gap <= 90 and abs(c_center_y - center_y) <= 18:
                    adjacent.append(
                        (
                            abs(horizontal_gap) + abs(c_center_y - center_y),
                            int(numeric.group(1)),
                            "+" not in re.sub(r"\s+", "", name),
                        )
                    )
        if not adjacent:
            return None
        adjacent.sort(key=lambda item: item[0])
        self.last_pending_badge_exact = adjacent[0][2]
        return adjacent[0][1]

    def _click_quick_pending(
        self,
        window: Any,
        shop: dict[str, Any],
        *,
        popups_cleared: bool = False,
    ) -> None:
        # Ads can arrive immediately after refresh and hide the page-ready
        # semantics from UIA.  Clear them before the strict page contract;
        # validating first would reject the shop without ever closing its ad.
        if not popups_cleared:
            self._dismiss_overlay_popups(window)
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_LIST,
            stage="点击待处理即将逾期前",
            require_ready=True,
        )
        handle = int(window.handle)
        matches = []
        discovery_deadline = time.monotonic() + float(
            self.config.get("aftersale_list_wait_seconds", 30)
        )
        while time.monotonic() < discovery_deadline:
            matches = []
            for control in self._controls(handle):
                try:
                    name = self._name(control)
                    if name == "待处理即将逾期" or name.startswith("即将逾期售后"):
                        matches.append(control)
                except Exception:
                    continue
            if matches:
                break
            time.sleep(
                max(
                    0.15,
                    min(
                        float(self.config.get("aftersale_search_poll_seconds", 0.3)),
                        1.0,
                    ),
                )
            )
        if not matches:
            raise PddUiaError("售后工作台加载完成后仍未找到待处理即将逾期入口")
        # The header shortcut and body shortcut can coexist. Prefer the body
        # label from the screenshot, otherwise use the unique header link.
        body = [item for item in matches if self._name(item) == "待处理即将逾期"]
        target = body[0] if len(body) == 1 else matches[0] if len(matches) == 1 else None
        if target is None:
            header = [item for item in matches if self._name(item).startswith("即将逾期售后")]
            target = header[0] if len(header) == 1 else None
        if target is None:
            raise PddUiaError(f"待处理即将逾期入口不唯一：{len(matches)}")
        if not (
            self._pending_filter_selected(target)
            or self._pending_filter_selected_by_url(window)
        ):
            try:
                if hasattr(target, "is_enabled") and not target.is_enabled():
                    raise PddUiaError("待处理即将逾期入口不可用")
            except AttributeError:
                pass
            self._focus(window)
            # This quick-filter is a toggle.  It is deliberately clicked at
            # most once: retry-clicking deselects a correctly selected filter
            # and makes the collector ingest unrelated aftersale records.
            target.click_input()

        selection_deadline = time.monotonic() + max(
            0.1,
            min(float(self.config.get("aftersale_pending_selection_wait_seconds", 5.0)), 10.0),
        )
        selected = False
        while time.monotonic() < selection_deadline:
            current = self._unique_named(handle, "待处理即将逾期", visible=True)
            if self._pending_filter_selected_by_url(window) or (
                current is not None and self._pending_filter_selected(current)
            ):
                selected = True
                break
            time.sleep(0.2)
        if not selected:
            raise PddUiaError("待处理即将逾期点击后未进入选中状态；已禁止二次点击和采集")

        time.sleep(
            max(
                0.0,
                min(float(self.config.get("aftersale_pending_settle_seconds", 1.2)), 5.0),
            )
        )
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_LIST,
            stage="待处理即将逾期已选中",
            require_ready=True,
        )

    @staticmethod
    def _aftersale_no_from_values(values: list[str]) -> str:
        """Extract a stable aftersale id only from explicit labels or detail URLs."""

        for raw_value in values:
            value = str(raw_value or "").strip()
            if not value:
                continue
            labelled = AFTERSALE_NO_RE.search(value)
            if labelled:
                return labelled.group(1)
            query_match = AFTERSALE_QUERY_ID_RE.search(value)
            if query_match:
                return query_match.group(1)
            try:
                parsed = urlparse(value)
                query = parse_qs(parsed.query)
            except Exception:
                continue
            if "aftersale" not in parsed.path.lower():
                continue
            for key in ("id", "afterSaleId", "aftersaleId", "refundId"):
                for candidate in query.get(key, []):
                    candidate = str(candidate or "").strip()
                    if re.fullmatch(r"[A-Za-z0-9_-]{6,64}", candidate):
                        return candidate
        return ""

    @staticmethod
    def _control_semantic_values(control: Any) -> list[str]:
        """Read non-mutating UIA values that may carry an aftersale detail URL."""

        values: list[str] = []
        try:
            info = control.element_info
            for attribute in ("name", "automation_id", "rich_text"):
                value = str(getattr(info, attribute, "") or "").strip()
                if value:
                    values.append(value)
        except Exception:
            pass
        try:
            legacy = control.legacy_properties()
            if isinstance(legacy, dict):
                values.extend(
                    str(value).strip()
                    for value in legacy.values()
                    if str(value or "").strip()
                )
        except Exception:
            pass
        try:
            value = str(control.get_value() or "").strip()
            if value:
                values.append(value)
        except Exception:
            pass
        try:
            value = str(control.iface_legacy_iaccessible.CurrentValue or "").strip()
            if value:
                values.append(value)
        except Exception:
            pass
        return list(dict.fromkeys(values))

    @staticmethod
    def _row_labeled_value(
        row: list[tuple[str, tuple[int, int, int, int], Any]],
        labels: tuple[str, ...],
    ) -> str:
        normalized_labels = tuple(label.rstrip("：:").strip() for label in labels)
        for name, _rect, _control in row:
            compact = str(name or "").strip()
            for label in normalized_labels:
                matched = re.match(rf"^{re.escape(label)}\s*[：:]\s*(.+)$", compact)
                if matched and matched.group(1).strip():
                    return matched.group(1).strip()

        label_rows = [
            (rect, name)
            for name, rect, _control in row
            if str(name or "").rstrip("：:").strip() in normalized_labels
        ]
        for label_rect, label_name in label_rows:
            candidates: list[tuple[int, str]] = []
            for name, rect, _control in row:
                if name == label_name or not str(name or "").strip():
                    continue
                if rect[0] < label_rect[2] - 4 or abs(rect[1] - label_rect[1]) > 8:
                    continue
                candidates.append((rect[0], str(name).strip()))
            if candidates:
                return min(candidates, key=lambda item: item[0])[1]
        return ""

    @classmethod
    def _row_aftersale_status(
        cls, row: list[tuple[str, tuple[int, int, int, int], Any]]
    ) -> str:
        labelled = cls._row_labeled_value(row, ("售后状态", "处理状态"))
        if labelled:
            return labelled
        names = [str(name or "").strip() for name, _rect, _control in row]
        for status in AFTERSALE_STATUS_NAMES:
            if any(name == status for name in names):
                return status
        for status in AFTERSALE_STATUS_NAMES:
            if any(status in name for name in names):
                return status
        return ""

    @classmethod
    def _row_apply_time(
        cls, row: list[tuple[str, tuple[int, int, int, int], Any]]
    ) -> str:
        labelled = cls._row_labeled_value(row, ("申请时间", "售后申请时间"))
        matched = DATETIME_RE.search(labelled)
        if matched:
            return matched.group(1)
        for name, _rect, _control in row:
            if "申请时间" not in str(name or ""):
                continue
            matched = DATETIME_RE.search(str(name))
            if matched:
                return matched.group(1)
        return ""

    @staticmethod
    def _looks_like_urgency_text(text: str) -> bool:
        value = re.sub(r"\s+", "", str(text or ""))
        if not value:
            return False
        normalized = value.replace("：", ":")
        for prefix in ("申请原因:", "退款原因:", "处理时效:", "剩余时间:"):
            if normalized.startswith(prefix):
                value = normalized[len(prefix) :]
                break
        if "已逾期" in value or "即将逾期" in value:
            return True
        if re.fullmatch(r"\d+天\d+(?:时|小时)\d+分(?:\d+秒)?", value):
            return True
        if re.fullmatch(r"\d+(?:时|小时)\d+分(?:\d+秒)?", value):
            return True
        return bool(
            re.search(r"\d+\s*(?:天|小时|时|分钟|分)", value)
            and any(token in value for token in ("剩余", "还剩", "后自动", "倒计时", "逾期"))
        )

    @classmethod
    def _row_urgency_text(
        cls,
        row: list[tuple[str, tuple[int, int, int, int], Any]]
    ) -> str:
        candidates: list[str] = []
        for name, _rect, _control in row:
            text = str(name or "").strip()
            if not text or text == "待处理即将逾期":
                continue
            if cls._looks_like_urgency_text(text):
                candidates.append(text)
        return max(candidates, key=len) if candidates else ""

    def _visible_cards(self, handle: int) -> list[AftersaleCard]:
        controls = self._controls(handle)
        named: list[tuple[str, tuple[int, int, int, int], Any]] = []
        for control in controls:
            try:
                name = self._name(control).strip()
                if name:
                    named.append((name, self._rect(control), control))
            except Exception:
                continue
        unique_orders = {
            (name, rect): (name, rect, control)
            for name, rect, control in named
            if ORDER_RE.fullmatch(name)
        }
        orders = sorted(unique_orders.values(), key=lambda item: (item[1][1], item[1][0]))
        cards: list[AftersaleCard] = []
        for order_index, (order_no, order_rect, _order_control) in enumerate(orders):
            top = order_rect[1]
            row_bottom = (
                orders[order_index + 1][1][1] - 8
                if order_index + 1 < len(orders)
                else top + 190
            )
            row = [
                (name, rect, control)
                for name, rect, control in named
                if top - 8 <= rect[1] < row_bottom
                and rect[0] >= max(200, order_rect[0] - 40)
            ]
            detail_controls = [
                control
                for name, rect, control in row
                if name == "查看详情" and rect[0] > order_rect[0]
            ]
            if not detail_controls:
                continue
            row_names = [name for name, _rect, _control in row]
            labelled_refund_type = self._row_labeled_value(row, ("售后类型", "退款类型"))
            if "退货退款" in labelled_refund_type or any(
                name == "退货退款" for name in row_names
            ):
                refund_type = "退货退款"
            elif labelled_refund_type == "退款" or any(name == "退款" for name in row_names):
                refund_type = "退款"
            else:
                refund_type = ""
            if not refund_type:
                continue
            shipping_status = next(
                (
                    name
                    for name in row_names
                    if name in {"已发货", "未发货", "已签收", "已收到货"}
                ),
                "",
            )
            reason = self._row_labeled_value(row, ("申请原因", "退款原因"))
            # Some UIA trees expose the urgency countdown immediately to the
            # right of a generic label.  Treating that moving value as the
            # refund reason both misleads operators and changes fallback
            # identity every scan, so reject it before the coordinate-based
            # semantic fallback as well.
            if self._looks_like_urgency_text(reason):
                reason = ""
            if not reason:
                reason = next(
                    (
                        name
                        for name, rect, _control in row
                        if 1380 <= rect[0] < 1580
                        and name
                        not in {"备注", "绿色", "添加备注", "查看/修改备注"}
                        and not name.startswith(("申请时间：", "（"))
                        and not self._looks_like_urgency_text(name)
                    ),
                    "",
                )
            amount = 0.0
            labelled_amount = self._row_labeled_value(row, ("退款金额",))
            amount_match = AMOUNT_RE.fullmatch(labelled_amount.replace(" ", ""))
            if amount_match:
                amount = float(amount_match.group(1))
            for name, rect, _control in row:
                if amount:
                    break
                if not (600 <= rect[0] <= 760):
                    continue
                match = AMOUNT_RE.fullmatch(name.replace(" ", ""))
                if match:
                    amount = float(match.group(1))
                    break
            identity_values = list(row_names)
            for control in detail_controls:
                identity_values.extend(self._control_semantic_values(control))
            aftersale_no = self._aftersale_no_from_values(identity_values)
            cards.append(
                AftersaleCard(
                    order_no=order_no,
                    refund_type=refund_type,
                    shipping_status=shipping_status,
                    refund_reason=reason,
                    refund_amount=amount,
                    aftersale_no=aftersale_no,
                    status=self._row_aftersale_status(row),
                    apply_time=self._row_apply_time(row),
                    urgency_text=self._row_urgency_text(row),
                )
            )
        return cards

    def scan_pending(self, shop: dict[str, Any]) -> AftersaleListResult:
        window = self._list_window(shop)
        self._focus(window)
        self.keyboard.send_keys("^r")
        time.sleep(float(self.config.get("aftersale_refresh_wait_seconds", 1.2)))
        # PDD intermittently redirects a refreshed workbench tab to
        # /aftersales/setup.  Recover the exact “退款/售后” list before any
        # semantic read instead of failing later or collecting the wrong page.
        self._ensure_aftersale_list_surface(window, shop)
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_LIST,
            stage="售后工作台刷新完成",
        )
        handle = int(window.handle)
        # Ads must still be closed before the badge can be trusted.  Its value
        # is only an empty/non-empty hint for this cycle: a confirmed zero
        # skips the filter click, scrolling and card collection entirely.
        self._dismiss_overlay_popups(window)
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_LIST,
            stage="售后工作台广告清理完成",
            require_ready=True,
        )
        pending_badge_count = self._aftersale_pending_badge_count(handle)
        if pending_badge_count == 0:
            self.last_collection_summary = {
                "discovered_cards": 0,
                "pending_badge_count": 0,
                "pending_badge_exact": bool(
                    getattr(self, "last_pending_badge_exact", True)
                ),
                "filter": "待处理即将逾期",
                "badge_vs_discovered_difference": 0,
                "badge_vs_pager_difference": 0,
                "pager_total": 0,
                "capture_complete": True,
                "capture_shortfall": 0,
                "skip_reason": "aftersale_pending_badge_zero",
                "scrolls": 0,
            }
            return AftersaleListResult(
                handle=handle,
                title=window.window_text(),
                url=self._current_url(window),
                cards=[],
            )
        self._click_quick_pending(window, shop, popups_cleared=True)
        cards = self._collect_pending_pages(
            handle,
            shop,
            single_page_hint=bool(
                getattr(self, "last_pending_badge_exact", None) is True
                and pending_badge_count is not None
                and 0 < pending_badge_count <= 8
            ),
        )
        return AftersaleListResult(
            handle=handle,
            title=window.window_text(),
            url=self._current_url(window),
            cards=cards,
        )

    @staticmethod
    def _aftersale_card_identity(card: AftersaleCard) -> str:
        if str(card.aftersale_no or "").strip():
            return f"aftersale_no:{str(card.aftersale_no).strip()}"
        # Older PDD list variants do not expose the aftersale number/link to
        # UIA.  Keep a deterministic semantic fallback instead of collapsing
        # every aftersale application belonging to the same original order.
        # It deliberately excludes page/viewport coordinates so scrolling or
        # pagination redraws cannot manufacture a new identity.
        return "fallback:" + "\x1f".join(
            (
                str(card.order_no or "").strip(),
                str(card.refund_type or "").strip(),
                str(card.apply_time or "").strip(),
                f"{float(card.refund_amount or 0):.2f}",
            )
        )

    def _materialize_aftersale_page(
        self,
        handle: int,
        shop: dict[str, Any],
        page_no: int,
    ) -> list[AftersaleCard]:
        """Collect one aftersale page from top to pager exactly once."""

        try:
            max_scrolls = max(
                3,
                min(int(self.config.get("aftersale_scan_max_scrolls", 30)), 80),
            )
        except (TypeError, ValueError):
            max_scrolls = 30
        try:
            wait_seconds = max(
                0.15,
                min(float(self.config.get("aftersale_scan_wait_seconds", 0.35)), 3.0),
            )
        except (TypeError, ValueError):
            wait_seconds = 0.35

        self._scroll_to_top(handle)
        found: dict[str, AftersaleCard] = {}
        no_new_rounds = 0
        for scroll_index in range(max_scrolls + 1):
            self.assert_page_contract(
                self.Desktop(backend="win32").window(handle=handle),
                shop,
                AFTERSALE_LIST,
                stage="售后工作台待处理采集",
                require_ready=True,
            )
            before = len(found)
            for card in self._visible_cards(handle):
                found.setdefault(self._aftersale_card_identity(card), card)
            no_new_rounds = no_new_rounds + 1 if len(found) == before else 0
            # One PageDown can be swallowed while Chromium is rendering or a
            # human agent has focus. Require two stable samples before ending
            # the page walk; this is intentionally a little permissive because
            # a false early stop silently loses an urgent aftersale record.
            if found and no_new_rounds >= 2:
                break
            if scroll_index >= max_scrolls:
                break
            self._focus(self.Desktop(backend="win32").window(handle=handle))
            self.keyboard.send_keys("{PGDN}")
            time.sleep(wait_seconds)

        self._scroll_to_pagination(handle)
        # Ctrl+End can reveal rows that the final PageDown never materialized.
        # Re-resolve the live UIA tree and collect that bottom viewport once
        # before reading/clicking the pagination controls.
        self.assert_page_contract(
            self.Desktop(backend="win32").window(handle=handle),
            shop,
            AFTERSALE_LIST,
            stage="售后工作台待处理即将逾期底部补采",
            require_ready=True,
        )
        for card in self._visible_cards(handle):
            found.setdefault(self._aftersale_card_identity(card), card)
        cards = list(found.values())
        for index, card in enumerate(cards):
            card.page_no = page_no
            card.page_index = index
        return cards

    def _collect_pending_pages(
        self,
        handle: int,
        shop: dict[str, Any],
        *,
        single_page_hint: bool = False,
    ) -> list[AftersaleCard]:
        """Scan each current aftersale page once, then restore page one."""

        all_cards: list[AftersaleCard] = []
        seen_pages: set[int] = set()
        reported_total: int | None = None
        if single_page_hint:
            try:
                seen_pages.add(1)
                all_cards.extend(self._materialize_aftersale_page(handle, shop, 1))
            finally:
                self._scroll_to_top(handle)
        else:
            self._go_to_first_page(handle, ensure_pagination_visible=True)
            try:
                for _ in range(100):
                    state = self._pagination_state(handle)
                    if state.get("total") is not None:
                        try:
                            reported_total = int(state["total"])
                        except (TypeError, ValueError):
                            pass
                    current_page = state.get("current_page")
                    if current_page is None:
                        if not seen_pages and self._pager_absent_single_page(state):
                            current_page = 1
                        elif not seen_pages and any(
                            page == 1 for page, _ in state.get("page_items", [])
                        ):
                            current_page = 1
                        else:
                            raise PddUiaError("售后工作台分页栏未稳定，无法读取当前页")
                    page_no = int(current_page)
                    if page_no in seen_pages:
                        raise PddUiaError(
                            f"售后工作台分页循环未前进，当前页：{page_no}"
                        )
                    seen_pages.add(page_no)
                    all_cards.extend(
                        self._materialize_aftersale_page(handle, shop, page_no)
                    )

                    # The page materializer leaves the pager visible. Re-resolve
                    # the live next control so a page redraw cannot leave a stale
                    # UIA wrapper in use.
                    state = self._pagination_state(handle)
                    if state.get("total") is not None:
                        try:
                            reported_total = int(state["total"])
                        except (TypeError, ValueError):
                            pass
                    next_control = state.get("next")
                    if next_control is None or state.get("next_disabled"):
                        break
                    old_signature = self._page_order_signature(handle)
                    self._focus(self.Desktop(backend="win32").window(handle=handle))
                    next_control.click_input()
                    self._wait_for_page_change(handle, page_no, old_signature)
                else:
                    raise PddUiaError("售后工作台翻页超过 100 页安全上限")
            finally:
                # A scan must never leave the shop on page two or at the bottom;
                # later exact-order searches assume the retained list anchor is on
                # the first page and can expose its search controls.
                self._go_to_first_page(handle, ensure_pagination_visible=False)
                self._scroll_to_top(handle)

        unique: dict[str, AftersaleCard] = {}
        for card in sorted(all_cards, key=lambda item: (item.page_no, item.page_index)):
            unique.setdefault(self._aftersale_card_identity(card), card)
        cards = list(unique.values())
        pending_badge_count = self._aftersale_pending_badge_count(handle)
        pending_badge_exact = getattr(self, "last_pending_badge_exact", None)
        self.last_collection_summary = {
            "filter": "待处理即将逾期",
            "discovered_cards": len(cards),
            "pending_badge_count": pending_badge_count,
            "pending_badge_exact": pending_badge_exact,
            "badge_vs_discovered_difference": (
                pending_badge_count - len(cards)
                if pending_badge_exact is True and pending_badge_count is not None
                else None
            ),
            "badge_vs_pager_difference": (
                pending_badge_count - reported_total
                if pending_badge_exact is True
                and pending_badge_count is not None
                and reported_total is not None
                else None
            ),
            "pager_total": reported_total,
            "capture_complete": reported_total is None or len(cards) >= reported_total,
            "capture_shortfall": (
                max(reported_total - len(cards), 0) if reported_total is not None else None
            ),
            "skip_reason": "",
            "pages_scanned": len(seen_pages),
            "single_pass": True,
            "single_page_hint": bool(single_page_hint),
        }
        return cards

    def _detail_controls_for_order(self, handle: int, order_no: str) -> list[Any]:
        """Return every row-local 查看详情 candidate for an exact order.

        PDD search can return more than one aftersale application for a single
        order number.  The return-refund legacy branch can still request an
        explicit index; the shipped-refund branch uses this complete ordered
        list together with row semantics to exclude closed applications.
        """
        controls = self._controls(handle)
        order_controls = []
        for control in controls:
            try:
                if self._name(control) == order_no:
                    order_controls.append(control)
            except Exception:
                continue
        if not order_controls:
            return []
        order_controls.sort(key=lambda item: self._rect(item)[1])
        details: dict[tuple[int, int, int, int], Any] = {}
        for index, order_control in enumerate(order_controls):
            order_rect = self._rect(order_control)
            row_top = order_rect[1] - 8
            row_bottom = (
                self._rect(order_controls[index + 1])[1] - 8
                if index + 1 < len(order_controls)
                else order_rect[1] + 190
            )
            for control in controls:
                try:
                    rect = self._rect(control)
                    if (
                        self._name(control) == "查看详情"
                        and rect[0] >= 1450
                        and row_top <= rect[1] < row_bottom
                    ):
                        details[rect] = control
                except Exception:
                    continue
        return [details[key] for key in sorted(details, key=lambda rect: (rect[1], rect[0]))]

    def _order_search(self, handle: int) -> Any:
        matches = []
        for control in self._controls(handle):
            try:
                if (
                    self._name(control) == "请输入订单编号/物流单号/商品ID/售后编号"
                    and str(control.element_info.control_type or "") == "Edit"
                    and "IPT_input" in str(control.element_info.class_name or "")
                ):
                    matches.append(control)
            except Exception:
                continue
        if len(matches) != 1:
            raise PddUiaError(f"售后订单搜索框不唯一：{len(matches)}")
        return matches[0]

    def _safe_order_search(
        self, window: Any, handle: int, shop: dict[str, Any]
    ) -> Any:
        """Resolve the exact search Edit through UIA ValuePattern only.

        The PDD sidebar and top navigation remain fixed while the document body
        moves.  Sending PageUp/PageDown to the Edge window made previously
        captured rectangles stale and could redirect a later click to a fixed
        navigation item.  The order number is written with ValuePattern and the
        adjacent search action is invoked with InvokePattern, so neither step
        needs the Edit to be inside an arbitrary screen-coordinate band.  This
        matters on a maximized 1080p Edge window where the real search box can
        sit at y=1007..1034 and is already visible to Chromium: ScrollIntoView
        correctly performs no movement, while the old hard-coded bottom<=1000
        test falsely rejected it.  If PDD does not expose the exact Edit and a
        readable ValuePattern, fail closed without scrolling or coordinates.
        """

        deadline = time.monotonic() + max(
            1.0,
            min(float(self.config.get("aftersale_search_wait_seconds", 8.0)), 15.0),
        )
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            self.assert_page_contract(
                window,
                shop,
                AFTERSALE_LIST,
                stage="售后工作台订单搜索框定位",
                require_ready=True,
            )
            try:
                search = self._order_search(handle)
                try:
                    # Reading the current value is a side-effect-free probe that
                    # Chromium exposed the UIA ValuePattern required by the
                    # subsequent SetValue operation.  Screen geometry is not a
                    # safety signal for a pattern-based action.
                    search.get_value()
                except Exception as exc:
                    last_error = exc
                else:
                    return search
            except PddUiaError as exc:
                last_error = exc
            time.sleep(0.2)

        detail = f"：{last_error}" if last_error is not None else ""
        raise PddUiaError(
            "售后订单搜索框无法通过精确 UIA ValuePattern 定位；"
            f"已禁止全局滚动和坐标兜底{detail}"
        )

    def _order_search_button(self, handle: int, search: Any) -> Any:
        """Wait through the search row's local Chromium repaint.

        Setting the Edit value can briefly replace the adjacent Search node in
        the UIA tree.  Re-resolve both controls until one geometrically bound
        button exists; never reuse a stale rectangle and never fall back to a
        desktop coordinate.
        """

        try:
            timeout = max(
                0.5,
                min(
                    float(self.config.get("aftersale_search_button_wait_seconds", 3.0)),
                    8.0,
                ),
            )
        except (TypeError, ValueError):
            timeout = 3.0
        deadline = time.monotonic() + timeout
        last_count = 0
        expected_value = ""
        try:
            expected_value = str(search.get_value() or "")
        except Exception:
            pass
        while time.monotonic() < deadline:
            try:
                current_search = self._order_search(handle)
                if expected_value and str(current_search.get_value() or "") != expected_value:
                    time.sleep(0.2)
                    continue
                search_rect = self._rect(current_search)
            except Exception:
                time.sleep(0.2)
                continue
            matches = []
            for control in self._controls(handle):
                try:
                    rect = self._rect(control)
                    if (
                        self._name(control) == "搜索"
                        and rect[0] >= search_rect[2]
                        and search_rect[1] - 10 <= rect[1] <= search_rect[3] + 10
                    ):
                        matches.append(control)
                except Exception:
                    continue
            last_count = len(matches)
            if last_count == 1:
                return matches[0]
            time.sleep(0.2)
        raise PddUiaError(f"售后订单搜索按钮不唯一：{last_count}")

    @staticmethod
    def _set_search_value_with_uia(search: Any, order_no: str) -> bool:
        """Write the exact order through UIA ValuePattern without coordinates."""

        try:
            search.iface_value.SetValue(order_no)
            return True
        except Exception:
            return False

    @staticmethod
    def _invoke_with_uia(control: Any) -> bool:
        """Invoke an exact actionable control/ancestor without screen coordinates."""

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

    def _detail_target_for_order(
        self,
        handle: int,
        order_no: str,
        candidate_index: int,
    ) -> Any | None:
        """Return the exact row-local 查看详情 candidate without moving the page."""

        targets = self._detail_controls_for_order(handle, order_no)
        if not targets:
            return None
        self.last_search_candidate_count = len(targets)
        if candidate_index >= len(targets):
            raise PddOrderNotFoundError(
                f"售后订单 {order_no} 没有第 {candidate_index + 1} 条候选记录"
            )
        return targets[candidate_index]

    def _select_shipped_refund_search_candidate(
        self,
        handle: int,
        order_no: str,
        expected_aftersale_no: str = "",
    ) -> tuple[int, str]:
        """Select a safe result row after searching the original order number.

        One original order can have multiple aftersale applications.  The
        result rows themselves expose all three business gates needed here, so
        do not open a detail page merely to discover that the first row is an
        old closed refund.
        """

        cards = [
            card
            for card in self._visible_cards(handle)
            if str(card.order_no or "").strip() == str(order_no or "").strip()
        ]
        targets = self._detail_controls_for_order(handle, order_no)
        self.last_search_candidate_count = len(targets)
        if not cards or not targets:
            raise PddOrderNotFoundError(
                f"售后订单 {order_no} 的搜索结果没有可识别候选行"
            )
        if len(cards) != len(targets):
            raise PddUiaError(
                f"售后订单 {order_no} 搜索结果语义行与详情按钮数量不一致："
                f"行={len(cards)}，按钮={len(targets)}；已禁止按序号猜测"
            )

        summaries: list[dict[str, Any]] = []
        eligible: list[int] = []
        for index, card in enumerate(cards):
            status = "".join(str(card.status or "").split())
            shipping_status = "".join(str(card.shipping_status or "").split())
            refund_type = "".join(str(card.refund_type or "").split())
            refund_closed = "退款关闭" in status
            refund_succeeded = "退款成功" in status
            matches_branch = bool(
                shipping_status == "已发货"
                and refund_type == "退款"
                and not refund_closed
                and not refund_succeeded
            )
            if matches_branch:
                eligible.append(index)
            summaries.append(
                {
                    "candidate_index": index,
                    "aftersale_no": str(card.aftersale_no or "").strip(),
                    "shipping_status": str(card.shipping_status or ""),
                    "refund_type": str(card.refund_type or ""),
                    "aftersale_status": str(card.status or ""),
                    "refund_closed": refund_closed,
                    "refund_succeeded": refund_succeeded,
                    "ignored_terminal_status": (
                        "退款关闭" if refund_closed else "退款成功" if refund_succeeded else ""
                    ),
                    "eligible": matches_branch,
                }
            )
        self.last_search_candidate_rows = summaries

        if not eligible:
            matching_branch_rows = [
                row
                for row in summaries
                if "".join(str(row["shipping_status"]).split()) == "已发货"
                and "".join(str(row["refund_type"]).split()) == "退款"
            ]
            if matching_branch_rows and all(
                bool(row["refund_closed"] or row["refund_succeeded"])
                for row in matching_branch_rows
            ):
                terminal_statuses = "、".join(
                    dict.fromkeys(
                        str(row["ignored_terminal_status"])
                        for row in matching_branch_rows
                        if row["ignored_terminal_status"]
                    )
                )
                raise PddAftersaleSearchResultsClosed(
                    f"售后订单 {order_no} 搜索结果中的已发货退款均为"
                    f"{terminal_statuses or '已终结状态'}，已忽略"
                )
            raise PddAftersaleSearchResultsClosed(
                f"售后订单 {order_no} 搜索结果没有同时满足已发货、退款、"
                "未关闭且未退款成功的记录，状态已变化并忽略"
            )

        expected = str(expected_aftersale_no or "").strip()
        if expected:
            exact = [
                index
                for index in eligible
                if str(cards[index].aftersale_no or "").strip() == expected
            ]
            if len(exact) == 1:
                selected = exact[0]
                return selected, expected
            if len(exact) > 1:
                raise PddUiaError(
                    f"售后订单 {order_no} 的售后编号 {expected} "
                    "对应多条未关闭且未退款成功记录"
                )

        if len(eligible) != 1:
            raise PddUiaError(
                f"售后订单 {order_no} 搜索结果有 {len(eligible)} 条已发货退款且"
                "未关闭、未退款成功，"
                "无法唯一确定目标，已禁止点击"
            )
        selected = eligible[0]
        return selected, str(cards[selected].aftersale_no or "").strip()

    def _visible_detail_target(
        self,
        window: Any,
        handle: int,
        order_no: str,
        candidate_index: int,
    ) -> Any | None:
        """Bring the requested row-local 查看详情 control into the viewport.

        Searching the aftersale workbench does not scroll the result table into
        view.  UIA can still enumerate the matching row and its 查看详情 control,
        but ``click_input`` is unsafe while that control is below the visible
        client area.  Prefer UIA ScrollItem, then use one deterministic page
        scroll and re-resolve the control after every movement so a stale UIA
        wrapper is never clicked.
        """

        for _attempt in range(4):
            target = self._detail_target_for_order(handle, order_no, candidate_index)
            if target is None:
                return None
            target_rect = self._rect(target)
            if 90 <= target_rect[1] and target_rect[3] <= 1000:
                return target

            # UIA ScrollItem is the only allowed movement in processing.  A
            # global Ctrl+End/PageDown moves the document under fixed PDD
            # navigation and makes later screen rectangles unsafe.
            try:
                target.iface_scroll_item.ScrollIntoView()
                time.sleep(0.35)
                continue
            except Exception:
                return None
        return None

    def open_detail(
        self,
        shop: dict[str, Any],
        order_no: str,
        *,
        candidate_index: int = 0,
        search_value: str | None = None,
        expected_aftersale_no: str = "",
        require_shipped_refund_result: bool = False,
    ) -> tuple[Any, str]:
        exact_search_value = str(search_value or order_no).strip()
        expected_aftersale_no = str(expected_aftersale_no or "").strip()
        if not exact_search_value:
            raise PddUiaError("售后工作台搜索值不能为空")
        existing = self._activate_detail_by_order(shop, order_no)
        if existing is not None:
            # Candidate rows for one order can point at different aftersale
            # records.  Reusing an already-open page solely by order number
            # means candidate 2 can silently read candidate 1 again.  Clear
            # that stale page first, then reopen the requested row from the
            # retained list anchor.
            cleanup = self.close_detail(existing, shop)
            if not cleanup.get("handled"):
                raise PddUiaError(
                    f"售后订单 {order_no} 的旧详情页未能安全关闭，拒绝复用候选记录"
                )
        # Reuse an existing workbench list when present.  A completed scan can
        # legitimately restore this shop's sole tab to 工单管理; in that case
        # _list_window performs the established sidebar-element click to
        # 售后工作台 in the same tab.  It never opens a replacement window and
        # the processing stage still does not touch the pending quick-filter.
        window = self._list_window(shop, navigate=True)
        # The pending quick-filter belongs to the scan stage only.  Processing
        # already has an exact order number and must search it directly.  The
        # old re-confirmation clicked an already-selected toggle, opened an
        # extra workbench tab, and could leave the next order on a stale list
        # anchor.
        handle = int(window.handle)
        self._dismiss_overlay_popups(window)
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_LIST,
            stage="售后工作台处理前",
            require_ready=True,
        )
        search = None
        for _attempt in range(3):
            search = self._safe_order_search(window, handle, shop)
            value_written = self._set_search_value_with_uia(search, exact_search_value)
            if not value_written:
                raise PddUiaError(
                    "售后工作台搜索框不支持安全 UIA 输入；"
                    "已禁止鼠标坐兜底"
                )
            input_deadline = time.monotonic() + 2
            while time.monotonic() < input_deadline:
                try:
                    if (
                        str(self._safe_order_search(window, handle, shop).get_value())
                        == exact_search_value
                    ):
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            else:
                time.sleep(0.3)
                continue
            search = self._safe_order_search(window, handle, shop)
            break
        else:
            raise PddUiaError(
                f"售后工作台搜索框输入校验失败：{exact_search_value}"
            )

        assert search is not None
        try:
            button = self._order_search_button(handle, search)
        except PddUiaError:
            # A promotional layer can arrive while the Edit value triggers a
            # local render.  No search action has happened yet, so it is safe
            # to clear the layer, revalidate the page, and resolve the exact
            # value/button pair once more.
            self._dismiss_overlay_popups(window)
            self.assert_page_contract(
                window,
                shop,
                AFTERSALE_LIST,
                stage="售后订单搜索按钮恢复",
                require_ready=True,
            )
            search = self._safe_order_search(window, handle, shop)
            if str(search.get_value() or "") != exact_search_value:
                raise PddUiaError(
                    f"售后订单搜索按钮恢复后输入值发生变化：{exact_search_value}"
                )
            button = self._order_search_button(handle, search)
        if not self._invoke_with_uia(button):
            raise PddUiaError(
                "售后订单搜索按钮不支持安全 UIA 调用；"
                "已禁止鼠标坐兜底"
            )
        detail_requested = False
        detail_request_method = ""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            time.sleep(
                max(
                    0.15,
                    min(
                        float(self.config.get("aftersale_search_poll_seconds", 0.3)),
                        1.0,
                    ),
                )
            )
            self.assert_page_contract(
                window,
                shop,
                AFTERSALE_LIST,
                stage="售后工作台订单搜索结果",
                require_ready=True,
            )
            targets = self._detail_controls_for_order(handle, order_no)
            if not targets:
                continue
            if require_shipped_refund_result:
                candidate_index, selected_aftersale_no = (
                    self._select_shipped_refund_search_candidate(
                        handle,
                        order_no,
                        expected_aftersale_no,
                    )
                )
                if selected_aftersale_no:
                    expected_aftersale_no = selected_aftersale_no
                self.last_selected_aftersale_no = selected_aftersale_no
            target = self._detail_target_for_order(handle, order_no, candidate_index)
            if target is None:
                continue

            # A late promotional layer can arrive during the search-result
            # wait.  Clear it at the action boundary, then discard the old
            # target and resolve this exact order row again.
            self._dismiss_overlay_popups(window)
            self.assert_page_contract(
                window,
                shop,
                AFTERSALE_LIST,
                stage="售后查看详情前广告清理完成",
                require_ready=True,
            )
            target = self._detail_target_for_order(
                handle, order_no, candidate_index
            )
            if target is None:
                continue

            # 查看详情 is a real Hyperlink on PDD and exposes InvokePattern.
            # Prefer that semantic action: click_input is a physical desktop
            # click and can be swallowed when another window briefly takes
            # foreground focus.  Only if InvokePattern is unavailable do we
            # refocus, re-resolve the exact same row, and issue one fallback
            # physical click.  Never click twice after a successful Invoke.
            if self._invoke_with_uia(target):
                detail_request_method = "uia_invoke"
            else:
                self._focus(window)
                self._dismiss_overlay_popups(window)
                self.assert_page_contract(
                    window,
                    shop,
                    AFTERSALE_LIST,
                    stage="售后查看详情物理点击兜底前",
                    require_ready=True,
                )
                target = self._visible_detail_target(
                    window,
                    handle,
                    order_no,
                    candidate_index,
                )
                if target is None:
                    raise PddUiaError(
                        f"售后订单 {order_no} 的查看详情不支持 UIA Invoke，"
                        "且无法安全滚动到可见区域"
                    )
                target.click_input()
                detail_request_method = "single_physical_fallback"
            detail_requested = True
            break

        if not detail_requested:
            raise PddOrderNotFoundError(f"售后订单 {order_no} 当前已不在待处理列表")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            time.sleep(
                max(
                    0.15,
                    min(
                        float(self.config.get("aftersale_detail_open_poll_seconds", 0.35)),
                        1.0,
                    ),
                )
            )
            detail_window = self._activate_detail_by_order(shop, order_no)
            if detail_window is not None:
                detail_url = self._current_url(detail_window)
                self.assert_page_contract(
                    detail_window,
                    shop,
                    AFTERSALE_DETAIL,
                    order_no=order_no,
                    stage="售后详情打开完成",
                )
                if expected_aftersale_no:
                    semantic_values = [detail_url]
                    for control in self._controls(int(detail_window.handle)):
                        semantic_values.extend(self._control_semantic_values(control))
                    actual_aftersale_no = self._aftersale_no_from_values(
                        semantic_values
                    )
                    if actual_aftersale_no != expected_aftersale_no:
                        actual_label = actual_aftersale_no or "无法读取"
                        cleanup = self.close_detail(detail_window, shop)
                        cleanup_handled = bool(cleanup.get("handled")) or (
                            bool(cleanup.get("list_ready"))
                            and bool(cleanup.get("closed") or cleanup.get("navigated"))
                        )
                        if not cleanup_handled:
                            raise PddUiaError(
                                "售后编号校验失败且错误详情页未能安全关闭："
                                f"期望={expected_aftersale_no}，实际={actual_label}"
                            )
                        raise PddUiaError(
                            "售后编号校验失败，已关闭错误详情页："
                            f"期望={expected_aftersale_no}，实际={actual_label}"
                        )
                return detail_window, detail_url
        raise PddUiaError(
            f"售后订单 {order_no} 通过 {detail_request_method} 打开查看详情后页面未稳定"
        )

    def _activate_detail_by_order(self, shop: dict[str, Any], order_no: str) -> Any | None:
        matches = []
        for window in self._shop_windows(shop):
            attempts = max(1, min(int(self.config.get("tab_cleanup_attempts", 20)), 30))
            seen: set[tuple[str, bool]] = set()
            for _ in range(attempts + 1):
                url = self._current_url(window)
                exact_order = self._has_exact_name(int(window.handle), order_no)
                signature = (url, exact_order)
                if signature in seen:
                    break
                seen.add(signature)
                if classify_aftersale_url(url) == "aftersale_detail" and exact_order:
                    matches.append(window)
                    break
                self._focus(window)
                self.keyboard.send_keys("^{TAB}")
                time.sleep(float(self.config.get("tab_switch_wait_seconds", 0.35)))
        unique = {int(item.handle): item for item in matches}
        if len(unique) > 1:
            raise PddUiaError(f"售后订单 {order_no} 的详情窗口不唯一：{len(unique)}")
        if unique:
            window = next(iter(unique.values()))
            self._focus(window)
            return window
        return None

    def activate_detail(self, shop: dict[str, Any], expected_url: str, order_no: str) -> Any:
        windows = self._shop_windows(shop)
        matches = []
        for window in windows:
            attempts = max(1, min(int(self.config.get("tab_switch_attempts", 8)), 20))
            seen: set[str] = set()
            for _ in range(attempts + 1):
                url = self._current_url(window)
                if url == expected_url and self._has_exact_name(int(window.handle), order_no):
                    matches.append(window)
                    break
                if url in seen:
                    break
                seen.add(url)
                self._focus(window)
                self.keyboard.send_keys("^{TAB}")
                time.sleep(float(self.config.get("tab_switch_wait_seconds", 0.35)))
        unique = {int(item.handle): item for item in matches}
        if len(unique) != 1:
            raise PddUiaError(f"售后订单 {order_no} 的详情页不唯一：{len(unique)}")
        window = next(iter(unique.values()))
        self._focus(window)
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_DETAIL,
            order_no=order_no,
            stage="激活售后详情",
        )
        return window

    @staticmethod
    def _value_right_of_label(
        controls: list[Any], label_text: str, *, allowed: set[str] | None = None
    ) -> str:
        labels = []
        for item in controls:
            try:
                name = str(item.element_info.name or "").strip()
                if name == label_text:
                    labels.append(item)
            except Exception:
                continue
        for label in labels:
            rect = label.rectangle()
            values = []
            for item in controls:
                try:
                    name = str(item.element_info.name or "").strip()
                    item_rect = item.rectangle()
                except Exception:
                    continue
                if item_rect.left < rect.right - 4 or abs(item_rect.top - rect.top) > 7:
                    continue
                if allowed is not None and name not in allowed:
                    continue
                if name and name != label_text:
                    values.append((item_rect.left, name))
            if values:
                return min(values)[1]
        return ""

    @classmethod
    def _first_value_right_of_labels(
        cls,
        controls: list[Any],
        labels: tuple[str, ...],
        *,
        allowed: set[str] | None = None,
    ) -> str:
        for label in labels:
            value = cls._value_right_of_label(controls, label, allowed=allowed)
            if value:
                return value
        return ""

    @classmethod
    def _detail_aftersale_status(cls, controls: list[Any]) -> str:
        labelled = cls._first_value_right_of_labels(
            controls,
            ("售后状态：", "处理状态："),
        )
        if labelled:
            return labelled
        names = []
        for control in controls:
            try:
                name = cls._name(control)
            except Exception:
                continue
            if name:
                names.append(name)
        for status in AFTERSALE_STATUS_NAMES:
            if status in names:
                return status
        # PDD sometimes exposes the right-side heading as one sentence, e.g.
        # “买家已发货，待商家处理”. Prefer the actionable suffix.
        for status in AFTERSALE_STATUS_NAMES:
            if any(status in name for name in names):
                return status
        return ""

    @classmethod
    def _detail_apply_time(cls, controls: list[Any]) -> str:
        labelled = cls._first_value_right_of_labels(
            controls,
            ("申请时间：", "售后申请时间：", "申请售后时间："),
        )
        matched = DATETIME_RE.search(labelled)
        if matched:
            return matched.group(1)
        for control in controls:
            try:
                name = cls._name(control)
            except Exception:
                continue
            if "申请时间" not in name and "申请售后时间" not in name:
                continue
            matched = DATETIME_RE.search(name)
            if matched:
                return matched.group(1)
        return ""

    @staticmethod
    def _normalized_action_text(value: Any) -> str:
        """Normalize UIA text without weakening the business meaning.

        Chromium may expose one Chinese label with spaces, newlines or NBSPs.
        Punctuation also varies in the platform's cooldown prompt.  Removing
        only layout whitespace and punctuation keeps exact action names and
        the three required cooldown clauses deterministic.
        """

        return re.sub(
            r"[\s\u00a0，,。；;：:！!、]+",
            "",
            str(value or ""),
        )

    @classmethod
    def _control_is_live_action(cls, control: Any) -> bool:
        try:
            control_type = str(control.element_info.control_type or "")
            if control_type not in ACTIONABLE_CONTROL_TYPES:
                return False
            if hasattr(control, "is_visible") and not bool(control.is_visible()):
                return False
            if hasattr(control, "is_enabled") and not bool(control.is_enabled()):
                return False
            rect = control.rectangle()
            width = rect.width() if callable(getattr(rect, "width", None)) else rect.right - rect.left
            height = rect.height() if callable(getattr(rect, "height", None)) else rect.bottom - rect.top
            return width > 0 and height > 0
        except Exception:
            return False

    @staticmethod
    def _control_is_visible(control: Any) -> bool:
        try:
            if hasattr(control, "is_visible") and not bool(control.is_visible()):
                return False
            rect = control.rectangle()
            width = rect.width() if callable(getattr(rect, "width", None)) else rect.right - rect.left
            height = rect.height() if callable(getattr(rect, "height", None)) else rect.bottom - rect.top
            return width > 0 and height > 0
        except Exception:
            return False

    @staticmethod
    def _control_supports_invoke(control: Any) -> bool:
        """Return whether UIA exposes a real InvokePattern for this control."""

        try:
            control.iface_invoke
            return True
        except Exception:
            return False

    @classmethod
    def _control_is_tight_invokable_wrapper(
        cls, control: Any, label: Any
    ) -> bool:
        """Accept a small invokable wrapper around one exact action label.

        PDD's underlined “驳回退款” is exposed by Chromium as a Text child of
        a Group that owns InvokePattern.  The wrapper is safe only when it is
        visible/enabled, tightly contains the exact label, and is not a broad
        page/panel ancestor.  This keeps coordinate guessing and history-text
        promotion out of the destructive path.
        """

        try:
            control_type = str(control.element_info.control_type or "")
            if control_type not in {"Text", "Group", "Custom"}:
                return False
            if hasattr(control, "is_visible") and not bool(control.is_visible()):
                return False
            if hasattr(control, "is_enabled") and not bool(control.is_enabled()):
                return False
            if not cls._control_supports_invoke(control):
                return False
            control_rect = cls._rect(control)
            label_rect = cls._rect(label)
            if not (
                control_rect[0] <= label_rect[0]
                and control_rect[1] <= label_rect[1]
                and control_rect[2] >= label_rect[2]
                and control_rect[3] >= label_rect[3]
            ):
                return False
            control_width = control_rect[2] - control_rect[0]
            control_height = control_rect[3] - control_rect[1]
            label_width = label_rect[2] - label_rect[0]
            label_height = label_rect[3] - label_rect[1]
            if min(control_width, control_height, label_width, label_height) <= 0:
                return False
            return (
                control_width <= label_width + 64
                and control_height <= label_height + 40
            )
        except Exception:
            return False

    def _action_capabilities_from_controls(
        self, controls: list[Any]
    ) -> dict[str, Any]:
        """Classify actions from one immutable UIA snapshot.

        Timeline/history text can contain the same words as a destructive
        action.  It is deliberately excluded by requiring a live actionable
        UIA control.  Duplicate destructive controls fail closed because a
        unique target cannot be proven.
        """

        # Chromium sometimes exposes an underlined action label as a Text
        # child while its parent owns the actual Invoke pattern.  Use the same
        # exact-label -> live actionable ancestor resolver used at execution
        # time, so collection cannot hide an action that execution can safely
        # invoke.  History/help Text controls still have no actionable parent
        # and therefore cannot be promoted.
        by_action: dict[str, list[Any]] = {
            action: self._action_targets(0, text, controls=controls)
            for action, text in AFTERSALE_ACTION_TEXT.items()
        }
        duplicates = {
            action: len(matches)
            for action, matches in by_action.items()
            if len(matches) > 1
        }
        if duplicates:
            return {
                "action_controls_read_complete": False,
                "available_aftersale_actions": [],
                "has_agree_refund": False,
                "has_agree_return_after_refund": False,
                "has_agree_recall_after_refund": False,
                "has_reject_refund": False,
                "action_control_error": f"当前售后动作控件不唯一：{duplicates}",
            }
        available = [
            action for action, matches in by_action.items() if len(matches) == 1
        ]
        return {
            "action_controls_read_complete": True,
            "available_aftersale_actions": available,
            "has_agree_refund": AGREE_REFUND in available,
            "has_agree_return_after_refund": AGREE_RETURN_AFTER_REFUND in available,
            "has_agree_recall_after_refund": AGREE_RECALL_AFTER_REFUND in available,
            "has_reject_refund": REJECT_REFUND in available,
        }

    def _read_action_capabilities(self, handle: int) -> dict[str, Any]:
        """Read only the current, enabled action buttons on the detail page."""

        return self._action_capabilities_from_controls(self._controls(handle))

    def _detect_reject_cooldown(self, controls: list[Any]) -> str:
        """Return the complete platform cooldown prompt, otherwise ``""``."""

        values = [
            str(self._name(control) or "").strip()
            for control in controls
            if str(self._name(control) or "").strip()
        ]
        combined = "\n".join(values)
        normalized = self._normalized_action_text(combined)
        required = (
            "请耐心等待消费者回复",
            "待倒计时结束",
            "可继续处理驳回",
        )
        if all(self._normalized_action_text(item) in normalized for item in required):
            matching = [
                value
                for value in values
                if any(
                    self._normalized_action_text(fragment)
                    in self._normalized_action_text(value)
                    for fragment in required
                )
            ]
            return "；".join(matching) or combined
        return ""

    def _detect_active_page_reject_cooldown(self, controls: list[Any]) -> str:
        """Recognize the live 30-minute page banner, not historical timeline text."""

        visible_values = [
            str(self._name(control) or "").strip()
            for control in controls
            if self._control_is_visible(control)
            and str(self._name(control) or "").strip()
        ]
        raw_text = " ".join(visible_values)
        normalized = self._normalized_action_text(raw_text)
        if not (
            "售后协商中" in normalized
            and "消费者未回复" in normalized
            and (
                "继续处理驳回退款" in normalized
                or "继续处理驳回" in normalized
            )
        ):
            return ""
        timer = re.search(r"\b\d{2}\s*:\s*\d{2}\s*:\s*\d{2}\b", raw_text)
        if timer is None:
            # Historical progress text can contain the same prose.  A live
            # countdown is the discriminator for a currently blocked action.
            return ""
        remaining = re.sub(r"\s+", "", timer.group(0))
        return (
            "售后协商中，消费者未回复后可继续处理驳回退款"
            f"（剩余 {remaining}）"
        )

    def _classify_reject_surface(self, controls: list[Any]) -> str:
        """Classify an observed reject stage, including nested negotiation."""

        if self._detect_reject_cooldown(controls):
            return "COOLDOWN"
        normalized = [
            self._normalized_action_text(self._name(control)) for control in controls
        ]
        combined = "".join(normalized)
        # Clicking ``坚持驳回`` does not select a value in the original
        # dialog.  PDD advances to a second (and on some versions an
        # intermediate third) negotiation stage.  Recognize that stage before
        # requiring the ordinary exact ``提交`` button; the real final button
        # here is ``提交协商方案``.
        negotiation_submit = any(value == "提交协商方案" for value in normalized)
        negotiation_title = "请与消费者协商方案" in combined
        full_refund = any("退货后退全款" in value for value in normalized)
        if negotiation_submit and negotiation_title and full_refund:
            return "KEEP_REJECT_NEGOTIATION"
        # 2026-08-27 real shop variant: after ``坚持驳回`` PDD first
        # presents an explanatory ``驳回退款流程`` modal. ``去协商`` is the
        # only forward business action; ``进一步了解`` opens help content.
        reject_process = "驳回退款流程" in combined
        go_negotiate = any(value == "去协商" for value in normalized)
        if reject_process and go_negotiate and not negotiation_submit:
            return "KEEP_REJECT_PROCESS_ENTRY"
        negotiation_entry = any(value == "与消费者协商方案" for value in normalized)
        if negotiation_entry and not negotiation_submit:
            return "KEEP_REJECT_NEGOTIATION_ENTRY"

        has_submit = any(value == "提交" for value in normalized)
        if not has_submit:
            return "UNKNOWN"
        keep_reject = any(value == "坚持驳回" for value in normalized)
        amount_not_agreed = any(
            "退款金额未达成一致" in value for value in normalized
        )
        consumer_truth = any(
            "消费者描述属实但不认可消费者提出的售后方案" in value
            for value in normalized
        )
        # ``纠纷率低`` is explanatory copy, not a required action or a stable
        # classification marker.  The business option itself is sufficient.
        # Real final-reject form (verified on 2026-08-27): selecting the
        # truthful-description reason expands a second section.  This branch
        # must choose ``不协商，直接驳回`` and fill the required
        # reject description.  ``驳回并与消费者协商`` and
        # ``退货后退全款`` belong to a different negotiation path and
        # are deliberately not classification requirements here.
        direct_reject = (
            consumer_truth
            and "不协商直接驳回" in combined
            and "驳回描述" in combined
        )
        matches = [
            name
            for name, matched in (
                ("KEEP_REJECT", keep_reject),
                ("AMOUNT_NOT_AGREED", amount_not_agreed),
                ("DIRECT_REJECT", direct_reject),
            )
            if matched
        ]
        if len(matches) == 1:
            return matches[0]
        return "AMBIGUOUS" if len(matches) > 1 else "UNKNOWN"

    def _infer_progressive_reject_surface(
        self,
        handle: int,
        controls: list[Any],
        *,
        scope: _StrictModalScope | None = None,
    ) -> str:
        """Recognize a unique first option before nested fields are rendered."""

        candidates = [
            name
            for name, text, contains in (
                ("KEEP_REJECT", "坚持驳回", False),
                ("AMOUNT_NOT_AGREED", "退款金额未达成一致", True),
                (
                    "DIRECT_REJECT",
                    "消费者描述属实，但不认可消费者提出的售后方案",
                    True,
                ),
            )
            if len(
                self._action_targets(
                    handle,
                    text,
                    contains=contains,
                    controls=controls,
                    scope=scope,
                )
            )
            == 1
        ]
        return candidates[0] if len(candidates) == 1 else "UNKNOWN"

    def read_detail(
        self,
        window: Any,
        order_no: str,
        detail_url: str,
        shop: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._current_url(window) != detail_url:
            raise PddUiaError("售后详情URL在读取前发生变化")
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_DETAIL,
            order_no=order_no,
            stage="读取售后详情",
        )
        handle = int(window.handle)
        self._focus(window)
        self.keyboard.send_keys("^{HOME}")
        time.sleep(0.35)
        controls = self._controls(handle)
        order_count = sum(self._name(item) == order_no for item in controls)
        url_orders = [
            str(item or "").strip()
            for item in parse_qs(urlparse(detail_url).query).get("orderSn", [])
            if str(item or "").strip()
        ]
        # Chromium can expose the same visible order field twice after the
        # detail page inserts a dynamic aftersale card.  This method used to
        # contradict assert_page_contract(), which already accepts duplicate
        # equal UIA nodes only when the immutable detail URL independently
        # binds this exact order.  Apply that same contract to the fresh tree
        # read after Ctrl+Home; missing/wrong URL identity still fails closed.
        url_order_verified = url_orders == [order_no]
        if order_count < 1 or (order_count != 1 and not url_order_verified):
            raise PddUiaError(
                "售后详情订单身份无法唯一确认："
                f"页面精确匹配数={order_count}，URL订单核验={url_order_verified}"
            )
        refund_type = self._value_right_of_label(
            controls, "售后类型：", allowed={"退款", "退货退款"}
        )
        shipping_status = self._value_right_of_label(controls, "发货状态：")
        refund_reason = self._value_right_of_label(controls, "申请原因：")
        carrier = self._value_right_of_label(controls, "发货物流公司：")
        tracking_no = self._value_right_of_label(controls, "发货快递单号：")
        if not TRACKING_RE.fullmatch(tracking_no):
            tracking_no = ""
        return_carrier = self._first_value_right_of_labels(
            controls,
            ("退货物流公司：", "退货快递公司："),
        )
        return_tracking_no = self._first_value_right_of_labels(
            controls,
            ("退货快递单号：", "退货物流单号：", "退货单号："),
        )
        if not TRACKING_RE.fullmatch(return_tracking_no):
            return_tracking_no = ""
        aftersale_description = self._first_value_right_of_labels(
            controls,
            (
                "售后申请说明：",
                "申请说明：",
                "申请描述：",
                "问题描述：",
                "买家描述：",
                "消费者描述：",
            ),
        )
        aftersale_values = [detail_url]
        for control in controls:
            aftersale_values.extend(self._control_semantic_values(control))
        aftersale_no = self._aftersale_no_from_values(aftersale_values)
        aftersale_status = self._current_aftersale_status_from_controls(controls)
        aftersale_apply_time = self._detail_apply_time(controls)
        amount = 0.0
        labels = [c for c in controls if self._name(c) == "退款金额："]
        for label in labels:
            rect = label.rectangle()
            candidates = []
            for item in controls:
                try:
                    name = self._name(item).replace("¥", "").strip()
                    item_rect = item.rectangle()
                except Exception:
                    continue
                if item_rect.left >= rect.right - 5 and abs(item_rect.top - rect.top) <= 7:
                    if re.fullmatch(r"\d+(?:\.\d{1,2})?", name):
                        candidates.append(float(name))
            if len(candidates) == 1:
                amount = candidates[0]
                break

        has_extend_receipt = any(self._name(c) == "延长收货" for c in controls)
        action_capabilities = self._action_capabilities_from_controls(controls)
        self._focus(window)
        self.keyboard.send_keys("^{HOME}")
        return {
            "order_no": order_no,
            "detail_url": detail_url,
            "aftersale_no": aftersale_no,
            "aftersale_status": aftersale_status,
            "aftersale_apply_time": aftersale_apply_time,
            "aftersale_description": aftersale_description,
            "refund_type": refund_type,
            "shipping_status": shipping_status,
            "refund_reason": refund_reason,
            "refund_amount": amount,
            "carrier": carrier,
            "tracking_no": tracking_no,
            "return_carrier": return_carrier,
            "return_tracking_no": return_tracking_no,
            "has_extend_receipt": has_extend_receipt,
            **action_capabilities,
            "engine": "edge_uia",
            "business_write_performed": False,
        }

    def _logistics_view_all_targets(self, handle: int) -> list[Any]:
        controls = self._controls(handle)
        headers = [
            control
            for control in controls
            if self._name(control).strip() == "物流轨迹"
        ]
        if len(headers) != 1:
            raise PddUiaError(f"售后详情物流轨迹标题不唯一：{len(headers)}")
        header_rect = self._rect(headers[0])
        matches: dict[tuple[int, int, int, int], Any] = {}
        for control in controls:
            try:
                name = self._name(control).strip()
                rect = self._rect(control)
            except Exception:
                continue
            if (
                (name == "查看全部" or name.startswith("查看全部物流"))
                and rect[1] > header_rect[1]
                and header_rect[0] - 80 <= rect[0] <= header_rect[0] + 520
            ):
                matches[rect] = control
        return [matches[key] for key in sorted(matches, key=lambda item: (item[1], item[0]))]

    def _read_expanded_logistics(self, handle: int) -> dict[str, Any]:
        controls = self._controls(handle)
        headers = [
            control
            for control in controls
            if self._name(control).strip() == "物流轨迹"
        ]
        if len(headers) != 1:
            raise PddUiaError(f"售后详情物流轨迹标题不唯一：{len(headers)}")
        header_rect = self._rect(headers[0])
        next_section_tops: list[int] = []
        for control in controls:
            try:
                name = self._name(control).strip().rstrip("：:")
                rect = self._rect(control)
            except Exception:
                continue
            if name in {"备注", "订单备注"} and rect[1] > header_rect[1] and 300 <= rect[0] <= 850:
                next_section_tops.append(rect[1])
        section_bottom = min(next_section_tops) if next_section_tops else 10**9
        records: list[dict[str, Any]] = []
        seen: set[tuple[str, tuple[int, int, int, int]]] = set()
        for control in controls:
            try:
                name = self._name(control).strip()
                rect = self._rect(control)
            except Exception:
                continue
            if (
                not name
                or rect[1] <= header_rect[1]
                or rect[1] >= section_bottom
                or rect[0] < 320
                or rect[0] > 850
            ):
                continue
            key = (name, rect)
            if key in seen:
                continue
            seen.add(key)
            records.append({"name": name, "rect": rect})
        records.sort(key=lambda item: (item["rect"][1], item["rect"][0], item["name"]))

        timestamp_records = [
            record for record in records if LOGISTICS_TIMESTAMP_RE.search(record["name"])
        ]
        event_records = [
            record
            for record in records
            if not LOGISTICS_TIMESTAMP_RE.search(record["name"])
            and record["name"] not in LOGISTICS_NON_EVENT_TEXTS
            and not re.fullmatch(r"[\d\s-]{8,}", record["name"])
            and "订单分" not in record["name"]
            and not record["name"].endswith(("：", ":"))
        ]
        trace_rows: list[dict[str, str]] = []
        for timestamp_record in timestamp_records:
            match = LOGISTICS_TIMESTAMP_RE.search(timestamp_record["name"])
            if match is None:
                continue
            timestamp_top = int(timestamp_record["rect"][1])
            timestamp_left = int(timestamp_record["rect"][0])
            candidates = [
                record
                for record in event_records
                if 0 < timestamp_top - int(record["rect"][1]) <= 120
                and abs(int(record["rect"][0]) - timestamp_left) <= 180
            ]
            if not candidates:
                raise PddUiaError(
                    f"物流轨迹时间 {match.group(0)} 没有匹配到对应轨迹内容"
                )
            event = max(candidates, key=lambda item: int(item["rect"][1]))
            trace_rows.append({"timestamp": match.group(0), "event": event["name"]})
        unique_rows = {
            (row["timestamp"], row["event"]): row for row in trace_rows
        }
        trace_rows = list(unique_rows.values())
        trace_rows.sort(key=lambda item: item["timestamp"], reverse=True)
        if len(trace_rows) != len(timestamp_records):
            raise PddUiaError(
                "物流轨迹时间与内容未能一一对应，禁止按不完整条数自动退款"
            )
        latest = trace_rows[0]["event"] if trace_rows else ""
        signed = bool(
            re.search(
                r"(?<!未)(?:已签收|签收成功|本人签收|他人代收|驿站代收)",
                latest,
            )
        )
        return {
            "logistics_read_complete": True,
            "logistics_trajectory_count": len(trace_rows),
            "logistics_trace_rows": trace_rows,
            "latest_logistics_text": latest,
            "logistics_latest_signed": signed,
        }

    def read_logistics_detail(
        self,
        window: Any,
        order_no: str,
        detail_url: str,
        shop: dict[str, Any],
    ) -> dict[str, Any]:
        """Expand every logistics segment and return a fail-closed exact count."""

        if self._current_url(window) != detail_url:
            raise PddUiaError("售后详情URL在物流读取前发生变化")
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_DETAIL,
            order_no=order_no,
            stage="读取普通退款完整物流轨迹",
        )
        handle = int(window.handle)
        self._focus(window)
        self.keyboard.send_keys("^{END}")
        time.sleep(
            max(
                0.15,
                min(
                    float(self.config.get("aftersale_scroll_settle_seconds", 0.35)),
                    1.0,
                ),
            )
        )
        initial_targets = self._logistics_view_all_targets(handle)
        if not initial_targets:
            # PDD can retain an expanded segment state when the exact detail
            # tab is revisited. In that state there is no 查看全部 control, but
            # the full timestamp/event rows are already present. Accept only
            # a successfully paired non-empty full list; an empty/loading
            # section still fails closed.
            already_expanded = self._read_expanded_logistics(handle)
            if int(already_expanded.get("logistics_trajectory_count") or 0) <= 0:
                raise PddUiaError(
                    "普通退款详情既没有物流轨迹“查看全部”，也没有可核验的完整轨迹"
                )
            already_expanded["logistics_expanded_segments"] = 0
            already_expanded["logistics_already_expanded"] = True
            return already_expanded
        expanded_segments = 0
        for _ in range(min(len(initial_targets), 8)):
            remaining = self._logistics_view_all_targets(handle)
            if not remaining:
                break
            target = remaining[0]
            try:
                target.iface_scroll_item.ScrollIntoView()
                time.sleep(0.35)
            except Exception:
                pass
            if not self._invoke_with_uia(target):
                raise PddUiaError("物流轨迹“查看全部”不支持安全 UIA Invoke")
            expanded_segments += 1
            # The next loop rebuilds the UIA tree and therefore already acts
            # as the render probe.  A short settle is enough when the segment
            # is ready; the existing final completeness check still fails
            # closed if any “查看全部” remains.
            time.sleep(
                max(
                    0.15,
                    min(
                        float(
                            self.config.get(
                                "aftersale_logistics_expand_settle_seconds", 0.35
                            )
                        ),
                        1.0,
                    ),
                )
            )
        if self._logistics_view_all_targets(handle):
            raise PddUiaError("物流轨迹仍有未展开分段，禁止按不完整条数自动退款")
        result = self._read_expanded_logistics(handle)
        result["logistics_expanded_segments"] = expanded_segments
        return result

    @staticmethod
    def _rect_contains(
        outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]
    ) -> bool:
        return (
            outer[0] <= inner[0]
            and outer[1] <= inner[1]
            and outer[2] >= inner[2]
            and outer[3] >= inner[3]
        )

    @staticmethod
    def _control_runtime_signature(control: Any) -> tuple[Any, ...]:
        """Return an identity suitable for comparing freshly wrapped UIA nodes.

        UIA runtime ids distinguish overlapping DOM nodes that have identical
        class names and rectangles.  Real pywinauto UIA elements expose them.
        The object-id fallback exists for small in-memory test doubles; if a
        real provider omits runtime ids, a re-wrap changes the fallback and the
        final-submit checks fail closed.
        """

        try:
            runtime_id = getattr(control.element_info, "runtime_id", 0)
            if callable(runtime_id):
                runtime_id = runtime_id()
            if runtime_id and runtime_id != 0:
                if isinstance(runtime_id, (list, tuple)):
                    value = tuple(runtime_id)
                else:
                    value = (runtime_id,)
                return ("uia_runtime", *value)
        except Exception:
            pass
        return ("wrapper_object", id(control))

    def _control_is_descendant_of_scope(
        self, control: Any, scope: _StrictModalScope
    ) -> bool:
        """Prove that ``control`` is the root or a real descendant of it."""

        current = control
        seen: set[tuple[Any, ...]] = set()
        for _depth in range(20):
            if current is None:
                return False
            signature = self._control_runtime_signature(current)
            if signature == scope.root_signature:
                return True
            if signature in seen:
                return False
            seen.add(signature)
            try:
                current = current.parent()
            except Exception:
                return False
        return False

    def _controls_in_modal_scope(
        self,
        controls: list[Any],
        scope: _StrictModalScope,
        *,
        visible_only: bool = True,
    ) -> list[Any]:
        """Return only true descendants of one strict modal root."""

        result: list[Any] = []
        for control in controls:
            if visible_only and not self._control_is_visible(control):
                continue
            if self._control_is_descendant_of_scope(control, scope):
                result.append(control)
        return result

    def _action_targets(
        self,
        handle: int,
        text: str,
        *,
        contains: bool = False,
        controls: list[Any] | None = None,
        scope: _StrictModalScope | None = None,
        below_top: int | None = None,
    ) -> list[Any]:
        """Resolve a unique live UIA action, optionally inside one dialog."""

        expected = self._normalized_action_text(text)
        controls = list(controls) if controls is not None else self._controls(handle)
        matches: dict[tuple[int, int, int, int], Any] = {}
        for label in controls:
            if scope is not None and not self._control_is_descendant_of_scope(
                label, scope
            ):
                continue
            try:
                name = self._normalized_action_text(self._name(label))
            except Exception:
                continue
            if not ((expected in name) if contains else (expected == name)):
                continue
            candidate = label
            for _depth in range(4):
                if candidate is None:
                    break
                if self._control_is_live_action(
                    candidate
                ) or self._control_is_tight_invokable_wrapper(candidate, label):
                    break
                if scope is not None and not self._control_is_descendant_of_scope(
                    candidate, scope
                ):
                    candidate = None
                    break
                try:
                    candidate = candidate.parent()
                except Exception:
                    candidate = None
                    break
            if candidate is None or not (
                self._control_is_live_action(candidate)
                or self._control_is_tight_invokable_wrapper(candidate, label)
            ):
                continue
            if scope is not None and not self._control_is_descendant_of_scope(
                candidate, scope
            ):
                continue
            try:
                rect = self._rect(candidate)
            except Exception:
                continue
            if below_top is not None and rect[1] < below_top - 30:
                continue
            matches[rect] = candidate
        return [
            matches[key] for key in sorted(matches, key=lambda item: (item[1], item[0]))
        ]

    def _dialog_scope_from_anchor(
        self, anchor: Any, controls: list[Any]
    ) -> _StrictModalScope | None:
        """Return only a strict web-modal root, never the browser document."""

        try:
            anchor_rect = self._rect(anchor)
        except Exception:
            return None
        current = anchor
        candidates: list[tuple[int, int, _StrictModalScope]] = []
        for depth in range(7):
            try:
                current = current.parent()
            except Exception:
                break
            if current is None:
                break
            try:
                control_type = str(current.element_info.control_type or "")
                class_name = str(
                    getattr(current.element_info, "class_name", "") or ""
                )
                automation_id = str(
                    getattr(current.element_info, "automation_id", "") or ""
                )
                rect = self._rect(current)
            except Exception:
                continue
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            identity = f"{class_name} {automation_id}".lower()
            modal_identity = any(
                marker in identity
                for marker in (
                    "mdl_inner",
                    "modal",
                    "dialog",
                    "popup",
                )
            )
            if (
                control_type in {"Window", "Pane", "Group", "Custom"}
                and modal_identity
                and width >= 320
                and height >= 160
                and width <= 1500
                and height <= 900
                and self._rect_contains(rect, anchor_rect)
            ):
                scope = _StrictModalScope(
                    root=current,
                    root_signature=self._control_runtime_signature(current),
                    rect=rect,
                )
                candidates.append((depth, width * height, scope))
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: (item[0], item[1]),
        )[2]

    def _reject_modal_snapshot(
        self, handle: int
    ) -> tuple[_StrictModalScope, list[Any]] | None:
        """Return one visible reject/cooldown MDL modal and only its controls."""

        controls = self._controls(handle)
        anchor_fragments = tuple(
            self._normalized_action_text(item)
            for item in (
                "坚持驳回",
                "驳回退款流程",
                "去协商",
                "退款金额未达成一致",
                "消费者描述属实，但不认可消费者提出的售后方案",
                "驳回并与消费者协商",
                "与消费者协商方案",
                "请与消费者协商方案",
                "与消费者协商售后方案",
                "退货后退全款",
                "提交协商方案",
                "请耐心等待消费者回复",
                "待倒计时结束",
                "可继续处理驳回",
            )
        )
        scopes: dict[tuple[Any, ...], _StrictModalScope] = {}
        for control in controls:
            if not self._control_is_visible(control):
                continue
            try:
                name = self._normalized_action_text(self._name(control))
            except Exception:
                continue
            if not any(fragment in name for fragment in anchor_fragments):
                continue
            scope = self._dialog_scope_from_anchor(control, controls)
            if scope is None:
                continue
            scopes.setdefault(scope.root_signature, scope)
        valid: list[tuple[_StrictModalScope, list[Any]]] = []
        for scope in scopes.values():
            scoped = self._controls_in_modal_scope(controls, scope)
            if self._detect_reject_cooldown(scoped):
                valid.append((scope, scoped))
                continue
            surface = self._classify_reject_surface(scoped)
            progressive = self._infer_progressive_reject_surface(
                handle, scoped, scope=scope
            )
            if surface not in {"UNKNOWN", "AMBIGUOUS"} or progressive != "UNKNOWN":
                valid.append((scope, scoped))
        if len(valid) != 1:
            return None
        return valid[0]

    def _wait_for_reject_modal_snapshot(
        self,
        handle: int,
        *,
        timeout_seconds: float = 8,
        expected_root_signature: tuple[Any, ...] | None = None,
    ) -> tuple[_StrictModalScope, list[Any]] | None:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            snapshot = self._reject_modal_snapshot(handle)
            if snapshot is not None and (
                expected_root_signature is None
                or snapshot[0].root_signature == expected_root_signature
            ):
                return snapshot
            time.sleep(0.2)
        return None

    def _reject_surface_from_snapshot(
        self,
        handle: int,
        snapshot: tuple[_StrictModalScope, list[Any]],
    ) -> str:
        scope, controls = snapshot
        surface = self._classify_reject_surface(controls)
        if surface == "UNKNOWN":
            surface = self._infer_progressive_reject_surface(
                handle, controls, scope=scope
            )
        return surface

    def _wait_for_reject_surface(
        self,
        handle: int,
        allowed: set[str],
        *,
        timeout_seconds: float = 8,
    ) -> tuple[str, _StrictModalScope, list[Any]] | None:
        """Wait for one exact reject stage; a stage transition may replace root."""

        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            snapshot = self._reject_modal_snapshot(handle)
            if snapshot is not None:
                surface = self._reject_surface_from_snapshot(handle, snapshot)
                if surface in allowed:
                    return surface, snapshot[0], snapshot[1]
            time.sleep(0.2)
        return None

    def _wait_for_reject_post_submit_outcome(
        self,
        window: Any,
        *,
        baseline_status: str,
        order_no: str,
        detail_url: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        """Read either a stable final status or the exact negotiation cooldown.

        The complete cooldown copy is an authoritative acknowledgement only
        when it appears *after* this worker clicked the final negotiation
        submit button for the same URL and order.  A partial toast, identity
        change, or silence remains an unknown write outcome.
        """

        timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(self.config.get("aftersale_reject_submit_wait_seconds", 12))
        )
        deadline = time.monotonic() + max(0.1, timeout)
        terminal_key: tuple[str, str] | None = None
        terminal_reads = 0
        handle = int(window.handle)
        while time.monotonic() < deadline:
            current_url = self._current_url(window)
            if current_url != detail_url:
                return None
            controls = self._controls(handle)
            # Chromium can expose the order number twice after the negotiation
            # card is inserted (the visible detail field plus a duplicated
            # accessibility node).  The exact unchanged detail URL is the
            # primary identity contract; require only that either the URL or
            # at least one live UIA node still carries this order number.
            if (
                order_no not in current_url
                and not any(self._name(control) == order_no for control in controls)
            ):
                return None
            snapshot = self._reject_modal_snapshot(handle)
            if snapshot is not None:
                prompt = self._detect_reject_cooldown(snapshot[1])
                if prompt:
                    return {"kind": "cooldown", "prompt_text": prompt}
            # The real amount-negotiation branch closes its modal after
            # submission and renders the 30-minute acknowledgement in the
            # detail page itself. It is an authoritative write receipt only
            # while the exact order and URL above remain unchanged and all
            # three semantic fragments are present together.
            active_page_cooldown = self._detect_active_page_reject_cooldown(controls)
            # Final direct rejection returns a modal write receipt before the
            # detail status settles.  Treat it as authoritative only when the
            # exact order/URL checks above succeeded and one strict modal owns
            # all three observed controls.  A generic historical "提交成功"
            # string elsewhere on the page can never satisfy this contract.
            success_receipt = self._confirmation_target(
                handle,
                ("暂不查看",),
                ("提交成功", "查看整改建议"),
            )
            if success_receipt is not None:
                return {
                    "kind": "terminal",
                    "terminal": {
                        "verified": True,
                        "aftersale_status": "",
                        "success_marker": "提交成功",
                        "success_dialog_present": True,
                    },
                }
            if active_page_cooldown:
                return {
                    "kind": "cooldown",
                    "prompt_text": active_page_cooldown,
                }
            terminal = self._terminal_for_action(
                handle, REJECT_REFUND, baseline_status=baseline_status
            )
            if terminal:
                key = (
                    str(terminal.get("aftersale_status") or ""),
                    str(terminal.get("success_marker") or ""),
                )
                terminal_reads = terminal_reads + 1 if key == terminal_key else 1
                terminal_key = key
                if terminal_reads >= 2:
                    return {"kind": "terminal", "terminal": terminal}
            else:
                terminal_key = None
                terminal_reads = 0
            time.sleep(0.35)
        return None

    def _invoke_action_target(
        self,
        window: Any,
        target: Any,
        text: str,
        *,
        submit_attempted: bool = False,
    ) -> dict[str, Any]:
        """Invoke one already-resolved live control without fixed coordinates."""

        try:
            target.iface_scroll_item.ScrollIntoView()
            time.sleep(0.2)
        except Exception:
            pass
        self._focus(window)
        control_type = str(target.element_info.control_type or "")
        executed = False
        method = ""
        if control_type == "RadioButton":
            try:
                target.iface_selection_item.Select()
                executed = True
                method = "target_selection_item"
            except Exception:
                try:
                    target.iface_toggle.Toggle()
                    executed = True
                    method = "target_toggle"
                except Exception:
                    pass
        elif control_type == "CheckBox":
            try:
                target.iface_toggle.Toggle()
                executed = True
                method = "target_toggle"
            except Exception:
                pass
        elif self._control_supports_invoke(target):
            try:
                target.iface_invoke.Invoke()
                executed = True
                method = "target_uia_invoke"
            except Exception:
                pass
        if not executed:
            rect = self._rect(target)
            if rect[1] < 75 or rect[3] > 1030:
                raise PddUiaError(f"售后操作“{text}”无法安全滚动到可见区域")
            target.click_input()
            executed = True
            method = "exact_live_control_click"
        time.sleep(0.45)
        return {
            "text": text,
            "control_type": control_type,
            "method": method,
            "executed": True,
            "submit_attempted": bool(submit_attempted),
        }

    @staticmethod
    def _selection_state(control: Any) -> bool | None:
        """Read a radio/checkbox state without mutating it."""

        try:
            return bool(control.iface_selection_item.CurrentIsSelected)
        except Exception:
            pass
        try:
            return bool(int(control.iface_toggle.CurrentToggleState))
        except Exception:
            pass
        try:
            return bool(int(control.get_toggle_state()))
        except Exception:
            return None

    def _amount_reject_selected_by_semantics(self, controls: list[Any]) -> bool:
        """Recognize the real default-selected amount negotiation form."""

        names = [
            self._normalized_action_text(self._name(control))
            for control in controls
            if self._control_is_visible(control)
        ]
        return bool(
            any(name == "退款金额未达成一致" for name in names)
            and any(
                "第二步请与消费者协商退款金额" in name
                and "提交后将自动发送协商话术给消费者" in name
                for name in names
            )
            and sum(name == "提交" for name in names) == 1
        )

    def _wait_for_amount_reject_selected_by_semantics(
        self,
        handle: int,
        *,
        expected_root_signature: tuple[Any, ...],
        timeout_seconds: float = 2.5,
    ) -> bool:
        """Wait for PDD's default-selected amount form to finish rendering.

        The live page exposes ``退款金额未达成一致`` as plain Text rather than
        a RadioButton.  Its label is rendered before the dependent second-step
        form, so an immediate read can briefly look unselected even though the
        blue radio is already selected.  Only the complete second-step form is
        authoritative; never click the non-actionable text label to compensate
        for that rendering gap.
        """

        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            snapshot = self._reject_modal_snapshot(handle)
            if (
                snapshot is not None
                and snapshot[0].root_signature == expected_root_signature
                and self._amount_reject_selected_by_semantics(snapshot[1])
            ):
                return True
            time.sleep(0.2)
        return False

    def _verify_selected_reject_option(
        self,
        handle: int,
        text: str,
        *,
        contains: bool,
        expected_root_signature: tuple[Any, ...],
        timeout_seconds: float = 3,
    ) -> bool | None:
        """Verify a selectable reject option in the current strict modal."""

        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        unknown_seen = False
        while time.monotonic() < deadline:
            snapshot = self._reject_modal_snapshot(handle)
            if (
                snapshot is None
                or snapshot[0].root_signature != expected_root_signature
            ):
                time.sleep(0.2)
                continue
            scope, controls = snapshot
            # On the observed amount-negotiation dialog this option is
            # selected by default, but Chromium exposes only a Text label and
            # its wide invokable row. The exact second-step copy exists only
            # in that selected form, so this combination is the semantic
            # equivalent of a checked radio.
            if self._normalized_action_text(text) == self._normalized_action_text(
                "退款金额未达成一致"
            ):
                if self._amount_reject_selected_by_semantics(controls):
                    return True
            targets = self._action_targets(
                handle,
                text,
                contains=contains,
                controls=controls,
                scope=scope,
            )
            if len(targets) != 1:
                time.sleep(0.2)
                continue
            target = targets[0]
            control_type = str(target.element_info.control_type or "")
            if control_type not in {"RadioButton", "CheckBox"}:
                return True
            selected = self._selection_state(target)
            if selected is True:
                return True
            if selected is None:
                unknown_seen = True
            time.sleep(0.2)
        return None if unknown_seen else False

    def _reject_option_label_target(
        self,
        controls: list[Any],
        scope: _StrictModalScope,
        text: str,
        *,
        contains: bool,
    ) -> Any | None:
        """Resolve one visible semantic option label in the strict modal."""

        expected = self._normalized_action_text(text)
        matches: dict[tuple[int, int, int, int], Any] = {}
        for control in controls:
            if not self._control_is_visible(control):
                continue
            if not self._control_is_descendant_of_scope(control, scope):
                continue
            name = self._normalized_action_text(self._name(control))
            if not ((expected in name) if contains else (expected == name)):
                continue
            matches[self._rect(control)] = control
        if len(matches) != 1:
            return None
        return next(iter(matches.values()))

    def _invoke_unique_action(
        self,
        window: Any,
        text: str,
        *,
        contains: bool = False,
        controls: list[Any] | None = None,
        scope: _StrictModalScope | None = None,
        below_top: int | None = None,
        submit_attempted: bool = False,
    ) -> tuple[dict[str, Any], Any]:
        handle = int(window.handle)
        targets = self._action_targets(
            handle,
            text,
            contains=contains,
            controls=controls,
            scope=scope,
            below_top=below_top,
        )
        if len(targets) != 1:
            raise PddUiaError(f"售后操作“{text}”当前可点击控件不唯一：{len(targets)}")
        target = targets[0]
        return (
            self._invoke_action_target(
                window,
                target,
                text,
                submit_attempted=submit_attempted,
            ),
            target,
        )

    def _wait_and_invoke_unique_action(
        self,
        window: Any,
        text: str,
        *,
        contains: bool = False,
        scope: _StrictModalScope | None = None,
        below_top: int | None = None,
        submit_attempted: bool = False,
        timeout_seconds: float = 6,
    ) -> tuple[dict[str, Any], Any]:
        if submit_attempted and scope is None:
            raise PddUiaError(f"售后最终提交“{text}”缺少严格业务弹窗范围")
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        last_count = 0
        while time.monotonic() < deadline:
            controls = self._controls(int(window.handle))
            if scope is not None:
                controls = self._controls_in_modal_scope(controls, scope)
            targets = self._action_targets(
                int(window.handle),
                text,
                contains=contains,
                controls=controls,
                scope=scope,
                below_top=below_top,
            )
            last_count = len(targets)
            if len(targets) == 1:
                target = targets[0]
                return (
                    self._invoke_action_target(
                        window,
                        target,
                        text,
                        submit_attempted=submit_attempted,
                    ),
                    target,
                )
            if len(targets) > 1:
                break
            time.sleep(0.2)
        raise PddUiaError(
            f"等待售后弹窗操作“{text}”失败，可点击控件数量：{last_count}"
        )

    @staticmethod
    def _status_name_from_text(value: Any) -> str:
        """Extract one status from a short, already-scoped header value."""

        text = re.sub(r"\s+", "", str(value or "")).strip()
        if not text or len(text) > 48:
            return ""
        if text in AFTERSALE_STATUS_NAMES:
            return text
        if text in AFTERSALE_STATUS_ALIASES:
            return AFTERSALE_STATUS_ALIASES[text]
        status_terms = {
            **{status: status for status in AFTERSALE_STATUS_NAMES},
            **AFTERSALE_STATUS_ALIASES,
        }
        hits = [
            (text.rfind(term), canonical)
            for term, canonical in status_terms.items()
            if term in text
        ]
        if not hits:
            return ""
        # Real PDD headings can be “买家已发货，待商家处理”.  The right-most
        # status is the current actionable suffix.  Ties are ambiguous.
        last_position = max(position for position, _status in hits)
        rightmost = list(
            dict.fromkeys(status for position, status in hits if position == last_position)
        )
        return rightmost[0] if len(rightmost) == 1 else ""

    def _current_aftersale_status_from_controls(
        self, controls: list[Any]
    ) -> str:
        """Read only an explicit status field or a proven action/header scope.

        Fixed page-wide Y cut-offs are deliberately forbidden: the aftersale
        timeline can begin above the old 760px threshold and contains the same
        status words.  If neither structural proof is unique, return ``""`` so
        callers fail closed.
        """

        visible = [
            control for control in controls if self._control_is_visible(control)
        ]
        label_names = ("售后状态：", "处理状态：")
        labelled_values: list[str] = []
        explicit_labels: list[Any] = []
        for control in visible:
            try:
                name = self._name(control).strip()
            except Exception:
                continue
            matched_label = next(
                (label for label in label_names if name.startswith(label)), None
            )
            if matched_label is None:
                continue
            explicit_labels.append(control)
            remainder = name[len(matched_label) :].strip()
            status = self._status_name_from_text(remainder)
            if status:
                labelled_values.append(status)

        # Separate label/value controls are the structure observed on the real
        # detail page.  Resolve only visible same-row values to the label's
        # right; timeline controls cannot satisfy this relation.
        for label in explicit_labels:
            try:
                label_name = self._name(label).strip()
                label_rect = self._rect(label)
            except Exception:
                continue
            if label_name not in label_names:
                continue
            row_values: list[tuple[int, str]] = []
            for candidate in visible:
                if candidate is label:
                    continue
                try:
                    candidate_rect = self._rect(candidate)
                    if (
                        candidate_rect[0] < label_rect[2] - 4
                        or abs(candidate_rect[1] - label_rect[1]) > 8
                    ):
                        continue
                    status = self._status_name_from_text(self._name(candidate))
                except Exception:
                    continue
                if status:
                    row_values.append((candidate_rect[0], status))
            if row_values:
                nearest_left = min(position for position, _status in row_values)
                nearest = list(
                    dict.fromkeys(
                        status
                        for position, status in row_values
                        if position == nearest_left
                    )
                )
                if len(nearest) == 1:
                    labelled_values.append(nearest[0])

        distinct_labelled = list(dict.fromkeys(labelled_values))
        if len(distinct_labelled) == 1:
            return distinct_labelled[0]
        if explicit_labels or len(distinct_labelled) > 1:
            return ""

        # Some PDD variants expose the current heading as one sentence without
        # a label.  Accept it only inside the nearest structural container that
        # also owns a live current action, and only when it lies above that
        # action.  History below the action row is excluded by relationship,
        # not by an absolute screen coordinate.
        scoped_statuses: list[str] = []
        for action in visible:
            if not self._control_is_live_action(action):
                continue
            action_name = self._normalized_action_text(self._name(action))
            if action_name not in {
                self._normalized_action_text(text)
                for text in AFTERSALE_ACTION_TEXT.values()
            }:
                continue
            try:
                action_rect = self._rect(action)
            except Exception:
                continue
            current = action
            for _depth in range(8):
                try:
                    current = current.parent()
                except Exception:
                    current = None
                if current is None:
                    break
                try:
                    control_type = str(current.element_info.control_type or "")
                    root_rect = self._rect(current)
                except Exception:
                    continue
                width = root_rect[2] - root_rect[0]
                height = root_rect[3] - root_rect[1]
                if (
                    control_type not in {"Pane", "Group", "Custom"}
                    or width < 240
                    or height < 80
                    or width > 1500
                    or height > 720
                ):
                    continue
                candidate_scope = _StrictModalScope(
                    root=current,
                    root_signature=self._control_runtime_signature(current),
                    rect=root_rect,
                )
                candidates: list[str] = []
                for candidate in self._controls_in_modal_scope(
                    visible, candidate_scope
                ):
                    if candidate is action:
                        continue
                    try:
                        candidate_rect = self._rect(candidate)
                        if candidate_rect[3] > action_rect[1] + 12:
                            continue
                        status = self._status_name_from_text(self._name(candidate))
                    except Exception:
                        continue
                    if status:
                        candidates.append(status)
                distinct = list(dict.fromkeys(candidates))
                if len(distinct) == 1:
                    scoped_statuses.append(distinct[0])
                    break
                if len(distinct) > 1:
                    break
        distinct_scoped = list(dict.fromkeys(scoped_statuses))
        if len(distinct_scoped) == 1:
            return distinct_scoped[0]
        if len(distinct_scoped) > 1:
            return ""

        # The current PDD detail page places the heading and action controls
        # directly under one tall page Group, so there is no smaller shared
        # container to prove.  Fall back to a strict geometric relationship:
        # a short status heading must be above and horizontally aligned with a
        # unique current action.  Timeline statuses below the action and the
        # unrelated left-side status summary cannot satisfy this relation.
        action_targets: dict[tuple[int, int, int, int], Any] = {}
        for text in AFTERSALE_ACTION_TEXT.values():
            for target in self._action_targets(0, text, controls=visible):
                try:
                    action_targets[self._rect(target)] = target
                except Exception:
                    continue
        aligned_statuses: list[str] = []
        for action in action_targets.values():
            try:
                action_rect = self._rect(action)
            except Exception:
                continue
            candidates: list[str] = []
            for candidate in visible:
                if candidate is action:
                    continue
                try:
                    candidate_rect = self._rect(candidate)
                    vertical_gap = action_rect[1] - candidate_rect[3]
                    horizontal_gap = max(
                        0,
                        action_rect[0] - candidate_rect[2],
                        candidate_rect[0] - action_rect[2],
                    )
                    if not (0 <= vertical_gap <= 320 and horizontal_gap <= 32):
                        continue
                    status = self._status_name_from_text(self._name(candidate))
                except Exception:
                    continue
                if status:
                    candidates.append(status)
            distinct = list(dict.fromkeys(candidates))
            if len(distinct) == 1:
                aligned_statuses.append(distinct[0])
            elif len(distinct) > 1:
                return ""
        distinct_aligned = list(dict.fromkeys(aligned_statuses))
        return distinct_aligned[0] if len(distinct_aligned) == 1 else ""

    def _terminal_for_action(
        self,
        handle: int,
        action: str,
        *,
        baseline_status: str = "",
    ) -> dict[str, Any] | None:
        controls = self._controls(handle)
        status = self._current_aftersale_status_from_controls(controls)
        capabilities = self._action_capabilities_from_controls(controls)
        if not capabilities.get("action_controls_read_complete"):
            return None
        if action == AGREE_RECALL_AFTER_REFUND:
            names = [
                self._normalized_action_text(self._name(control))
                for control in controls
                if self._name(control)
            ]
            combined = "".join(names)
            success_marker = ""
            if "召回退款操作成功" in combined:
                success_marker = "召回退款操作成功"
            elif (
                "商家已同意召回后退款" in combined
                and "上传召回凭证" in combined
            ):
                success_marker = "商家已同意召回后退款"
            button_gone = not bool(
                capabilities.get("has_agree_recall_after_refund")
            )
            if not button_gone or not success_marker:
                return None
            return {
                "verified": True,
                "aftersale_status": status,
                "success_marker": success_marker,
                "action_control_absent": True,
            }
        if not status:
            return None
        if action == AGREE_REFUND:
            button_gone = not bool(capabilities.get("has_agree_refund"))
            verified = button_gone and status in {
                "已同意退款",
                "退款成功",
                "已完成",
            }
        elif action == AGREE_RETURN_AFTER_REFUND:
            button_gone = not bool(capabilities.get("has_agree_return_after_refund"))
            verified = (
                button_gone
                and bool(baseline_status)
                and status != baseline_status
                and status
                in {
                    "待买家处理",
                    "待买家发货",
                    "等待买家退货",
                    "待商家收货",
                    "待快递退回后退款",
                    "退款成功",
                    "已完成",
                }
            )
        elif action == REJECT_REFUND:
            button_gone = not bool(capabilities.get("has_reject_refund"))
            verified = (
                button_gone
                and bool(baseline_status)
                and status != baseline_status
                and status
                in {"待买家处理", "待平台处理", "已拒绝退款", "已完成"}
            )
        else:
            return None
        if not verified:
            return None
        return {
            "verified": True,
            "aftersale_status": status,
            "success_marker": status,
            "action_control_absent": not bool(
                capabilities.get(
                    {
                        AGREE_REFUND: "has_agree_refund",
                        AGREE_RETURN_AFTER_REFUND: "has_agree_return_after_refund",
                        AGREE_RECALL_AFTER_REFUND: "has_agree_recall_after_refund",
                        REJECT_REFUND: "has_reject_refund",
                    }[action]
                )
            ),
        }

    def _wait_for_action_terminal(
        self,
        window: Any,
        action: str,
        *,
        baseline_status: str,
        timeout_seconds: float | None = None,
        order_no: str = "",
        detail_url: str = "",
    ) -> dict[str, Any] | None:
        timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(self.config.get("aftersale_action_verify_seconds", 25))
        )
        deadline = time.monotonic() + max(0.1, timeout)
        consecutive = 0
        previous_key: tuple[str, str] | None = None
        while time.monotonic() < deadline:
            if detail_url and self._current_url(window) != detail_url:
                return None
            if order_no:
                controls = self._controls(int(window.handle))
                order_matches = sum(
                    self._name(control) == order_no for control in controls
                )
                if (
                    action == AGREE_RECALL_AFTER_REFUND
                    and order_matches == 0
                ):
                    # The observed recall action reloads the same detail URL.
                    # During the white loading frame no order text exists yet;
                    # keep waiting, but never accept another URL or duplicate.
                    time.sleep(0.2)
                    continue
                if order_matches != 1:
                    return None
            terminal = self._terminal_for_action(
                int(window.handle), action, baseline_status=baseline_status
            )
            if terminal:
                key = (
                    str(terminal.get("aftersale_status") or ""),
                    str(terminal.get("success_marker") or ""),
                )
                consecutive = consecutive + 1 if key == previous_key else 1
                previous_key = key
                if consecutive >= 2:
                    return terminal
            else:
                consecutive = 0
                previous_key = None
            time.sleep(0.4)
        return None

    def _confirmation_target(
        self,
        handle: int,
        labels: tuple[str, ...],
        markers: tuple[str, ...],
        *,
        expected_root_signature: tuple[Any, ...] | None = None,
    ) -> tuple[Any, _StrictModalScope] | None:
        """Resolve a modal-local confirmation using its business copy."""

        controls = self._controls(handle)
        normalized_markers = tuple(
            self._normalized_action_text(marker) for marker in markers
        )
        matches: dict[
            tuple[Any, ...], tuple[Any, _StrictModalScope]
        ] = {}
        for label in labels:
            for target in self._action_targets(handle, label, controls=controls):
                scope = self._dialog_scope_from_anchor(target, controls)
                if scope is None:
                    continue
                if (
                    expected_root_signature is not None
                    and scope.root_signature != expected_root_signature
                ):
                    continue
                scope_text_parts: list[str] = []
                scoped_controls = self._controls_in_modal_scope(controls, scope)
                for control in scoped_controls:
                    scope_text_parts.append(self._name(control))
                scope_text = self._normalized_action_text("".join(scope_text_parts))
                if not any(marker in scope_text for marker in normalized_markers):
                    continue
                target_signature = self._control_runtime_signature(target)
                matches[(*scope.root_signature, *target_signature)] = (target, scope)
        if len(matches) != 1:
            return None
        return next(iter(matches.values()))

    def _wait_for_confirmation_target(
        self,
        handle: int,
        labels: tuple[str, ...],
        markers: tuple[str, ...],
        *,
        timeout_seconds: float = 8,
        expected_root_signature: tuple[Any, ...] | None = None,
    ) -> tuple[Any, _StrictModalScope] | None:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            target = self._confirmation_target(
                handle,
                labels,
                markers,
                expected_root_signature=expected_root_signature,
            )
            if target is not None:
                return target
            time.sleep(0.2)
        return None

    def _execute_return_after_refund(
        self,
        window: Any,
        *,
        baseline_status: str,
        shop: dict[str, Any],
        order_no: str,
        detail_url: str,
    ) -> dict[str, Any]:
        trace: list[dict[str, Any]] = []
        first, _target = self._invoke_unique_action(
            window, "同意快递退回后退款"
        )
        trace.append(first)
        confirmation = self._wait_for_confirmation_target(
            int(window.handle),
            ("确认", "提交"),
            ("同意快递退回后退款", "快递退回后退款", "确认退款"),
            timeout_seconds=float(
                self.config.get("aftersale_return_dialog_wait_seconds", 8)
            ),
        )
        if confirmation is None:
            return {
                "ok": False,
                "action": AGREE_RETURN_AFTER_REFUND,
                "trace": trace,
                "needs_review": True,
                "reason_code": "PDD_RETURN_REFUND_MODAL_NOT_VERIFIED",
                "error": "点击同意快递退回后退款后，未找到严格业务确认弹窗",
                "business_write_performed": False,
                "submit_attempted": False,
            }
        _initial_target, initial_scope = confirmation
        expected_root_signature = initial_scope.root_signature
        if self._current_url(window) != detail_url:
            raise PddUiaError("退回后退款确认前详情URL发生变化")
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_DETAIL,
            order_no=order_no,
            stage="同意快递退回后退款最终确认前",
        )
        # Re-resolve after the identity check; never use a wrapper captured
        # before a possible Chromium redraw.
        confirmation = self._confirmation_target(
            int(window.handle),
            ("确认", "提交"),
            ("同意快递退回后退款", "快递退回后退款", "确认退款"),
            expected_root_signature=expected_root_signature,
        )
        if confirmation is None:
            return {
                "ok": False,
                "action": AGREE_RETURN_AFTER_REFUND,
                "trace": trace,
                "needs_review": True,
                "reason_code": "PDD_RETURN_REFUND_MODAL_CHANGED",
                "error": "最终确认前业务弹窗结构已变化，停止提交",
                "business_write_performed": False,
                "submit_attempted": False,
            }
        target, _scope = confirmation
        confirmation_text = self._name(target).strip()
        trace.append(
            self._invoke_action_target(
                window,
                target,
                confirmation_text,
                submit_attempted=True,
            )
        )
        terminal = self._wait_for_action_terminal(
            window,
            AGREE_RETURN_AFTER_REFUND,
            baseline_status=baseline_status,
            order_no=order_no,
            detail_url=detail_url,
        )
        if not terminal:
            return {
                "ok": False,
                "action": AGREE_RETURN_AFTER_REFUND,
                "trace": trace,
                "write_outcome_unknown": True,
                "reason_code": "PDD_RETURN_REFUND_OUTCOME_UNKNOWN",
                "error": "退回后退款提交后未读取到可核验终态，禁止自动重试",
                "business_write_performed": True,
                "submit_attempted": True,
            }
        return {
            "ok": True,
            "action": AGREE_RETURN_AFTER_REFUND,
            "trace": trace,
            "terminal": terminal,
            "business_write_performed": True,
            "submit_attempted": True,
        }

    def _execute_recall_after_refund(
        self,
        window: Any,
        *,
        baseline_status: str,
        shop: dict[str, Any],
        order_no: str,
        detail_url: str,
    ) -> dict[str, Any]:
        """Submit PDD's observed recall-before-refund action safely."""

        trace: list[dict[str, Any]] = []
        first, _target = self._invoke_unique_action(window, "同意召回后退款")
        trace.append(first)
        confirmation = self._wait_for_confirmation_target(
            int(window.handle),
            ("确认",),
            (
                "同意召回后退款",
                "请及时联系物流公司召回商品",
                "48小时内无退回轨迹",
                "系统将自动退款",
            ),
            timeout_seconds=float(
                self.config.get("aftersale_recall_dialog_wait_seconds", 8)
            ),
        )
        if confirmation is None:
            return {
                "ok": False,
                "action": AGREE_RECALL_AFTER_REFUND,
                "trace": trace,
                "needs_review": True,
                "reason_code": "PDD_RECALL_REFUND_MODAL_NOT_VERIFIED",
                "error": "点击同意召回后退款后，未找到严格业务确认弹窗",
                "business_write_performed": False,
                "submit_attempted": False,
            }
        _initial_target, initial_scope = confirmation
        if self._current_url(window) != detail_url:
            raise PddUiaError("召回后退款确认前详情URL发生变化")
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_DETAIL,
            order_no=order_no,
            stage="同意召回后退款最终确认前",
        )
        confirmation = self._confirmation_target(
            int(window.handle),
            ("确认",),
            (
                "同意召回后退款",
                "请及时联系物流公司召回商品",
                "48小时内无退回轨迹",
                "系统将自动退款",
            ),
            expected_root_signature=initial_scope.root_signature,
        )
        if confirmation is None:
            return {
                "ok": False,
                "action": AGREE_RECALL_AFTER_REFUND,
                "trace": trace,
                "needs_review": True,
                "reason_code": "PDD_RECALL_REFUND_MODAL_CHANGED",
                "error": "最终确认前召回退款弹窗结构已变化，停止提交",
                "business_write_performed": False,
                "submit_attempted": False,
            }
        target, _scope = confirmation
        trace.append(
            self._invoke_action_target(
                window,
                target,
                self._name(target).strip(),
                submit_attempted=True,
            )
        )
        terminal = self._wait_for_action_terminal(
            window,
            AGREE_RECALL_AFTER_REFUND,
            baseline_status=baseline_status,
            order_no=order_no,
            detail_url=detail_url,
        )
        if not terminal:
            return {
                "ok": False,
                "action": AGREE_RECALL_AFTER_REFUND,
                "trace": trace,
                "write_outcome_unknown": True,
                "reason_code": "PDD_RECALL_REFUND_OUTCOME_UNKNOWN",
                "error": "召回后退款提交后未读取到成功标记，禁止自动重试",
                "business_write_performed": True,
                "submit_attempted": True,
            }
        return {
            "ok": True,
            "action": AGREE_RECALL_AFTER_REFUND,
            "trace": trace,
            "terminal": terminal,
            "business_write_performed": True,
            "submit_attempted": True,
        }

    def _execute_keep_reject_negotiation(
        self,
        window: Any,
        *,
        baseline_status: str,
        shop: dict[str, Any],
        order_no: str,
        detail_url: str,
        initial_scope: _StrictModalScope,
        trace: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Execute the real multi-stage ``坚持驳回`` negotiation flow."""

        handle = int(window.handle)
        keep_step, _target = self._wait_and_invoke_unique_action(
            window,
            "坚持驳回",
            scope=initial_scope,
        )
        trace.append(keep_step)

        transitioned = self._wait_for_reject_surface(
            handle,
            {
                "KEEP_REJECT_PROCESS_ENTRY",
                "KEEP_REJECT_NEGOTIATION_ENTRY",
                "KEEP_REJECT_NEGOTIATION",
                "COOLDOWN",
            },
            timeout_seconds=float(
                self.config.get("aftersale_reject_negotiation_wait_seconds", 8)
            ),
        )
        if transitioned is None:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "needs_review": True,
                "reason_code": "PDD_REJECT_NEGOTIATION_NOT_VERIFIED",
                "error": "点击坚持驳回后未找到唯一的消费者协商页面",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        surface, scope, controls = transitioned
        if surface == "COOLDOWN":
            prompt = self._detect_reject_cooldown(controls)
            return {
                "ok": True,
                "completed": False,
                "deferred": True,
                "retryable": True,
                "action": REJECT_REFUND,
                "reason_code": REJECT_COOLDOWN_REASON_CODE,
                "retry_after_seconds": REJECT_COOLDOWN_SECONDS,
                "prompt_text": prompt,
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }

        # Some shops show a policy explanation after ``坚持驳回``. The real
        # forward path is the exact modal-local ``去协商`` button; the adjacent
        # ``进一步了解`` button is intentionally never clicked.
        if surface == "KEEP_REJECT_PROCESS_ENTRY":
            process_step, _target = self._wait_and_invoke_unique_action(
                window,
                "去协商",
                scope=scope,
            )
            trace.append(process_step)
            transitioned = self._wait_for_reject_surface(
                handle,
                {
                    "KEEP_REJECT_NEGOTIATION_ENTRY",
                    "KEEP_REJECT_NEGOTIATION",
                    "COOLDOWN",
                },
                timeout_seconds=float(
                    self.config.get("aftersale_reject_negotiation_wait_seconds", 8)
                ),
            )
            if transitioned is None:
                return {
                    "ok": False,
                    "action": REJECT_REFUND,
                    "needs_review": True,
                    "reason_code": "PDD_REJECT_GO_NEGOTIATE_NOT_VERIFIED",
                    "error": "点击去协商后未找到唯一的消费者协商方案页面",
                    "trace": trace,
                    "business_write_performed": False,
                    "submit_attempted": False,
                }
            surface, scope, controls = transitioned
            if surface == "COOLDOWN":
                prompt = self._detect_reject_cooldown(controls)
                return {
                    "ok": True,
                    "completed": False,
                    "deferred": True,
                    "retryable": True,
                    "action": REJECT_REFUND,
                    "reason_code": REJECT_COOLDOWN_REASON_CODE,
                    "retry_after_seconds": REJECT_COOLDOWN_SECONDS,
                    "prompt_text": prompt,
                    "trace": trace,
                    "business_write_performed": False,
                    "submit_attempted": False,
                }

        # Some PDD versions insert a separate ``与消费者协商方案`` button;
        # others render the final proposal form immediately.  Follow the
        # former only when the exact live control exists.
        if surface == "KEEP_REJECT_NEGOTIATION_ENTRY":
            entry_step, _target = self._wait_and_invoke_unique_action(
                window,
                "与消费者协商方案",
                scope=scope,
            )
            trace.append(entry_step)
            transitioned = self._wait_for_reject_surface(
                handle,
                {"KEEP_REJECT_NEGOTIATION", "COOLDOWN"},
                timeout_seconds=float(
                    self.config.get("aftersale_reject_negotiation_wait_seconds", 8)
                ),
            )
            if transitioned is None:
                return {
                    "ok": False,
                    "action": REJECT_REFUND,
                    "needs_review": True,
                    "reason_code": "PDD_REJECT_PROPOSAL_NOT_VERIFIED",
                    "error": "点击与消费者协商方案后未找到唯一的协商方案表单",
                    "trace": trace,
                    "business_write_performed": False,
                    "submit_attempted": False,
                }
            surface, scope, controls = transitioned
            if surface == "COOLDOWN":
                prompt = self._detect_reject_cooldown(controls)
                return {
                    "ok": True,
                    "completed": False,
                    "deferred": True,
                    "retryable": True,
                    "action": REJECT_REFUND,
                    "reason_code": REJECT_COOLDOWN_REASON_CODE,
                    "retry_after_seconds": REJECT_COOLDOWN_SECONDS,
                    "prompt_text": prompt,
                    "trace": trace,
                    "business_write_performed": False,
                    "submit_attempted": False,
                }

        proposal_root_signature = scope.root_signature
        label_clicked_selections: set[str] = set()

        def exact_scoped_labels(
            text: str,
            controls: list[Any],
            current_scope: _StrictModalScope,
        ) -> list[Any]:
            """Return one exact visible option label inside the verified modal.

            Chromium currently exposes the proposal checkbox caption as a
            plain ``Text`` node instead of a CheckBox/InvokePattern.  Clicking
            that exact label is the browser-equivalent of clicking its HTML
            label, but the fallback remains confined to one verified modal and
            refuses duplicate captions.
            """

            expected = self._normalized_action_text(text)
            matches: dict[tuple[int, int, int, int], Any] = {}
            for control in controls:
                if not self._control_is_visible(control):
                    continue
                if not self._control_is_descendant_of_scope(control, current_scope):
                    continue
                if self._normalized_action_text(self._name(control)) != expected:
                    continue
                matches[self._rect(control)] = control
            return [
                matches[key]
                for key in sorted(matches, key=lambda item: (item[1], item[0]))
            ]

        def ensure_selected(
            text: str, *, required: bool
        ) -> tuple[bool, str]:
            snapshot = self._wait_for_reject_modal_snapshot(
                handle,
                expected_root_signature=proposal_root_signature,
            )
            if snapshot is None:
                return False, f"选择“{text}”前协商方案弹窗结构已变化"
            current_scope, current_controls = snapshot
            targets = self._action_targets(
                handle,
                text,
                controls=current_controls,
                scope=current_scope,
            )
            if not targets:
                labels = exact_scoped_labels(text, current_controls, current_scope)
                if len(labels) == 1:
                    step = self._invoke_action_target(window, labels[0], text)
                    step["method"] = "exact_modal_label_click"
                    step["selection_verified"] = True
                    step["verification"] = "unique_exact_label_in_unchanged_modal"
                    trace.append(step)
                    label_clicked_selections.add(text)
                    return True, ""
            if not targets and not required:
                return True, ""
            if len(targets) != 1:
                return False, f"协商方案“{text}”可点击控件不唯一：{len(targets)}"
            target = targets[0]
            control_type = str(target.element_info.control_type or "")
            selected = (
                self._selection_state(target)
                if control_type in {"RadioButton", "CheckBox"}
                else False
            )
            if selected is True:
                trace.append(
                    {
                        "text": text,
                        "control_type": control_type,
                        "method": "already_selected",
                        "executed": False,
                        "selection_verified": True,
                    }
                )
                return True, ""
            step = self._invoke_action_target(window, target, text)
            trace.append(step)
            verified = self._verify_selected_reject_option(
                handle,
                text,
                contains=False,
                expected_root_signature=proposal_root_signature,
            )
            trace[-1]["selection_verified"] = verified is True
            if verified is not True:
                return False, f"协商方案“{text}”点击后未能回读为已选择"
            return True, ""

        # The proposal radio is selected by default in the observed page.  Do
        # not toggle it off on recovery.  Older variants omit it and go
        # straight to the full-refund choice, which is also supported.
        ok, error = ensure_selected("与消费者协商售后方案", required=False)
        if not ok:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "needs_review": True,
                "reason_code": "PDD_REJECT_NEGOTIATION_SELECTION_UNVERIFIED",
                "error": error,
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        negotiation_choice_present = any(
            item.get("text") == "与消费者协商售后方案" for item in trace
        )
        ok, error = ensure_selected("退货后退全款", required=True)
        if not ok:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "needs_review": True,
                "reason_code": "PDD_REJECT_NEGOTIATION_SELECTION_UNVERIFIED",
                "error": error,
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }

        if self._current_url(window) != detail_url:
            raise PddUiaError("提交协商方案前详情URL发生变化")
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_DETAIL,
            order_no=order_no,
            stage="坚持驳回提交协商方案前",
        )
        snapshot = self._reject_modal_snapshot(handle)
        if (
            snapshot is None
            or snapshot[0].root_signature != proposal_root_signature
            or self._reject_surface_from_snapshot(handle, snapshot)
            != "KEEP_REJECT_NEGOTIATION"
        ):
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "needs_review": True,
                "reason_code": "PDD_REJECT_NEGOTIATION_MODAL_CHANGED",
                "error": "订单身份复核后协商方案弹窗已变化，停止提交",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        scope, _controls = snapshot
        required_selections = ["退货后退全款"]
        if negotiation_choice_present:
            required_selections.insert(0, "与消费者协商售后方案")
        for text in required_selections:
            if text in label_clicked_selections:
                current = self._reject_modal_snapshot(handle)
                if (
                    current is None
                    or current[0].root_signature != proposal_root_signature
                    or len(exact_scoped_labels(text, current[1], current[0])) != 1
                ):
                    return {
                        "ok": False,
                        "action": REJECT_REFUND,
                        "needs_review": True,
                        "reason_code": "PDD_REJECT_NEGOTIATION_SELECTION_CHANGED",
                        "error": f"提交协商方案前“{text}”所在弹窗已变化",
                        "trace": trace,
                        "business_write_performed": False,
                        "submit_attempted": False,
                    }
                continue
            selected = self._verify_selected_reject_option(
                handle,
                text,
                contains=False,
                expected_root_signature=proposal_root_signature,
                timeout_seconds=1.0,
            )
            if selected is not True:
                return {
                    "ok": False,
                    "action": REJECT_REFUND,
                    "needs_review": True,
                    "reason_code": "PDD_REJECT_NEGOTIATION_SELECTION_CHANGED",
                    "error": f"提交协商方案前“{text}”不再是已选择状态",
                    "trace": trace,
                    "business_write_performed": False,
                    "submit_attempted": False,
                }
        submit, submit_target = self._wait_and_invoke_unique_action(
            window,
            "提交协商方案",
            scope=scope,
            submit_attempted=False,
        )
        trace.append(submit)
        final_labels = ("确认，发给消费者", "确认，发送给消费者")
        final_markers = (
            "确认提交售后协商方案吗",
            "售后协商方案中任意1个",
            "发给消费者",
        )
        final_wait_seconds = float(
            self.config.get("aftersale_reject_final_confirm_wait_seconds", 8)
        )
        confirmation = self._wait_for_confirmation_target(
            handle,
            final_labels,
            final_markers,
            timeout_seconds=min(3.0, final_wait_seconds),
        )
        final_label_fallback = False
        if confirmation is None:
            deadline = time.monotonic() + max(0.5, final_wait_seconds - 3.0)
            while time.monotonic() < deadline:
                current = self._reject_modal_snapshot(handle)
                if (
                    current is not None
                    and current[0].root_signature == proposal_root_signature
                ):
                    labels = [
                        control
                        for label_text in final_labels
                        for control in exact_scoped_labels(
                            label_text, current[1], current[0]
                        )
                    ]
                    unique = {self._rect(control): control for control in labels}
                    if len(unique) == 1:
                        confirmation = (next(iter(unique.values())), current[0])
                        final_label_fallback = True
                        break
                time.sleep(0.2)
        visual_confirmation: dict[str, Any] | None = None
        if confirmation is None:
            # The real PDD pop-confirm is custom painted in some Chromium
            # builds and contributes no actionable UIA/text node.  Its blue
            # confirmation button is the only filled control in a narrow box
            # immediately above the already-verified ``提交协商方案`` button.
            # Locate that rectangle from the fresh window pixels instead of a
            # desktop-wide or fixed coordinate.
            try:
                submit_rect = self._rect(submit_target)
                window_rect = self._rect(window)
                roi = (
                    max(window_rect[0], submit_rect[0] - 150),
                    max(window_rect[1], submit_rect[1] - 150),
                    min(window_rect[2], submit_rect[2] + 150),
                    min(window_rect[3], submit_rect[1] - 8),
                )
                image = window.capture_as_image().convert("RGB")
                crop = image.crop(
                    (
                        roi[0] - window_rect[0],
                        roi[1] - window_rect[1],
                        roi[2] - window_rect[0],
                        roi[3] - window_rect[1],
                    )
                )
                pixels = crop.load()
                width, height = crop.size

                def is_blue(x: int, y: int) -> bool:
                    red, green, blue = pixels[x, y]
                    return bool(
                        blue >= 180
                        and blue - red >= 70
                        and blue - green >= 35
                        and red <= 145
                    )

                row_hits = [
                    sum(1 for x in range(width) if is_blue(x, y))
                    for y in range(height)
                ]
                row_runs: list[tuple[int, int]] = []
                start: int | None = None
                for y, count in enumerate([*row_hits, 0]):
                    if count >= 55 and start is None:
                        start = y
                    elif count < 55 and start is not None:
                        if y - start >= 14:
                            row_runs.append((start, y))
                        start = None
                candidates: list[tuple[int, int, int, int]] = []
                for top, bottom in row_runs:
                    run_height = bottom - top
                    col_hits = [
                        sum(1 for y in range(top, bottom) if is_blue(x, y))
                        for x in range(width)
                    ]
                    left: int | None = None
                    threshold = max(5, run_height // 3)
                    for x, count in enumerate([*col_hits, 0]):
                        if count >= threshold and left is None:
                            left = x
                        elif count < threshold and left is not None:
                            if x - left >= 60:
                                candidates.append((left, top, x, bottom))
                            left = None
                if len(candidates) == 1:
                    left, top, right, bottom = candidates[0]
                    screen_rect = (
                        roi[0] + left,
                        roi[1] + top,
                        roi[0] + right,
                        roi[1] + bottom,
                    )
                    visual_confirmation = {
                        "screen_rect": screen_rect,
                        "coords": (
                            (screen_rect[0] + screen_rect[2]) // 2,
                            (screen_rect[1] + screen_rect[3]) // 2,
                        ),
                    }
            except Exception:
                visual_confirmation = None
        if confirmation is None and visual_confirmation is None:
            # Retain compatibility with a platform variant that may submit the
            # proposal directly without the observed second confirmation.
            outcome = self._wait_for_reject_post_submit_outcome(
                window,
                baseline_status=baseline_status,
                order_no=order_no,
                detail_url=detail_url,
                timeout_seconds=2.0,
            )
            if outcome is None:
                return {
                    "ok": False,
                    "action": REJECT_REFUND,
                    "reject_surface": "KEEP_REJECT",
                    "needs_review": True,
                    "reason_code": "PDD_REJECT_NEGOTIATION_FINAL_CONFIRM_NOT_VERIFIED",
                    "error": "提交协商方案后未找到“确认，发给消费者”按钮",
                    "trace": trace,
                    "business_write_performed": False,
                    "submit_attempted": False,
                }
        elif visual_confirmation is not None:
            if self._current_url(window) != detail_url:
                raise PddUiaError("确认发送协商方案前详情URL发生变化")
            self.assert_page_contract(
                window,
                shop,
                AFTERSALE_DETAIL,
                order_no=order_no,
                stage="坚持驳回确认发送给消费者前",
            )
            self._focus(window)
            self.mouse.click(coords=visual_confirmation["coords"])
            time.sleep(0.45)
            trace.append(
                {
                    "text": "确认，发给消费者",
                    "control_type": "CustomPaintedButton",
                    "method": "verified_blue_button_relative_to_submit",
                    "executed": True,
                    "submit_attempted": True,
                    "screen_rect": visual_confirmation["screen_rect"],
                }
            )
            outcome = self._wait_for_reject_post_submit_outcome(
                window,
                baseline_status=baseline_status,
                order_no=order_no,
                detail_url=detail_url,
            )
        else:
            _initial_target, final_scope = confirmation
            if self._current_url(window) != detail_url:
                raise PddUiaError("确认发送协商方案前详情URL发生变化")
            self.assert_page_contract(
                window,
                shop,
                AFTERSALE_DETAIL,
                order_no=order_no,
                stage="坚持驳回确认发送给消费者前",
            )
            if final_label_fallback:
                current = self._reject_modal_snapshot(handle)
                labels = (
                    [
                        control
                        for label_text in final_labels
                        for control in exact_scoped_labels(
                            label_text, current[1], current[0]
                        )
                    ]
                    if current is not None
                    and current[0].root_signature == final_scope.root_signature
                    else []
                )
                unique = {self._rect(control): control for control in labels}
                confirmation = (
                    (next(iter(unique.values())), current[0])
                    if len(unique) == 1 and current is not None
                    else None
                )
            else:
                confirmation = self._confirmation_target(
                    handle,
                    final_labels,
                    final_markers,
                    expected_root_signature=final_scope.root_signature,
                )
            if confirmation is None:
                return {
                    "ok": False,
                    "action": REJECT_REFUND,
                    "reject_surface": "KEEP_REJECT",
                    "needs_review": True,
                    "reason_code": "PDD_REJECT_NEGOTIATION_FINAL_CONFIRM_CHANGED",
                    "error": "最终确认前“确认，发给消费者”弹窗已变化",
                    "trace": trace,
                    "business_write_performed": False,
                    "submit_attempted": False,
                }
            final_target, _final_scope = confirmation
            final_text = self._name(final_target).strip()
            trace.append(
                self._invoke_action_target(
                    window,
                    final_target,
                    final_text,
                    submit_attempted=True,
                )
            )
            if final_label_fallback:
                trace[-1]["method"] = "exact_modal_label_click"
                trace[-1]["verification"] = "unique_exact_label_in_unchanged_modal"
            outcome = self._wait_for_reject_post_submit_outcome(
                window,
                baseline_status=baseline_status,
                order_no=order_no,
                detail_url=detail_url,
            )
        if outcome and outcome.get("kind") == "cooldown":
            return {
                "ok": True,
                "completed": False,
                "deferred": True,
                "retryable": True,
                "action": REJECT_REFUND,
                "reject_surface": "KEEP_REJECT",
                "defer_kind": "POST_SUBMIT_VERIFIED",
                "stage_committed": True,
                "negotiation_round_submitted": True,
                "resume_action": REJECT_REFUND,
                "reason_code": REJECT_NEGOTIATION_COOLDOWN_REASON_CODE,
                "retry_after_seconds": REJECT_COOLDOWN_SECONDS,
                "prompt_text": str(outcome.get("prompt_text") or ""),
                "trace": trace,
                "write_outcome_verified": True,
                "write_outcome_unknown": False,
                "business_write_performed": True,
                "submit_attempted": True,
            }
        if outcome and outcome.get("kind") == "terminal":
            return {
                "ok": True,
                "action": REJECT_REFUND,
                "reject_surface": "KEEP_REJECT",
                "trace": trace,
                "terminal": dict(outcome.get("terminal") or {}),
                "business_write_performed": True,
                "submit_attempted": True,
            }
        return {
            "ok": False,
            "action": REJECT_REFUND,
            "reject_surface": "KEEP_REJECT",
            "trace": trace,
            "write_outcome_unknown": True,
            "reason_code": "PDD_REJECT_NEGOTIATION_OUTCOME_UNKNOWN",
            "error": "提交协商方案后未读取到终态或完整30分钟等待提示，禁止自动重试",
            "business_write_performed": True,
            "submit_attempted": True,
        }

    def _direct_reject_form_snapshot(
        self,
        handle: int,
        *,
        timeout_seconds: float = 8,
        expected_root_signature: tuple[Any, ...] | None = None,
    ) -> tuple[_StrictModalScope, list[Any]] | None:
        """Wait for the real final direct-reject form rendered by PDD."""

        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            snapshot = self._reject_modal_snapshot(handle)
            if snapshot is None:
                time.sleep(0.2)
                continue
            scope, controls = snapshot
            if (
                expected_root_signature is not None
                and scope.root_signature != expected_root_signature
            ):
                time.sleep(0.2)
                continue
            names = [
                self._normalized_action_text(self._name(control))
                for control in controls
                if self._control_is_visible(control)
            ]
            edits = [
                control
                for control in controls
                if self._control_is_visible(control)
                and str(control.element_info.control_type or "") == "Edit"
                and "请详细描述" in self._normalized_action_text(
                    self._name(control)
                )
            ]
            if (
                any(
                    "消费者描述属实但不认可消费者提出的售后方案"
                    in name
                    for name in names
                )
                and any(name == "不协商直接驳回" for name in names)
                and any(name == "驳回描述" for name in names)
                and sum(name == "提交" for name in names) == 1
                and len(edits) == 1
            ):
                return scope, controls
            time.sleep(0.2)
        return None

    def _execute_direct_reject_with_description(
        self,
        window: Any,
        *,
        baseline_status: str,
        shop: dict[str, Any],
        order_no: str,
        detail_url: str,
        initial_scope: _StrictModalScope,
        trace: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Execute the verified final-reject path without negotiation.

        The first reason replaces/expands Chromium descendants, so the form is
        deliberately re-resolved after every click.  Submission is permitted
        only after the exact description is read back from UIA ValuePattern in
        the same strict modal and the order identity is checked again.
        """

        handle = int(window.handle)
        first_text = "消费者描述属实，但不认可消费者提出的售后方案"
        first_snapshot = self._reject_modal_snapshot(handle)
        if (
            first_snapshot is None
            or first_snapshot[0].root_signature != initial_scope.root_signature
        ):
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "reject_surface": "DIRECT_REJECT",
                "needs_review": True,
                "reason_code": "PDD_DIRECT_REJECT_MODAL_CHANGED",
                "error": "选择最终驳回原因前业务弹窗已变化",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        first_scope, first_controls = first_snapshot
        first_targets = self._action_targets(
            handle,
            first_text,
            contains=True,
            controls=first_controls,
            scope=first_scope,
        )
        if len(first_targets) != 1:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "reject_surface": "DIRECT_REJECT",
                "needs_review": True,
                "reason_code": "PDD_DIRECT_REJECT_REASON_NOT_UNIQUE",
                "error": "最终驳回原因控件不唯一",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        trace.append(
            self._invoke_action_target(window, first_targets[0], first_text)
        )

        form = self._direct_reject_form_snapshot(
            handle,
            timeout_seconds=float(
                self.config.get("aftersale_direct_reject_form_wait_seconds", 8)
            ),
        )
        if form is None:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "reject_surface": "DIRECT_REJECT",
                "needs_review": True,
                "reason_code": "PDD_DIRECT_REJECT_FORM_NOT_VERIFIED",
                "error": "选择最终驳回原因后未读到完整的驳回描述表单",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        form_scope, form_controls = form
        direct_text = "不协商，直接驳回"
        direct_label = self._reject_option_label_target(
            form_controls,
            form_scope,
            direct_text,
            contains=False,
        )
        if direct_label is None:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "reject_surface": "DIRECT_REJECT",
                "needs_review": True,
                "reason_code": "PDD_DIRECT_REJECT_OPTION_NOT_UNIQUE",
                "error": "“不协商，直接驳回”选项不唯一",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        trace.append(
            self._invoke_action_target(window, direct_label, direct_text)
        )

        if self._current_url(window) != detail_url:
            raise PddUiaError("填写最终驳回描述前详情URL发生变化")
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_DETAIL,
            order_no=order_no,
            stage="填写最终驳回描述前",
        )
        form = self._direct_reject_form_snapshot(handle)
        if form is None:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "reject_surface": "DIRECT_REJECT",
                "needs_review": True,
                "reason_code": "PDD_DIRECT_REJECT_FORM_CHANGED",
                "error": "订单身份复核后最终驳回表单已变化",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        form_scope, form_controls = form
        expected_root_signature = form_scope.root_signature
        edits = [
            control
            for control in form_controls
            if str(control.element_info.control_type or "") == "Edit"
            and "请详细描述" in self._normalized_action_text(
                self._name(control)
            )
        ]
        if len(edits) != 1:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "reject_surface": "DIRECT_REJECT",
                "needs_review": True,
                "reason_code": "PDD_DIRECT_REJECT_DESCRIPTION_NOT_UNIQUE",
                "error": "驳回描述输入框不唯一",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        description = str(
            self.config.get("aftersale_direct_reject_description")
            or DEFAULT_DIRECT_REJECT_DESCRIPTION
        ).strip()
        if not description:
            raise PddUiaError("最终驳回描述为空")
        edits[0].set_edit_text(description)
        time.sleep(0.35)
        try:
            actual_description = str(edits[0].get_value() or "")
        except Exception:
            actual_description = ""
        if actual_description != description:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "reject_surface": "DIRECT_REJECT",
                "needs_review": True,
                "reason_code": "PDD_DIRECT_REJECT_DESCRIPTION_UNVERIFIED",
                "error": "驳回描述写入后回读不一致",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        trace.append(
            {
                "text": "驳回描述",
                "control_type": "Edit",
                "method": "uia_value_pattern",
                "executed": True,
                "value_verified": True,
                "submit_attempted": False,
            }
        )

        if self._current_url(window) != detail_url:
            raise PddUiaError("最终驳回提交前详情URL发生变化")
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_DETAIL,
            order_no=order_no,
            stage="最终驳回提交前",
        )
        form = self._direct_reject_form_snapshot(
            handle,
            expected_root_signature=expected_root_signature,
        )
        if form is None:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "reject_surface": "DIRECT_REJECT",
                "needs_review": True,
                "reason_code": "PDD_DIRECT_REJECT_FORM_CHANGED",
                "error": "最终提交前驳回表单已变化",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        form_scope, form_controls = form
        edits = [
            control
            for control in form_controls
            if str(control.element_info.control_type or "") == "Edit"
            and "请详细描述" in self._normalized_action_text(
                self._name(control)
            )
        ]
        if len(edits) != 1 or str(edits[0].get_value() or "") != description:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "reject_surface": "DIRECT_REJECT",
                "needs_review": True,
                "reason_code": "PDD_DIRECT_REJECT_DESCRIPTION_CHANGED",
                "error": "最终提交前驳回描述不再一致",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        # Chromium exposes this radio as Image + Text rather than a selectable
        # UIA role.  Re-click the unique exact label immediately before submit;
        # radio semantics make the operation idempotent and remove any chance
        # that a redraw restored the default negotiation choice.
        direct_label = self._reject_option_label_target(
            form_controls,
            form_scope,
            direct_text,
            contains=False,
        )
        if direct_label is None:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "reject_surface": "DIRECT_REJECT",
                "needs_review": True,
                "reason_code": "PDD_DIRECT_REJECT_OPTION_CHANGED",
                "error": "最终提交前“不协商，直接驳回”选项已变化",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        trace.append(
            self._invoke_action_target(window, direct_label, direct_text)
        )
        form = self._direct_reject_form_snapshot(
            handle,
            expected_root_signature=expected_root_signature,
        )
        if form is None:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "reject_surface": "DIRECT_REJECT",
                "needs_review": True,
                "reason_code": "PDD_DIRECT_REJECT_FORM_CHANGED",
                "error": "重新确认直接驳回选项后表单已变化",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        form_scope, form_controls = form
        edits = [
            control
            for control in form_controls
            if str(control.element_info.control_type or "") == "Edit"
            and "请详细描述" in self._normalized_action_text(
                self._name(control)
            )
        ]
        if len(edits) != 1 or str(edits[0].get_value() or "") != description:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "reject_surface": "DIRECT_REJECT",
                "needs_review": True,
                "reason_code": "PDD_DIRECT_REJECT_DESCRIPTION_CHANGED",
                "error": "重新确认直接驳回选项后驳回描述发生变化",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        submits = [
            control
            for control in form_controls
            if str(control.element_info.control_type or "") == "Button"
            and self._normalized_action_text(self._name(control)) == "提交"
        ]
        if len(submits) != 1:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "reject_surface": "DIRECT_REJECT",
                "needs_review": True,
                "reason_code": "PDD_DIRECT_REJECT_SUBMIT_NOT_UNIQUE",
                "error": "最终驳回提交按钮不唯一",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        trace.append(
            self._invoke_action_target(
                window,
                submits[0],
                "提交",
                submit_attempted=True,
            )
        )
        outcome = self._wait_for_reject_post_submit_outcome(
            window,
            baseline_status=baseline_status,
            order_no=order_no,
            detail_url=detail_url,
        )
        if not outcome or outcome.get("kind") != "terminal":
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "reject_surface": "DIRECT_REJECT",
                "trace": trace,
                "write_outcome_unknown": True,
                "reason_code": "PDD_DIRECT_REJECT_OUTCOME_UNKNOWN",
                "error": "最终驳回提交后未读到“提交成功”或稳定终态，禁止自动重试",
                "business_write_performed": True,
                "submit_attempted": True,
            }
        terminal = dict(outcome.get("terminal") or {})
        if terminal.get("success_dialog_present"):
            dismiss = self._confirmation_target(
                handle,
                ("暂不查看",),
                ("提交成功", "查看整改建议"),
            )
            if dismiss is not None:
                target, _scope = dismiss
                trace.append(
                    self._invoke_action_target(window, target, "暂不查看")
                )
                stable = self._wait_for_action_terminal(
                    window,
                    REJECT_REFUND,
                    baseline_status=baseline_status,
                    timeout_seconds=5,
                    order_no=order_no,
                    detail_url=detail_url,
                )
                if stable:
                    terminal = stable
                    terminal["success_marker"] = "提交成功"
        return {
            "ok": True,
            "action": REJECT_REFUND,
            "reject_surface": "DIRECT_REJECT",
            "trace": trace,
            "terminal": terminal,
            "business_write_performed": True,
            "submit_attempted": True,
        }

    def _execute_reject_refund(
        self,
        window: Any,
        *,
        baseline_status: str,
        shop: dict[str, Any],
        order_no: str,
        detail_url: str,
    ) -> dict[str, Any]:
        """Execute one of the three exact reject dialogs as a state machine."""

        handle = int(window.handle)
        trace: list[dict[str, Any]] = []
        active_cooldown = self._detect_active_page_reject_cooldown(
            self._controls(handle)
        )
        if active_cooldown:
            return {
                "ok": True,
                "completed": False,
                "deferred": True,
                "retryable": True,
                "action": REJECT_REFUND,
                "reason_code": REJECT_COOLDOWN_REASON_CODE,
                "retry_after_seconds": REJECT_COOLDOWN_SECONDS,
                "prompt_text": active_cooldown,
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        # A live page-level cooldown is identified by its active negotiation
        # heading plus an HH:MM:SS timer, so historical timeline prose cannot
        # suppress a valid action.  Outside that one state, a fresh worker must
        # invoke the unique top-level button and scope all choices to the strict
        # modal that appears afterwards.
        first, _target = self._invoke_unique_action(window, "驳回退款")
        trace.append(first)
        snapshot = self._wait_for_reject_modal_snapshot(
            handle,
            timeout_seconds=float(
                self.config.get("aftersale_reject_dialog_wait_seconds", 8)
            ),
        )
        if snapshot is None:
            active_cooldown = self._detect_active_page_reject_cooldown(
                self._controls(handle)
            )
            if active_cooldown:
                return {
                    "ok": True,
                    "completed": False,
                    "deferred": True,
                    "retryable": True,
                    "action": REJECT_REFUND,
                    "reason_code": REJECT_COOLDOWN_REASON_CODE,
                    "retry_after_seconds": REJECT_COOLDOWN_SECONDS,
                    "prompt_text": active_cooldown,
                    "trace": trace,
                    "business_write_performed": False,
                    "submit_attempted": False,
                }
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "needs_review": True,
                "reason_code": "PDD_REJECT_MODAL_NOT_VERIFIED",
                "error": "点击驳回退款后未找到严格、唯一的业务弹窗",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        scope, controls = snapshot
        expected_root_signature = scope.root_signature
        cooldown_prompt = self._detect_reject_cooldown(controls)
        if cooldown_prompt:
            return {
                "ok": True,
                "completed": False,
                "deferred": True,
                "retryable": True,
                "action": REJECT_REFUND,
                "reason_code": REJECT_COOLDOWN_REASON_CODE,
                "retry_after_seconds": REJECT_COOLDOWN_SECONDS,
                "prompt_text": cooldown_prompt,
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        surface = self._classify_reject_surface(controls)
        if surface == "UNKNOWN":
            surface = self._infer_progressive_reject_surface(
                handle, controls, scope=scope
            )
        if surface in {"UNKNOWN", "AMBIGUOUS"}:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "needs_review": True,
                "reason_code": "PDD_REJECT_SURFACE_UNRECOGNIZED",
                "error": f"驳回退款弹窗无法唯一识别：{surface}",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }

        if surface == "KEEP_REJECT":
            return self._execute_keep_reject_negotiation(
                window,
                baseline_status=baseline_status,
                shop=shop,
                order_no=order_no,
                detail_url=detail_url,
                initial_scope=scope,
                trace=trace,
            )

        if surface == "DIRECT_REJECT":
            return self._execute_direct_reject_with_description(
                window,
                baseline_status=baseline_status,
                shop=shop,
                order_no=order_no,
                detail_url=detail_url,
                initial_scope=scope,
                trace=trace,
            )

        sequences = {
            "AMOUNT_NOT_AGREED": (("退款金额未达成一致", True),),
        }
        anchor_bottom: int | None = None
        for text, contains in sequences[surface]:
            # Re-resolve after every click because Chromium invalidates stale
            # wrappers when a radio option expands another part of the dialog.
            snapshot = self._wait_for_reject_modal_snapshot(
                handle,
                expected_root_signature=expected_root_signature,
            )
            if snapshot is None:
                return {
                    "ok": False,
                    "action": REJECT_REFUND,
                    "needs_review": True,
                    "reason_code": "PDD_REJECT_MODAL_CHANGED",
                    "error": f"选择“{text}”前业务弹窗结构已变化",
                    "trace": trace,
                    "business_write_performed": False,
                    "submit_attempted": False,
                }
            scope, controls = snapshot
            cooldown_prompt = self._detect_reject_cooldown(controls)
            if cooldown_prompt:
                return {
                    "ok": True,
                    "completed": False,
                    "deferred": True,
                    "retryable": True,
                    "action": REJECT_REFUND,
                    "reason_code": REJECT_COOLDOWN_REASON_CODE,
                    "retry_after_seconds": REJECT_COOLDOWN_SECONDS,
                    "prompt_text": cooldown_prompt,
                    "trace": trace,
                    "business_write_performed": False,
                    "submit_attempted": False,
                }
            already_selected = bool(
                surface == "AMOUNT_NOT_AGREED"
                and self._amount_reject_selected_by_semantics(controls)
            )
            if surface == "AMOUNT_NOT_AGREED" and not already_selected:
                # The real second-round form renders progressively: the Text
                # label appears first and the dependent form follows shortly.
                # Wait for the complete default-selected form before trying a
                # genuine actionable radio variant.  This avoids attempting to
                # click a Chromium Text node, which has no safe UIA action.
                already_selected = (
                    self._wait_for_amount_reject_selected_by_semantics(
                        handle,
                        expected_root_signature=expected_root_signature,
                        timeout_seconds=float(
                            self.config.get(
                                "aftersale_reject_default_selection_wait_seconds",
                                2.5,
                            )
                        ),
                    )
                )
            if already_selected is True:
                refreshed = self._wait_for_reject_modal_snapshot(
                    handle,
                    expected_root_signature=expected_root_signature,
                    timeout_seconds=1.0,
                )
                if refreshed is None:
                    return {
                        "ok": False,
                        "action": REJECT_REFUND,
                        "needs_review": True,
                        "reason_code": "PDD_REJECT_MODAL_CHANGED",
                        "error": f"回读默认选中的“{text}”时业务弹窗结构已变化",
                        "trace": trace,
                        "business_write_performed": False,
                        "submit_attempted": False,
                    }
                scope, controls = refreshed
                label = self._reject_option_label_target(
                    controls, scope, text, contains=contains
                )
                if label is None:
                    return {
                        "ok": False,
                        "action": REJECT_REFUND,
                        "needs_review": True,
                        "reason_code": "PDD_REJECT_SELECTION_UNVERIFIED",
                        "error": f"驳回选项“{text}”虽为默认选中但标签不唯一",
                        "trace": trace,
                        "business_write_performed": False,
                        "submit_attempted": False,
                    }
                anchor_bottom = self._rect(label)[3]
                trace.append(
                    {
                        "text": text,
                        "control_type": str(label.element_info.control_type or ""),
                        "method": "already_selected_by_modal_semantics",
                        "executed": False,
                        "selection_verified": True,
                    }
                )
                continue
            step, target = self._wait_and_invoke_unique_action(
                window,
                text,
                contains=contains,
                scope=scope,
            )
            trace.append(step)
            anchor_bottom = self._rect(target)[3]
            selected = self._verify_selected_reject_option(
                handle,
                text,
                contains=contains,
                expected_root_signature=expected_root_signature,
            )
            trace[-1]["selection_verified"] = selected is True
            if selected is not True:
                return {
                    "ok": False,
                    "action": REJECT_REFUND,
                    "needs_review": True,
                    "reason_code": "PDD_REJECT_SELECTION_UNVERIFIED",
                    "error": f"驳回选项“{text}”点击后未能回读为已选择，停止提交",
                    "trace": trace,
                    "business_write_performed": False,
                    "submit_attempted": False,
                }

        snapshot = self._wait_for_reject_modal_snapshot(
            handle,
            expected_root_signature=expected_root_signature,
        )
        if snapshot is None:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "needs_review": True,
                "reason_code": "PDD_REJECT_MODAL_CHANGED",
                "error": "最终提交前业务弹窗结构已变化",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        scope, controls = snapshot
        cooldown_prompt = self._detect_reject_cooldown(controls)
        if cooldown_prompt:
            return {
                "ok": True,
                "completed": False,
                "deferred": True,
                "retryable": True,
                "action": REJECT_REFUND,
                "reason_code": REJECT_COOLDOWN_REASON_CODE,
                "retry_after_seconds": REJECT_COOLDOWN_SECONDS,
                "prompt_text": cooldown_prompt,
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        if self._current_url(window) != detail_url:
            raise PddUiaError("驳回退款提交前详情URL发生变化")
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_DETAIL,
            order_no=order_no,
            stage="驳回退款最终提交前",
        )
        # The identity check can redraw Chromium; require the same strict
        # business modal again and resolve Submit from that fresh snapshot.
        snapshot = self._reject_modal_snapshot(handle)
        if (
            snapshot is None
            or snapshot[0].root_signature != expected_root_signature
        ):
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "needs_review": True,
                "reason_code": "PDD_REJECT_MODAL_CHANGED",
                "error": "订单身份复核后驳回弹窗已变化，停止提交",
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }
        scope, controls = snapshot

        final_surface = self._classify_reject_surface(controls)
        if final_surface == "UNKNOWN":
            final_surface = self._infer_progressive_reject_surface(
                handle,
                controls,
                scope=scope,
            )
        if final_surface != surface:
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "needs_review": True,
                "reason_code": "PDD_REJECT_SURFACE_CHANGED",
                "error": (
                    "最终提交前驳回弹窗分支已变化："
                    f"expected={surface}, actual={final_surface}"
                ),
                "trace": trace,
                "business_write_performed": False,
                "submit_attempted": False,
            }

        # The page-contract read can rebuild descendants without replacing the
        # modal root.  Re-read every required selection from that same root;
        # never submit a freshly reset/default form.
        for text, contains in sequences[surface]:
            selected = self._verify_selected_reject_option(
                handle,
                text,
                contains=contains,
                expected_root_signature=expected_root_signature,
                timeout_seconds=1.0,
            )
            if selected is not True:
                return {
                    "ok": False,
                    "action": REJECT_REFUND,
                    "needs_review": True,
                    "reason_code": "PDD_REJECT_SELECTION_CHANGED",
                    "error": f"最终提交前驳回选项“{text}”不再是已选择状态",
                    "trace": trace,
                    "business_write_performed": False,
                    "submit_attempted": False,
                }

        last_text, last_contains = sequences[surface][-1]
        last_targets = self._action_targets(
            handle,
            last_text,
            contains=last_contains,
            controls=controls,
            scope=scope,
        )
        if len(last_targets) == 1:
            anchor_bottom = self._rect(last_targets[0])[3]
        else:
            last_label = self._reject_option_label_target(
                controls, scope, last_text, contains=last_contains
            )
            if last_label is None:
                return {
                    "ok": False,
                    "action": REJECT_REFUND,
                    "needs_review": True,
                    "reason_code": "PDD_REJECT_SELECTION_CHANGED",
                    "error": "最终提交前无法在原业务弹窗中唯一回读最后一个驳回选项",
                    "trace": trace,
                    "business_write_performed": False,
                    "submit_attempted": False,
                }
            anchor_bottom = self._rect(last_label)[3]
        submit, _target = self._wait_and_invoke_unique_action(
            window,
            "提交",
            scope=scope,
            below_top=anchor_bottom,
            submit_attempted=True,
        )
        trace.append(submit)
        outcome = self._wait_for_reject_post_submit_outcome(
            window,
            baseline_status=baseline_status,
            order_no=order_no,
            detail_url=detail_url,
        )
        if outcome is None:
            # Preserve the existing verified terminal reader for platform
            # variants that complete immediately without a negotiation banner.
            terminal_fallback = self._wait_for_action_terminal(
                window,
                REJECT_REFUND,
                baseline_status=baseline_status,
                order_no=order_no,
                detail_url=detail_url,
            )
            if terminal_fallback:
                outcome = {"kind": "terminal", "terminal": terminal_fallback}
        if outcome and outcome.get("kind") == "cooldown":
            return {
                "ok": True,
                "completed": False,
                "deferred": True,
                "retryable": True,
                "action": REJECT_REFUND,
                "reject_surface": surface,
                "defer_kind": "POST_SUBMIT_VERIFIED",
                "stage_committed": True,
                "negotiation_round_submitted": True,
                "resume_action": REJECT_REFUND,
                "reason_code": REJECT_NEGOTIATION_COOLDOWN_REASON_CODE,
                "retry_after_seconds": REJECT_COOLDOWN_SECONDS,
                "prompt_text": str(outcome.get("prompt_text") or ""),
                "trace": trace,
                "write_outcome_verified": True,
                "write_outcome_unknown": False,
                "business_write_performed": True,
                "submit_attempted": True,
            }
        if not outcome or outcome.get("kind") != "terminal":
            return {
                "ok": False,
                "action": REJECT_REFUND,
                "trace": trace,
                "write_outcome_unknown": True,
                "reason_code": "PDD_REJECT_OUTCOME_UNKNOWN",
                "prompt_text": "",
                "error": "驳回退款提交后未读取到终态或完整30分钟等待提示，禁止自动重试",
                "business_write_performed": True,
                "submit_attempted": True,
            }
        terminal = dict(outcome.get("terminal") or {})
        return {
            "ok": True,
            "action": REJECT_REFUND,
            "reject_surface": surface,
            "trace": trace,
            "terminal": terminal,
            "business_write_performed": True,
            "submit_attempted": True,
        }

    def _click_stage(self, window: Any, text: str, *, timeout: float = 15) -> dict[str, Any]:
        handle = int(window.handle)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            controls = self._controls(handle)
            matches = []
            for control in controls:
                try:
                    if self._name(control) != text:
                        continue
                    rect = control.rectangle()
                    if 75 <= rect.top <= 1030:
                        matches.append(control)
                except Exception:
                    continue
            if len(matches) > 1:
                actionable = [
                    item
                    for item in matches
                    if str(item.element_info.control_type or "")
                    in {"Button", "Hyperlink", "RadioButton"}
                ]
                matches = actionable if len(actionable) == 1 else matches
            if len(matches) == 1:
                target = matches[0]
                target.click_input()
                time.sleep(0.45)
                return {
                    "text": text,
                    "control_type": str(target.element_info.control_type or ""),
                    "executed": True,
                }
            if len(matches) > 1:
                raise PddUiaError(f"售后操作“{text}”控件不唯一：{len(matches)}")
            self._focus(window)
            self.keyboard.send_keys("{PGDN}")
            time.sleep(0.25)
        raise PddUiaError(f"等待售后操作控件超时：{text}")

    def execute_action(
        self,
        window: Any,
        action: str,
        *,
        shop: dict[str, Any],
        order_no: str,
        detail_url: str,
        max_agree_refund_trace_count: int = 3,
    ) -> dict[str, Any]:
        if self._current_url(window) != detail_url:
            raise PddUiaError("售后详情URL在执行前发生变化")
        self.assert_page_contract(
            window,
            shop,
            AFTERSALE_DETAIL,
            order_no=order_no,
            stage="执行售后操作前",
        )
        self._focus(window)
        self.keyboard.send_keys("^{HOME}")
        shipped_refund_actions = {
            AGREE_REFUND,
            AGREE_RETURN_AFTER_REFUND,
            AGREE_RECALL_AFTER_REFUND,
            REJECT_REFUND,
        }
        current_status = ""
        if action in shipped_refund_actions:
            # WDT lookup and interception can take long enough for another
            # logistics row or a terminal aftersale status to arrive. Refresh
            # and re-run the exact detail/logistics contract immediately
            # before the first business click.
            self.keyboard.send_keys("^r")
            time.sleep(float(self.config.get("aftersale_refresh_wait_seconds", 1.2)))
            self._dismiss_overlay_popups(window)
            current_detail = self.read_detail(window, order_no, detail_url, shop)
            current_status = self._current_aftersale_status_from_controls(
                self._controls(int(window.handle))
            )
            if not current_status:
                return {
                    "ok": False,
                    "action": action,
                    "needs_review": True,
                    "reason_code": "PDD_CURRENT_AFTERSALE_STATUS_UNVERIFIED",
                    "error": "提交前无法从明确状态字段或当前动作区域唯一读取售后状态",
                    "trace": [],
                    "business_write_performed": False,
                }
            ignored_status = _ignored_refund_status_marker(current_status)
            if ignored_status:
                return {
                    "ok": True,
                    "action": action,
                    "skipped": True,
                    "skip_reason": f"售后状态包含{ignored_status}，按规则忽略",
                    "aftersale_status": current_status,
                    "trace": [],
                    "business_write_performed": False,
                }
            already_terminal = self._wait_for_action_terminal(
                window,
                action,
                baseline_status=current_status,
                timeout_seconds=1.0,
                order_no=order_no,
                detail_url=detail_url,
            )
            if already_terminal:
                return {
                    "ok": True,
                    "action": action,
                    "already_terminal": True,
                    "trace": [],
                    "terminal": already_terminal,
                    "business_write_performed": False,
                    "submit_attempted": False,
                }
            logistics = self.read_logistics_detail(
                window, order_no, detail_url, shop
            )
            trace_count = int(logistics.get("logistics_trajectory_count", -1))
            trace_limit = max(1, min(int(max_agree_refund_trace_count), 3))
            limit_text = "一条" if trace_limit == 1 else "三条"
            if action == AGREE_REFUND and trace_count > trace_limit:
                return {
                    "ok": True,
                    "action": action,
                    "skipped": True,
                    "skip_reason": (
                        f"提交前物流轨迹已超过{limit_text}，停止自动同意退款"
                    ),
                    "max_agree_refund_trace_count": trace_limit,
                    **logistics,
                    "trace": [],
                    "business_write_performed": False,
                }
            if action in {
                AGREE_RETURN_AFTER_REFUND,
                AGREE_RECALL_AFTER_REFUND,
                REJECT_REFUND,
            } and trace_count <= trace_limit:
                return {
                    "ok": True,
                    "action": action,
                    "skipped": True,
                    "skip_reason": (
                        f"提交前完整物流轨迹已变为{limit_text}以内，"
                        f"停止执行超过{limit_text}分支"
                    ),
                    "max_agree_refund_trace_count": trace_limit,
                    **logistics,
                    "trace": [],
                    "business_write_performed": False,
                }
            capability_key = {
                AGREE_REFUND: "has_agree_refund",
                AGREE_RETURN_AFTER_REFUND: "has_agree_return_after_refund",
                AGREE_RECALL_AFTER_REFUND: "has_agree_recall_after_refund",
                REJECT_REFUND: "has_reject_refund",
            }[action]
            if not bool(current_detail.get("action_controls_read_complete")):
                return {
                    "ok": False,
                    "action": action,
                    "needs_review": True,
                    "reason_code": "PDD_ACTION_CONTROLS_INCOMPLETE",
                    "error": "提交前当前售后动作控件未能完整读取",
                    "trace": [],
                    "business_write_performed": False,
                }
            if not bool(current_detail.get(capability_key)):
                return {
                    "ok": True,
                    "action": action,
                    "skipped": True,
                    "skip_reason": "提交前当前可操作按钮已变化，停止执行原决策",
                    "trace": [],
                    "business_write_performed": False,
                }
            self._focus(window)
            self.keyboard.send_keys("^{HOME}")
            time.sleep(0.35)
        # Re-read the terminal business status immediately before any click.
        # A list/detail snapshot can become stale while the task is queued.
        # ``退款关闭`` and ``退款成功`` are absolute no-op rules and take
        # precedence over every action, including a retained confirmation flow.
        controls = self._controls(int(window.handle))
        # The top status chip can temporarily disappear after expanding the
        # logistics panel and returning to the action area.  Reuse the status
        # read from this same freshly-refreshed, order-verified detail instead
        # of rejecting an otherwise proven action surface.
        aftersale_status = (
            self._current_aftersale_status_from_controls(controls) or current_status
        )
        ignored_status = _ignored_refund_status_marker(aftersale_status)
        if ignored_status:
            return {
                "ok": True,
                "action": action,
                "skipped": True,
                "skip_reason": f"售后状态包含{ignored_status}，按规则忽略",
                "aftersale_status": aftersale_status,
                "trace": [],
                "business_write_performed": False,
            }
        if action == AGREE_RETURN_AFTER_REFUND:
            return self._execute_return_after_refund(
                window,
                baseline_status=current_status,
                shop=shop,
                order_no=order_no,
                detail_url=detail_url,
            )
        if action == AGREE_RECALL_AFTER_REFUND:
            return self._execute_recall_after_refund(
                window,
                baseline_status=current_status,
                shop=shop,
                order_no=order_no,
                detail_url=detail_url,
            )
        if action == REJECT_REFUND:
            return self._execute_reject_refund(
                window,
                baseline_status=current_status,
                shop=shop,
                order_no=order_no,
                detail_url=detail_url,
            )
        sequences = {
            EXTEND_RECEIPT: ["延长收货", "确认"],
            # PDD 的同意退款确认弹窗主按钮文案是“确认退款”而非“确认”，
            # 精确匹配“确认”会永远超时。
            AGREE_REFUND: ["同意退款", "确认退款"],
        }
        sequence = sequences.get(action)
        if not sequence:
            raise PddUiaError(f"未知售后工作台操作：{action}")
        if action == EXTEND_RECEIPT:
            # Resume safely if an earlier worker stopped after opening the
            # confirmation modal.  Do not click the background action twice.
            names = [
                self._name(control)
                for control in self._controls(int(window.handle))
            ]
            if any(name.startswith("是否确认延长收货") for name in names):
                sequence = ["确认"]
        trace = []
        for index, text in enumerate(sequence, start=1):
            self.assert_page_contract(
                window,
                shop,
                AFTERSALE_DETAIL,
                order_no=order_no,
                stage=f"售后操作第{index}步：{text}",
            )
            trace.append(self._click_stage(window, text))
        terminal: dict[str, Any] = {}
        if action == AGREE_REFUND:
            verified = self._wait_for_action_terminal(
                window,
                AGREE_REFUND,
                baseline_status=current_status,
                order_no=order_no,
                detail_url=detail_url,
            )
            if not verified:
                return {
                    "ok": False,
                    "action": action,
                    "trace": trace,
                    "write_outcome_unknown": True,
                    "reason_code": "PDD_AGREE_REFUND_OUTCOME_UNKNOWN",
                    "error": "同意退款提交后未读取到已同意/退款成功终态，禁止自动重试",
                    "business_write_performed": True,
                    "submit_attempted": True,
                }
            terminal = verified
        return {
            "ok": True,
            "action": action,
            "trace": trace,
            "terminal": terminal,
            "business_write_performed": True,
            "submit_attempted": action == AGREE_REFUND,
        }

    def _cleanup_tab_attempts(self) -> int:
        """Use the cleanup budget when a retained list tab must be recovered."""

        try:
            normal = int(self.config.get("tab_switch_attempts", 8))
        except (TypeError, ValueError):
            normal = 8
        try:
            cleanup = int(self.config.get("tab_cleanup_attempts", 20))
        except (TypeError, ValueError):
            cleanup = 20
        return max(1, min(max(normal, cleanup), 40))

    def _activate_exact_detail_tab(
        self,
        window: Any,
        shop: dict[str, Any],
        detail_url: str,
        order_no: str,
        *,
        attempts: int,
    ) -> bool:
        """Return to this exact detail tab without treating another candidate as it.

        A pair of aftersale records often shares the same original order
        number.  The URL contains the unique aftersale record id, so both the
        URL and visible order are required before a destructive Ctrl+W.
        """

        for _ in range(max(1, attempts) + 1):
            try:
                current_url = self._current_url(window)
                handle = int(window.handle)
                is_target = (
                    classify_aftersale_url(current_url) == "aftersale_detail"
                    and current_url == detail_url
                    and self._has_exact_name(handle, order_no)
                )
            except Exception:
                return False
            if is_target:
                self._focus(window)
                return True
            self._focus(window)
            self.keyboard.send_keys("^{TAB}")
            time.sleep(float(self.config.get("tab_switch_wait_seconds", 0.35)))
        return False

    def wait_for_post_action_render(
        self,
        window: Any,
        *,
        expected_url: str,
        order_no: str,
        timeout_seconds: float = 15,
    ) -> bool:
        """Wait past Chromium's post-submit blank repaint.

        PDD can expose the verified cooldown text to UIA, start a route repaint,
        and only then paint the final detail page.  Capturing or cleaning up at
        that boundary produces a white screenshot and can temporarily hide the
        order number.  A short mandatory grace period followed by two stable
        identity reads keeps evidence and cleanup aligned with the rendered
        final state; it performs no business action.
        """

        time.sleep(
            max(
                0.0,
                float(self.config.get("aftersale_post_action_grace_seconds", 1.2)),
            )
        )
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        stable_reads = 0
        while time.monotonic() < deadline:
            try:
                identity_ready = (
                    self._current_url(window) == expected_url
                    and self._has_exact_name(int(window.handle), order_no)
                )
            except Exception:
                identity_ready = False
            stable_reads = stable_reads + 1 if identity_ready else 0
            if stable_reads >= 2:
                return True
            time.sleep(0.35)
        return False

    def close_detail(self, window: Any, shop: dict[str, Any]) -> dict[str, Any]:
        """Close one detail tab while retaining exactly one usable list anchor.

        ``查看详情`` normally opens a second Edge tab.  The prior implementation
        converted that detail tab into a new workbench tab whenever its short
        list-tab scan missed the existing anchor.  With two candidate records,
        that fallback accumulated workbench tabs.  The cleanup path now scans
        all configured tabs before falling back and only reuses the detail tab
        as the list anchor when no list tab exists at all.
        """

        detail_url = self._current_url(window)
        if classify_aftersale_url(detail_url) != "aftersale_detail":
            return {"closed": 0, "list_ready": False, "handled": False}
        order_numbers = list(
            dict.fromkeys(
                self._name(control)
                for control in self._controls(int(window.handle))
                if ORDER_RE.fullmatch(self._name(control))
            )
        )
        order_no = order_numbers[0] if len(order_numbers) == 1 else ""
        if not order_no:
            raise PddUiaError("关闭售后详情前无法唯一确认订单号")

        attempts = self._cleanup_tab_attempts()
        try:
            list_window = self._list_window(
                shop,
                navigate=False,
                tab_attempts=attempts,
                continue_after_repeat=True,
            )
        except PddUiaError as exc:
            # A real identity/login/navigation problem must still fail closed.
            # Only the known "no list tab" result may reuse the current detail
            # tab as the canonical workbench anchor.
            if "未找到售后工作台列表页" not in str(exc):
                raise
            list_window = None

        if list_window is not None:
            # _list_window activates the list tab.  Return to the exact detail
            # that was opened for this candidate; do not close a same-order
            # sibling candidate.
            if not self._activate_exact_detail_tab(
                list_window,
                shop,
                detail_url,
                order_no,
                attempts=attempts,
            ):
                # The browser has already disposed of the detail, while a
                # verified list anchor remains.  Treat this as successful
                # cleanup rather than raising the old false "0 unique page"
                # failure.
                return {
                    "closed": 0,
                    "navigated": 0,
                    "list_ready": True,
                    "handled": True,
                    "disposition": "detail_already_gone_list_retained",
                }
            self.keyboard.send_keys("^w")
            time.sleep(
                max(
                    0.15,
                    min(
                        float(self.config.get("tab_close_settle_seconds", 0.3)),
                        1.0,
                    ),
                )
            )
            # Ctrl+W normally reveals the retained list tab immediately in
            # this same top-level Edge window.  Verify that active surface
            # directly and reserve the expensive all-tab cleanup scan for the
            # exceptional case only.
            try:
                if (
                    classify_aftersale_url(self._current_url(list_window))
                    == "aftersale_list"
                    and self._window_matches_shop(list_window, shop)
                ):
                    self.assert_page_contract(
                        list_window,
                        shop,
                        AFTERSALE_LIST,
                        stage="售后详情关闭后列表恢复",
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
                self._list_window(
                    shop,
                    navigate=False,
                    tab_attempts=attempts,
                    continue_after_repeat=True,
                )
            except PddUiaError as exc:
                raise PddUiaError(
                    f"{shop['name']} 售后详情页已关闭但工作台列表未恢复：{exc}"
                ) from exc
            return {
                "closed": 1,
                "navigated": 0,
                "list_ready": True,
                "handled": True,
                "disposition": "detail_tab_closed_list_retained",
            }

        # No list tab exists after the full cleanup scan.  In that specific
        # same-tab opening mode, turn the current detail tab back into the
        # single workbench anchor; this does not create another browser tab.
        if not self._activate_exact_detail_tab(
            window,
            shop,
            detail_url,
            order_no,
            attempts=attempts,
        ):
            raise PddUiaError("售后详情页关闭前已丢失，且未找到可用的售后工作台列表页")
        self._navigate_aftersale_list(window, shop)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            time.sleep(0.4)
            if (
                classify_aftersale_url(self._current_url(window)) == "aftersale_list"
                and self._has_exact_name(int(window.handle), shop["name"])
            ):
                return {
                    "closed": 0,
                    "navigated": 1,
                    "list_ready": True,
                    "handled": True,
                    "disposition": "detail_reused_as_single_list_anchor",
                }
        raise PddUiaError("售后详情页导航回列表超时")


def card_to_item(card: AftersaleCard) -> dict[str, Any]:
    return asdict(card)


__all__ = [
    "AFTERSALE_LIST_URL",
    "AftersaleCard",
    "AftersaleListResult",
    "PddAftersaleUiaSession",
    "card_to_item",
    "classify_aftersale_url",
]
