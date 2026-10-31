from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol
from urllib.parse import parse_qs, urlparse

from .base import ConnectorResult, PddConnector


PDD_HOST = "mms.pinduoduo.com"
LIST_PATH = "/aftersales/work_order/list"
DETAIL_PATH = "/aftersales/work_order/tododetail"
ORDER_NUMBER_RE = re.compile(r"\b\d{6}-\d{15,}\b")
TOTAL_PATTERNS = (
    re.compile(r"共查询到\s*(\d+)\s*个工单"),
    re.compile(r"查询结果[^\r\n]{0,30}?(\d+)\s*个工单"),
    re.compile(r"共\s*(\d+)\s*个工单"),
)
EMPTY_MARKERS = ("暂无工单", "暂无相关工单", "暂无数据", "未查询到相关工单", "查询结果为空")


class PddReadOnlyError(RuntimeError):
    pass


class PddDependencyError(PddReadOnlyError):
    pass


@dataclass(slots=True)
class WindowSnapshot:
    handle: int
    title: str
    url: str
    page_text: str
    accessible_names: tuple[str, ...] = ()


class ReadOnlySession(Protocol):
    def health(self) -> dict[str, Any]: ...
    def diagnostics(self) -> list[dict[str, Any]]: ...
    def snapshots(self) -> list[WindowSnapshot]: ...
    def refresh_and_wait(
        self, handle: int, expected_kind: str, expected_shop: str, required_text: str = ""
    ) -> WindowSnapshot: ...
    def cards(self, handle: int) -> list[dict[str, Any]]: ...
    def collect_cards(self, handle: int, total: int) -> list[dict[str, Any]]: ...
    def screenshot(self, handle: int, path: Path) -> None: ...


def classify_pdd_url(url: str) -> str:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return "other"
    if parsed.netloc.lower() != PDD_HOST:
        return "other"
    path = parsed.path.rstrip("/").lower()
    if path == LIST_PATH:
        return "list"
    if path == DETAIL_PATH:
        return "detail"
    return "other"


def extract_work_order_id(url: str) -> str:
    if classify_pdd_url(url) != "detail":
        return ""
    return str((parse_qs(urlparse(url).query).get("id") or [""])[0]).strip()


def extract_total(page_text: str) -> int | None:
    for pattern in TOTAL_PATTERNS:
        match = pattern.search(str(page_text or ""))
        if match:
            return int(match.group(1))
    return None


def validate_complete_snapshot(page_text: str, cards: list[dict[str, Any]]) -> int:
    total = extract_total(page_text)
    compact = re.sub(r"\s+", "", str(page_text or ""))
    explicit_empty = any(marker in compact for marker in EMPTY_MARKERS)
    if total is None:
        if not cards and explicit_empty:
            return 0
        if cards:
            # The displayed total is a moving snapshot. Human customer
            # service may change the queue while cards are being read.
            return len(cards)
        raise PddReadOnlyError("无法读取工单总数，拒绝把当前页面当作完整列表")
    if total == 0 and not cards:
        return 0
    # Do not compare ``total`` with card count: the queue can legitimately
    # change during a scan. The executable result is the cards actually read.
    return len(cards)


def card_identity(card: dict[str, Any]) -> str:
    text = str(card.get("text", "") or "")
    orders = set(ORDER_NUMBER_RE.findall(text))
    if len(orders) != 1:
        raise PddReadOnlyError("工单卡片缺少唯一拼多多订单号")
    order_no = next(iter(orders))
    work_order_id = extract_work_order_id(str(card.get("detail_url", "") or ""))
    if work_order_id:
        return f"work_order:{work_order_id}"
    dom_key = str(card.get("dom_key", "") or "").strip()
    if dom_key:
        return f"dom:{order_no}:{dom_key}"
    created = re.search(
        r"创建时间[：:]\s*(20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
        text,
    )
    if created:
        return f"created:{order_no}:{created.group(1)}"
    raise PddReadOnlyError(f"订单{order_no}缺少可稳定去重的工单标识")


def _remaining_deadline(text: str, now: datetime | None = None) -> str:
    text = str(text or "")
    patterns = (
        (r"(\d+)\s*天", 86400),
        (r"(\d+)\s*(?:小时|时)", 3600),
        (r"(\d+)\s*(?:分钟|分)", 60),
        (r"(\d+)\s*(?:秒)", 1),
    )
    seconds = 0
    matched = False
    for pattern, factor in patterns:
        match = re.search(pattern, text)
        if match:
            matched = True
            seconds += int(match.group(1)) * factor
    if not matched:
        return ""
    return ((now or datetime.now()) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def parse_card(card: dict[str, Any], shop: dict[str, Any]) -> dict[str, Any]:
    text = str(card.get("text", "") or "")
    orders = set(ORDER_NUMBER_RE.findall(text))
    if len(orders) != 1:
        raise PddReadOnlyError("工单卡片缺少唯一拼多多订单号")
    order_no = next(iter(orders))
    detail_url = str(card.get("detail_url", "") or "")
    identity = card_identity(card)
    work_order_no = identity.split(":", 1)[1]
    problem = re.search(r"订单问题[：:]\s*([^\r\n]+)", text)
    reason = problem.group(1).strip() if problem else ""
    amount_match = re.search(r"[¥￥]\s*(\d+(?:\.\d{1,2})?)", text)
    return {
        "shop_id": shop["id"],
        "shop_name": shop["name"],
        "edge_profile": shop["edge_profile"],
        "work_order_no": work_order_no,
        "order_no": order_no,
        "work_order_title": reason or "待处理工单",
        "refund_type": "",
        "refund_reason": reason,
        # 列表金额可能是商品金额而不是可退款金额；详情接入前不做资金推断。
        "refund_amount": 0.0,
        "pdd_order_status": "",
        "pdd_logistics_status": "",
        "carrier": "",
        "tracking_no": "",
        "work_order_deadline": _remaining_deadline(text),
        "buyer_description": "",
        "source_evidence": {
            "pdd_list_url": detail_url,
            "pdd_card_text": text,
            "card_display_amount": amount_match.group(1) if amount_match else "",
            "list_page_number": int(card.get("list_page_number", 1) or 1),
            "collection_mode": "edge_readonly",
        },
    }


def select_shop_snapshot(
    snapshots: list[WindowSnapshot], shop: dict[str, Any]
) -> WindowSnapshot:
    expected = str(shop["name"]).strip()
    matches = [
        item
        for item in snapshots
        if classify_pdd_url(item.url) == "list"
        and (expected in item.page_text or expected in item.accessible_names)
    ]
    if not matches:
        raise PddReadOnlyError(
            f"未找到“{expected}”的拼多多工单列表窗口；请先在{shop['edge_profile']}打开工单管理"
        )
    if len(matches) != 1:
        raise PddReadOnlyError(f"发现{len(matches)}个“{expected}”工单窗口，无法安全确定目标")
    return matches[0]


class WindowsEdgeReadOnlySession:
    """只读取可见Edge窗口；不导航、不刷新、不点击业务按钮。"""

    CARD_EXPRESSION = (
        "(()=>{const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();"
        "return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0};"
        "const raw=[...document.querySelectorAll('[class*=workOrderItem]')].filter(card=>{"
        "const text=card.innerText||'',orders=text.match(/\\b\\d{6}-\\d{15,}\\b/g)||[];"
        "return visible(card)&&new Set(orders).size===1});"
        "const cards=raw.filter(card=>!raw.some(other=>other!==card&&card.contains(other)));"
        "return cards.map(card=>{const link=[...card.querySelectorAll('a[href]')].find("
        "a=>a.href&&a.href.includes('/aftersales/work_order/tododetail'));"
        "const keys=['data-row-key','data-id','data-work-order-id','id'];"
        "const domKey=keys.map(k=>card.getAttribute(k)||'').find(Boolean)||'';"
        "return {text:card.innerText||'',detail_url:link?link.href:'',dom_key:domKey}})})()"
    )
    PAGER_EXPRESSION = (
        "(()=>({pages:[...document.querySelectorAll('[class*=PGT_pagerItem]')].filter(e=>{"
        "const s=getComputedStyle(e),r=e.getBoundingClientRect(),t=(e.innerText||'').trim();"
        "return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0&&/^\\d+$/.test(t)"
        "}).map(e=>({number:Number((e.innerText||'').trim()),active:String(e.className||'').includes('Active')}))}))()"
    )
    PENDING_FILTER_EXPRESSION = (
        "(()=>{const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();"
        "return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0};"
        "const items=[...document.querySelectorAll('[class*=BeastQuickFilter___item___]')].filter(e=>visible(e)&&(e.innerText||'').trim().startsWith('待处理'));"
        "if(items.length!==1)return {ready:false,reason:'pending_filter_not_unique',count:items.length};"
        "const item=items[0],card=item.querySelector('[class*=CAD_beastCard]')||item,r=card.getBoundingClientRect(),hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);"
        "return {ready:true,selected:String(card.className||'').includes('CAD_beastCardChecked'),text:(item.innerText||'').trim(),"
        "unblocked:hit===card||card.contains(hit),x:window.screenX+r.left+r.width/2,y:window.screenY+(window.outerHeight-window.innerHeight)+r.top+r.height/2}})()"
    )

    def __init__(self, config: dict[str, Any]):
        try:
            import win32clipboard
            import win32gui
            from pywinauto import Desktop, keyboard, mouse
        except ImportError as exc:
            raise PddDependencyError("缺少pywinauto/pywin32，请运行启动脚本安装依赖") from exc
        self.config = config
        self.win32clipboard = win32clipboard
        self.win32gui = win32gui
        self.Desktop = Desktop
        self.keyboard = keyboard
        self.mouse = mouse

    def health(self) -> dict[str, Any]:
        diagnostics = self.diagnostics()
        return {
            "available": True,
            "visible_edge_windows": len(diagnostics),
            "pdd_list_windows": sum(item["page_kind"] == "list" for item in diagnostics),
            "login_required_windows": sum(item["login_required"] for item in diagnostics),
            "windows": diagnostics,
        }

    def _windows(self) -> list[Any]:
        pattern = self.config.get("window_title_pattern", ".*Microsoft.*Edge")
        return [
            item
            for item in self.Desktop(backend="win32").windows(title_re=pattern, visible_only=True)
            if item.friendly_class_name() == "Chrome_WidgetWin_1"
        ]

    @contextmanager
    def _preserve_clipboard(self) -> Iterator[None]:
        old = self._get_clipboard()
        try:
            yield
        finally:
            self._set_clipboard(old)

    def _get_clipboard(self) -> str:
        for attempt in range(10):
            try:
                self.win32clipboard.OpenClipboard()
                value = (
                    self.win32clipboard.GetClipboardData(self.win32clipboard.CF_UNICODETEXT)
                    if self.win32clipboard.IsClipboardFormatAvailable(
                        self.win32clipboard.CF_UNICODETEXT
                    )
                    else ""
                )
                self.win32clipboard.CloseClipboard()
                return str(value)
            except Exception:
                try:
                    self.win32clipboard.CloseClipboard()
                except Exception:
                    pass
                if attempt == 9:
                    raise
                time.sleep(0.05)
        return ""

    def _set_clipboard(self, value: str) -> None:
        for attempt in range(10):
            try:
                self.win32clipboard.OpenClipboard()
                self.win32clipboard.EmptyClipboard()
                self.win32clipboard.SetClipboardText(value)
                self.win32clipboard.CloseClipboard()
                return
            except Exception:
                try:
                    self.win32clipboard.CloseClipboard()
                except Exception:
                    pass
                if attempt == 9:
                    raise
                time.sleep(0.05)

    def _focus(self, window: Any) -> None:
        last_error: Exception | None = None
        for _ in range(3):
            try:
                if window.is_minimized():
                    window.restore()
                    time.sleep(0.3)
                window.set_focus()
            except Exception as exc:
                last_error = exc
                try:
                    rect = window.rectangle()
                    self.mouse.click(coords=(rect.left + min(250, rect.width() // 2), rect.top + 12))
                except Exception as click_exc:
                    last_error = click_exc
            time.sleep(0.25)
            if int(self.win32gui.GetForegroundWindow()) == int(window.handle):
                return
        raise PddReadOnlyError(f"Edge窗口未取得前台焦点，停止读取：{last_error or 'unknown'}")

    def _current_url(self, window: Any) -> str:
        ui_window = self.Desktop(backend="uia").window(handle=window.handle)
        for edit in ui_window.descendants(control_type="Edit"):
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

    def _page_text(self, window: Any) -> str:
        self._focus(window)
        rect = window.rectangle()
        point = self.config.get("body_point", [900, 700])
        with self._preserve_clipboard():
            self.mouse.click(coords=(rect.left + int(point[0]), rect.top + int(point[1])))
            self.keyboard.send_keys("^a")
            self.keyboard.send_keys("^c")
            time.sleep(0.25)
            return self._get_clipboard()

    def _accessible_names(self, window: Any) -> tuple[str, ...]:
        names: list[str] = []
        try:
            controls = self.Desktop(backend="uia").window(handle=window.handle).descendants()
        except Exception:
            return ()
        for control in controls:
            try:
                value = control.window_text().strip()
                if value:
                    names.append(value)
            except Exception:
                continue
        return tuple(names)

    def diagnostics(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for window in self._windows():
            url = self._current_url(window)
            parsed = urlparse(url)
            title = window.window_text()
            results.append(
                {
                    "handle": int(window.handle),
                    "title": title,
                    "url_origin_path": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                    "page_kind": classify_pdd_url(url),
                    "login_required": parsed.netloc.lower() == PDD_HOST and parsed.path.startswith("/login"),
                }
            )
        return results

    def snapshots(self) -> list[WindowSnapshot]:
        results: list[WindowSnapshot] = []
        for window in self._windows():
            try:
                url = self._current_url(window)
                if classify_pdd_url(url) != "list":
                    continue
                page_text = self._page_text(window)
                results.append(
                    WindowSnapshot(
                        handle=int(window.handle),
                        title=window.window_text(),
                        url=url,
                        page_text=page_text,
                        accessible_names=self._accessible_names(window),
                    )
                )
            except Exception:
                continue
        return results

    def refresh_and_wait(
        self, handle: int, expected_kind: str, expected_shop: str, required_text: str = ""
    ) -> WindowSnapshot:
        """Refresh a bound PDD window and fail closed until its identity is readable again."""
        window = self.Desktop(backend="win32").window(handle=handle)
        before_url = self._current_url(window)
        if classify_pdd_url(before_url) != expected_kind:
            raise PddReadOnlyError(
                f"刷新前页面类型不匹配：期望 {expected_kind}，实际 {classify_pdd_url(before_url)}"
            )
        self._focus(window)
        self.keyboard.send_keys("^r")
        deadline = time.monotonic() + float(self.config.get("refresh_timeout_seconds", 20))
        last_url = before_url
        last_text = ""
        stable_signature: tuple[Any, ...] | None = None
        stable_count = 0
        pending_filter_clicked = False
        while time.monotonic() < deadline:
            time.sleep(0.7)
            last_url = self._current_url(window)
            if classify_pdd_url(last_url) != expected_kind:
                continue
            try:
                last_text = self._page_text(window)
            except Exception:
                continue
            if expected_shop not in last_text:
                continue
            if required_text and required_text not in last_text:
                continue
            if expected_kind == "list":
                try:
                    pending = self.prompt_json(int(window.handle), self.PENDING_FILTER_EXPRESSION)
                except Exception:
                    continue
                if pending.get("ready") is not True or pending.get("unblocked") is not True:
                    continue
                if pending.get("selected") is not True:
                    if pending_filter_clicked:
                        continue
                    self._focus(window)
                    self.mouse.click(coords=(int(round(float(pending["x"]))), int(round(float(pending["y"])))))
                    pending_filter_clicked = True
                    stable_signature = None
                    stable_count = 0
                    time.sleep(0.7)
                    continue
                try:
                    cards = self.cards(int(window.handle))
                    total = validate_complete_snapshot(last_text, cards)
                    signature = (total, tuple(sorted(card_identity(card) for card in cards)))
                except Exception:
                    stable_signature = None
                    stable_count = 0
                    continue
                if signature != stable_signature:
                    stable_signature = signature
                    stable_count = 1
                    continue
                stable_count += 1
                if stable_count < 2:
                    continue
            return WindowSnapshot(
                handle=int(window.handle),
                title=window.window_text(),
                url=last_url,
                page_text=last_text,
                accessible_names=self._accessible_names(window),
            )
        raise PddReadOnlyError(
            f"刷新后页面未恢复到可采集状态：kind={expected_kind}, url={last_url}, shop={expected_shop}"
        )

    def _read_prompt(self, window: Any) -> str:
        timeout = float(self.config.get("javascript_prompt_timeout_seconds", 3))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ui_window = self.Desktop(backend="uia").window(handle=window.handle)
            edits = [
                item
                for item in ui_window.descendants(control_type="Edit")
                if str(item.element_info.name or "").strip() == "RPA"
            ]
            if len(edits) == 1:
                value = str(edits[0].get_value() or "")
                self.keyboard.send_keys("{ESC}")
                return value
            time.sleep(0.1)
        raise PddReadOnlyError("Edge未返回只读页面脚本结果")

    def _javascript_prompt(self, window: Any, expression: str) -> str:
        before = self._current_url(window)
        script = f"prompt('RPA',JSON.stringify({expression}))"
        self._focus(window)
        with self._preserve_clipboard():
            self.keyboard.send_keys("^l")
            time.sleep(float(self.config.get("address_bar_ready_wait_seconds", 0.5)))
            self.keyboard.send_keys("java", with_spaces=True, pause=0.04)
            self._set_clipboard("script:" + script)
            self.keyboard.send_keys("^v")
            self.keyboard.send_keys("{ENTER}")
            result = self._read_prompt(window)
        after = self._current_url(window)
        if before and after and before != after:
            raise PddReadOnlyError("只读脚本执行前后URL发生变化，已停止采集")
        return result

    def execute_script(self, handle: int, script: str) -> None:
        """在当前页面安装只读辅助脚本；执行前后URL必须保持一致。"""
        window = self.Desktop(backend="win32").window(handle=handle)
        before = self._current_url(window)
        self._focus(window)
        with self._preserve_clipboard():
            self.keyboard.send_keys("^l")
            time.sleep(float(self.config.get("address_bar_ready_wait_seconds", 0.5)))
            self.keyboard.send_keys("java", with_spaces=True, pause=0.04)
            self._set_clipboard("script:" + script)
            self.keyboard.send_keys("^v")
            self.keyboard.send_keys("{ENTER}")
            time.sleep(0.35)
        after = self._current_url(window)
        if before and after and before != after:
            raise PddReadOnlyError("页面辅助脚本执行前后URL发生变化")

    def prompt_json(self, handle: int, expression: str) -> dict[str, Any]:
        window = self.Desktop(backend="win32").window(handle=handle)
        raw = self._javascript_prompt(window, expression)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PddReadOnlyError("页面辅助脚本结果无法解析") from exc
        if not isinstance(value, dict):
            raise PddReadOnlyError("页面辅助脚本未返回对象")
        return value

    def cards(self, handle: int) -> list[dict[str, Any]]:
        window = self.Desktop(backend="win32").window(handle=handle)
        attempts = int(self.config.get("script_read_attempts", 3))
        for attempt in range(attempts):
            raw = self._javascript_prompt(window, self.CARD_EXPRESSION)
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, list):
                return value
            if attempt + 1 < attempts:
                time.sleep(0.4)
        raise PddReadOnlyError("工单卡片脚本结果连续无法解析")

    def _pager(self, window: Any) -> dict[str, Any]:
        raw = self._javascript_prompt(window, self.PAGER_EXPRESSION)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PddReadOnlyError("分页信息无法解析") from exc
        if not isinstance(value, dict) or not isinstance(value.get("pages"), list):
            raise PddReadOnlyError("分页信息结构不正确")
        return value

    def _click_page(self, window: Any, number: int) -> None:
        expression = (
            "(()=>{const n=" + str(int(number)) + ",items=[...document.querySelectorAll(" 
            "'[class*=PGT_pagerItem]')].filter(e=>Number((e.innerText||'').trim())===n);"
            "if(items.length!==1)return {clicked:false,count:items.length};"
            "items[0].click();return {clicked:true}})()"
        )
        raw = self._javascript_prompt(window, expression)
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PddReadOnlyError(f"第{number}页点击结果无法解析") from exc
        if result.get("clicked") is not True:
            raise PddReadOnlyError(f"无法唯一定位第{number}页页签")

    def _wait_page_change(self, handle: int, previous: set[str]) -> list[dict[str, Any]]:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            time.sleep(0.4)
            cards = self.cards(handle)
            current = {card_identity(item) for item in cards}
            if current and current != previous:
                return cards
        raise PddReadOnlyError("切换分页后工单卡片未发生变化")

    def collect_cards(self, handle: int, total: int) -> list[dict[str, Any]]:
        visible = self.cards(handle)
        window = self.Desktop(backend="win32").window(handle=handle)
        pager = self._pager(window)
        active = next((int(item["number"]) for item in pager["pages"] if item.get("active")), 1)
        for item in visible:
            item["list_page_number"] = active
        collected = {card_identity(item): item for item in visible}
        pages = sorted({int(item["number"]) for item in pager["pages"]})
        if not pages:
            return list(collected.values())
        previous = {card_identity(item) for item in visible}
        try:
            for number in pages:
                if number == active:
                    continue
                self._click_page(window, number)
                visible = self._wait_page_change(handle, previous)
                for item in visible:
                    item["list_page_number"] = number
                previous = {card_identity(item) for item in visible}
                for item in visible:
                    identity = card_identity(item)
                    if identity in collected and collected[identity] != item:
                        raise PddReadOnlyError(f"跨页出现冲突工单：{identity}")
                    collected[identity] = item
        finally:
            current = self._pager(window)
            current_active = next(
                (int(item["number"]) for item in current["pages"] if item.get("active")),
                active,
            )
            if current_active != active:
                self._click_page(window, active)
        return list(collected.values())

    def screenshot(self, handle: int, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        window = self.Desktop(backend="win32").window(handle=handle)
        window.capture_as_image().save(path)


class PddEdgeReadOnlyConnector(PddConnector):
    name = "pdd.edge_readonly"
    supports_live_writes = False

    def __init__(
        self,
        config: dict[str, Any],
        evidence_dir: Path,
        session_factory: Callable[[dict[str, Any]], ReadOnlySession] = WindowsEdgeReadOnlySession,
    ):
        self.config = config
        self.evidence_dir = Path(evidence_dir)
        self.session_factory = session_factory

    def health(self) -> ConnectorResult:
        try:
            session = self.session_factory(self.config)
            return ConnectorResult(True, {"mode": "readonly", **session.health()})
        except Exception as exc:
            return ConnectorResult(False, {"mode": "readonly"}, error=str(exc))

    def discover(self, shop: dict[str, Any]) -> ConnectorResult:
        try:
            session = self.session_factory(self.config)
            snapshot = select_shop_snapshot(session.snapshots(), shop)
            refresh = getattr(session, "refresh_and_wait", None)
            if callable(refresh):
                snapshot = refresh(snapshot.handle, "list", str(shop["name"]), "工单管理")
            snapshot = select_shop_snapshot([snapshot], shop)
            initial_cards = session.cards(snapshot.handle)
            total = extract_total(snapshot.page_text)
            if total is None:
                validate_complete_snapshot(snapshot.page_text, initial_cards)
                total = 0
            cards = (
                session.collect_cards(snapshot.handle, total)
                if total > len(initial_cards)
                else initial_cards
            )
            total = validate_complete_snapshot(snapshot.page_text, cards)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            evidence = self.evidence_dir / f"pdd_{shop['id']}_{stamp}_readonly.png"
            session.screenshot(snapshot.handle, evidence)
            items = [parse_card(card, shop) for card in cards]
            for item in items:
                item["screenshots"] = []
                item["source_evidence"]["window_title"] = snapshot.title
                item["source_evidence"]["list_url"] = snapshot.url
                item["source_evidence"]["scan_screenshot"] = str(evidence)
            return ConnectorResult(
                True,
                {"items": items, "total": total, "shop_name": shop["name"]},
                evidence=[str(evidence)],
            )
        except Exception as exc:
            return ConnectorResult(False, {"items": []}, error=str(exc))
