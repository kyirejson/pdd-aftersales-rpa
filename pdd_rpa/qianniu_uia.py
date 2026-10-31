"""Edge/UIA controller for Qianniu unreceived-refund list and detail pages."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .connectors.pdd_edge_readonly import WindowsEdgeReadOnlySession
from .edge_uia import PddEdgeUiaSession
from .qianniu_edge import QianniuEdgeConfig, QianniuEdgeError
from .qianniu_playwright import QianniuPlaywrightBridge


QIANNIU_REFUND_LIST_URL = (
    "https://myseller.taobao.com/home.htm/trade-platform/refund-list"
)
QIANNIU_REFUND_DETAIL_URL = (
    QIANNIU_REFUND_LIST_URL + "/detail?disputeId={aftersale_no}"
)
QIANNIU_REFUND_LIST_PATH = "/home.htm/trade-platform/refund-list"
QIANNIU_REFUND_DETAIL_PATH = "/home.htm/trade-platform/refund-list/detail"
UNRECEIVED_REFUND_LABEL = "未收货退款"
PENDING_REFUND_STATUS = "退款待处理"
INTERCEPT_EXPRESS = "拦截快递"
AGREE_REFUND = "同意退款"
INTERCEPT_SUCCESS_FEEDBACK = "已告知用户您正在拦截，请联系快递进行操作"


class QianniuUiaError(RuntimeError):
    pass


class QianniuActionUnavailable(QianniuUiaError):
    """The page is valid, but the requested business action is no longer present."""

    pass


class _ForegroundGuardPrompt:
    """Physical-input proxy that forces the Qianniu window to OS foreground.

    Playwright's ``bring_to_front`` only activates the tab inside the browser
    and cannot break the Windows foreground lock.  pywinauto mouse/keyboard
    events therefore landed on whichever Edge window happened to be foreground
    (verified live: a PDD shop window), which is why the notification close
    click silently did nothing.  Every physical input is now preceded by a
    real Win32 foreground switch on the verified Qianniu window; when the
    switch fails the click is refused instead of hitting a foreign window.
    """

    def __init__(self, prompt: Any, session: "QianniuUiaSession") -> None:
        self._prompt = prompt
        self._session = session

    @property
    def mouse(self) -> Any:
        self._session.ensure_foreground()
        return self._prompt.mouse

    @property
    def keyboard(self) -> Any:
        self._session.ensure_foreground()
        return self._prompt.keyboard

    def screenshot(self, handle: int, path: Any) -> Any:
        return self._prompt.screenshot(handle, path)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._prompt, name)


def classify_qianniu_url(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    if parsed.scheme != "https" or parsed.hostname != "myseller.taobao.com":
        return "other"
    path = parsed.path.rstrip("/")
    if path == QIANNIU_REFUND_LIST_PATH:
        return "refund_list"
    if path == QIANNIU_REFUND_DETAIL_PATH:
        return "refund_detail"
    return "other"


def detail_aftersale_no(value: str) -> str:
    if classify_qianniu_url(value) != "refund_detail":
        return ""
    return str((parse_qs(urlsplit(value).query).get("disputeId") or [""])[0])


@dataclass(frozen=True, slots=True)
class QianniuAftersaleRecord:
    aftersale_no: str
    order_no: str
    shipping_status: str
    aftersale_status: str
    refund_amount: float
    refund_type: str
    application_time: str
    available_actions: tuple[str, ...]

    @property
    def processable(self) -> bool:
        return (
            self.refund_type == UNRECEIVED_REFUND_LABEL
            and self.aftersale_status == PENDING_REFUND_STATUS
            and "拦截快递" in self.available_actions
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "processable": self.processable}


def record_from_raw(raw: dict[str, Any]) -> QianniuAftersaleRecord:
    aftersale_no = str(raw.get("aftersale_no") or "").strip()
    order_no = str(raw.get("order_no") or "").strip()
    if not re.fullmatch(r"\d{15,24}", aftersale_no):
        raise QianniuUiaError(f"千牛售后单号无效：{aftersale_no or '空'}")
    if not re.fullmatch(r"\d{15,24}", order_no):
        raise QianniuUiaError(f"千牛订单号无效：{order_no or '空'}")
    try:
        amount = float(raw.get("refund_amount") or 0)
    except (TypeError, ValueError) as exc:
        raise QianniuUiaError("千牛退款金额无法解析") from exc
    return QianniuAftersaleRecord(
        aftersale_no=aftersale_no,
        order_no=order_no,
        shipping_status=str(raw.get("shipping_status") or "").strip(),
        aftersale_status=str(raw.get("aftersale_status") or "").strip(),
        refund_amount=amount,
        refund_type=str(raw.get("refund_type") or "").strip(),
        application_time=str(raw.get("application_time") or "").strip(),
        available_actions=tuple(
            str(item or "").strip()
            for item in (raw.get("available_actions") or [])
            if str(item or "").strip()
        ),
    )


class QianniuUiaSession:
    """Semantic UIA controller; all clicks are preceded by an exact DOM readback."""

    def __init__(
        self,
        config: QianniuEdgeConfig,
        *,
        uia_factory: Callable[[dict[str, Any]], Any] = PddEdgeUiaSession,
        prompt_factory: Callable[[dict[str, Any]], Any] = WindowsEdgeReadOnlySession,
        browser_factory: Callable[[QianniuEdgeConfig], Any] = QianniuPlaywrightBridge,
    ) -> None:
        self.config = config
        raw = {
            "window_title_pattern": config.window_title_pattern,
            "javascript_prompt_timeout_seconds": 5,
            "address_bar_ready_wait_seconds": 0.35,
        }
        self.uia = uia_factory(raw)
        self.prompt: Any = _ForegroundGuardPrompt(prompt_factory(raw), self)
        self.browser = browser_factory(config)
        self._foreground_handle: int | None = None
        self._foreground_until: float = 0.0

    def ensure_foreground(self) -> None:
        """把已验证的千牛窗口真正切到操作系统前台，失败则拒绝物理输入。

        ``AttachThreadInput`` + ``SetForegroundWindow`` 是后台进程突破
        Windows 前台锁的标准做法；一次成功后的一秒内免重复切换，
        连续点击（关闭图标、确认弹窗）不会被反复打断。
        """
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if time.monotonic() < self._foreground_until:
            return
        handle = self._foreground_handle
        if not handle or not user32.IsWindow(handle):
            window = self.window()
            handle = int(window.handle)
            self._foreground_handle = handle
        if user32.GetForegroundWindow() == handle:
            self._foreground_until = time.monotonic() + 1.0
            return
        foreground = user32.GetForegroundWindow()
        foreground_thread = (
            user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        )
        current_thread = kernel32.GetCurrentThreadId()
        attached = False
        if foreground_thread and foreground_thread != current_thread:
            attached = bool(
                user32.AttachThreadInput(current_thread, foreground_thread, True)
            )
        try:
            user32.SetForegroundWindow(handle)
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
        time.sleep(0.08)
        if user32.GetForegroundWindow() == handle:
            self._foreground_until = time.monotonic() + 1.0
            return
        # 最小化后立即还原可以从系统拿到前台权；从最大化状态最小化的
        # 窗口还原后仍回到最大化，不会改变窗口尺寸。
        user32.ShowWindow(handle, 6)  # SW_MINIMIZE
        user32.ShowWindow(handle, 9)  # SW_RESTORE
        time.sleep(0.15)
        if user32.GetForegroundWindow() != handle:
            raise QianniuUiaError("千牛窗口无法置于前台，已拒绝物理点击")
        self._foreground_until = time.monotonic() + 1.0

    def _current_url(self, window: Any) -> str:
        return str(self.uia._current_url(window) or "")

    def _shop_verified(self, window: Any) -> bool:
        try:
            return bool(
                self.uia._has_exact_name(int(window.handle), self.config.shop_name)
            )
        except Exception:
            return self.config.shop_name in str(window.window_text() or "")

    def window(self, *, allowed_kinds: set[str] | None = None) -> Any:
        allowed = allowed_kinds or {"refund_list", "refund_detail"}
        if hasattr(self, "browser"):
            try:
                self.browser.ensure_page(
                    self.config.url if allowed == {"refund_list"} else None
                )
            except Exception as exc:
                raise QianniuUiaError(f"千牛专用浏览器恢复失败：{exc}") from exc
        candidates = []
        for window in self.uia._windows():
            url = self._current_url(window)
            if classify_qianniu_url(url) not in allowed:
                continue
            verified = self._shop_verified(window)
            if not verified:
                try:
                    identity = self.browser.evaluate(
                        f"(()=>({{shop:(document.body.innerText||'').includes('{self.config.shop_name}')}}))()",
                    )
                    verified = bool(identity.get("shop"))
                except Exception:
                    verified = False
            if verified:
                candidates.append(window)
        unique = {int(window.handle): window for window in candidates}
        if len(unique) != 1:
            raise QianniuUiaError(
                f"示例天猫旗舰店千牛窗口数量异常：{len(unique)}"
            )
        return next(iter(unique.values()))

    def _window_with_retry(
        self, allowed_kinds: set[str], *, timeout_seconds: float
    ) -> Any:
        """轮询解析窗口；页面加载与地址栏更新期间的瞬时“0 窗口”不算失败。

        刚导航完成的页面要过几百毫秒才渲染出店铺名，UIA 地址栏读取也
        存在滞后，单次 window() 在这段窗口期会抛“数量异常：0”。调用方
        的重试节奏本意就是等它就绪，这里把瞬时失败吞掉直到超时。
        """

        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return self.window(allowed_kinds=allowed_kinds)
            except QianniuUiaError as exc:
                last_error = exc
                time.sleep(0.35)
        raise last_error or QianniuUiaError("千牛窗口解析超时")

    def _probe(self, window: Any, expression: str) -> dict[str, Any]:
        try:
            del window
            return self.browser.evaluate(expression)
        except Exception as exc:
            raise QianniuUiaError(f"千牛页面语义读取失败：{exc}") from exc

    def dismiss_obstructions(self, window: Any | None = None) -> dict[str, Any]:
        """Close only recognized Qianniu notification/dialog overlays.

        A bottom-right important-message card can cover the row operation
        column and contains its own ``查看详情`` button.  The close icon has no
        accessible name, so identify it only inside the exact notification
        container and verify the container disappears after the physical click.

        Real-page verification showed new important messages can keep
        arriving while older cards are being closed, so a fixed click budget
        followed by a single presence check fails even though every click
        worked.  Compare the card text between probes instead: a changed card
        means the previous card was dismissed and a new one appeared; an
        unchanged card that survives consecutive close clicks means the close
        control is ineffective and the session must fail closed.
        """

        target_window = window or self.window()
        expression = """(()=>{const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'},containers=[...document.querySelectorAll('[class*=notify_container]')].filter(visible),closes=containers.flatMap(c=>[...c.querySelectorAll('i.next-icon-close_blod')].filter(visible)),unique=[...new Set(closes)];if(containers.length===0)return {present:false,containers:0,closes:0};if(containers.length!==1||unique.length!==1)return {present:true,containers:containers.length,closes:unique.length};const e=unique[0],r=e.getBoundingClientRect();return {present:true,containers:1,closes:1,text:(containers[0].innerText||'').replace(/\\s+/g,' ').slice(0,120),point:{x:Math.round(screenX+r.left+r.width/2),y:Math.round(screenY+(outerHeight-innerHeight)+r.top+r.height/2)}}})()"""
        closed = 0
        same_card_probes = 0
        last_text: str | None = None
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            state = self._probe(target_window, expression)
            if not state.get("present"):
                return {
                    "ok": True,
                    "closed": closed,
                    "notification_present": False,
                    "write_performed": False,
                }
            if int(state.get("containers") or 0) != 1 or int(
                state.get("closes") or 0
            ) != 1:
                raise QianniuUiaError(
                    "千牛消息遮挡层或关闭控件不唯一，已禁止盲点"
                )
            card_text = str(state.get("text") or "")
            if card_text == last_text:
                same_card_probes += 1
                if same_card_probes >= 3:
                    raise QianniuUiaError(
                        "千牛右下角消息层多次点击关闭后仍未消失"
                    )
            else:
                same_card_probes = 1
                last_text = card_text
            self.prompt.mouse.click(coords=self._screen_point(state, "point"))
            closed += 1
            time.sleep(0.6)
        after = self._probe(target_window, expression)
        if after.get("present"):
            raise QianniuUiaError("千牛右下角消息层持续弹出，30秒内未能清空")
        return {
            "ok": True,
            "closed": closed,
            "notification_present": False,
            "write_performed": False,
        }

    @staticmethod
    def _screen_point(data: dict[str, Any], key: str) -> tuple[int, int]:
        value = data.get(key) or {}
        try:
            return int(value["x"]), int(value["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise QianniuUiaError(f"千牛控件坐标缺失：{key}") from exc

    @staticmethod
    def _parse_logistics_carrier_line(logistics_text: str) -> tuple[str, str]:
        """Read one carrier/waybill line without accepting trajectory phones."""

        pattern = re.compile(
            r"^\s*([\u4e00-\u9fffA-Za-z0-9·—_\-]{1,20}(?:快递|速递|物流))"
            r"\s+([A-Za-z0-9]{8,30})(?:复制)?\s*$",
            re.IGNORECASE,
        )
        matches = []
        for line in str(logistics_text or "").splitlines():
            match = pattern.fullmatch(line.strip())
            if match:
                matches.append((match.group(1).strip(), match.group(2).upper()))
        unique = list(dict.fromkeys(matches))
        return unique[0] if len(unique) == 1 else ("", "")

    def ensure_unreceived_refund_filter(self) -> dict[str, Any]:
        window = self.window(allowed_kinds={"refund_list"})
        self.dismiss_obstructions(window)
        expression = """(()=>{const body=document.body.innerText||'',items=[...document.querySelectorAll('.next-tabs-tab')].filter(e=>(e.innerText||'').trim()==='未收货退款');if(items.length!==1)return {ready:false,count:items.length};const e=items[0],r=e.getBoundingClientRect();return {ready:body.includes('示例天猫旗舰店'),count:1,active:e.classList.contains('active')||e.getAttribute('aria-selected')==='true',point:{x:Math.round(screenX+r.left+r.width/2),y:Math.round(screenY+(outerHeight-innerHeight)+r.top+r.height/2)}}})()"""
        # After close_detail navigates back, the tab strip renders later than
        # the body text, so a one-shot probe can fire before the tab exists.
        before: dict[str, Any] = {}
        deadline = time.monotonic() + max(
            5.0, float(self.config.launch_verify_timeout_seconds)
        )
        while time.monotonic() < deadline:
            try:
                before = self._probe(window, expression)
            except QianniuUiaError:
                before = {}
            if before.get("ready") and int(before.get("count") or 0) == 1:
                break
            time.sleep(0.5)
        if not before.get("ready") or int(before.get("count") or 0) != 1:
            raise QianniuUiaError("未收货退款筛选项未唯一识别")
        if not before.get("active"):
            self.prompt.mouse.click(coords=self._screen_point(before, "point"))
        after: dict[str, Any] = before
        deadline = time.monotonic() + max(
            5.0, float(self.config.launch_verify_timeout_seconds)
        )
        while time.monotonic() < deadline:
            try:
                after = self._probe(window, expression)
            except QianniuUiaError:
                after = {}
            if (
                after.get("ready")
                and int(after.get("count") or 0) == 1
                and after.get("active")
            ):
                break
            time.sleep(0.35)
        if not after.get("active"):
            raise QianniuUiaError("点击后等待页面重绘，未收货退款筛选项仍未选中")
        return {
            "ok": True,
            "label": UNRECEIVED_REFUND_LABEL,
            "clicked": not bool(before.get("active")),
            "selected": True,
            "write_performed": False,
        }

    def ensure_refund_list_window(self) -> Any:
        """Return the dedicated shop window to the fixed refund-list route."""
        try:
            self.browser.goto(QIANNIU_REFUND_LIST_URL)
        except Exception as exc:
            raise QianniuUiaError(f"千牛退款/售后页面恢复失败：{exc}") from exc
        deadline = time.monotonic() + max(
            5.0, float(self.config.launch_verify_timeout_seconds)
        )
        ready: dict[str, Any] = {}
        expression = f"""(()=>{{const t=document.body.innerText||'';return {{shop:t.includes('{self.config.shop_name}'),refund_list:t.includes('退款管理')}}}})()"""
        while time.monotonic() < deadline:
            time.sleep(0.35)
            try:
                window = self.window(allowed_kinds={"refund_list"})
            except QianniuUiaError:
                continue
            if classify_qianniu_url(self._current_url(window)) != "refund_list":
                continue
            try:
                ready = self._probe(window, expression)
            except QianniuUiaError:
                continue
            if ready.get("shop") and ready.get("refund_list"):
                return window
        raise QianniuUiaError("千牛详情页返回退款/售后列表失败")

    def refresh_refund_list(self) -> dict[str, Any]:
        """Refresh the verified refund list once before each collection pass."""

        window = self.ensure_refund_list_window()
        try:
            self.browser.reload()
        except Exception as exc:
            raise QianniuUiaError(f"刷新千牛退款/售后列表失败：{exc}") from exc
        deadline = time.monotonic() + max(
            5.0,
            float(
                getattr(
                    self.config,
                    "refresh_verify_timeout_seconds",
                    max(30.0, float(self.config.launch_verify_timeout_seconds)),
                )
            ),
        )
        ready: dict[str, Any] = {}
        last_url = ""
        probe_errors = 0
        expression = f"""(()=>{{const body=document.body.innerText||'',tabs=[...document.querySelectorAll('.next-tabs-tab')].filter(e=>(e.innerText||'').trim()==='{UNRECEIVED_REFUND_LABEL}');const shop=body.includes('{self.config.shop_name}'),refund_list=body.includes('退款管理');return {{ready:shop&&refund_list&&tabs.length===1,shop:shop,refund_list:refund_list,tab_count:tabs.length,body_length:body.length}}}})()"""
        while time.monotonic() < deadline:
            time.sleep(0.35)
            last_url = self._current_url(window)
            if classify_qianniu_url(last_url) != "refund_list":
                continue
            try:
                ready = self._probe(window, expression)
            except QianniuUiaError:
                probe_errors += 1
                continue
            if ready.get("ready"):
                break
        if not ready.get("ready"):
            raise QianniuUiaError(
                "刷新后千牛退款/售后列表未在限定时间内恢复："
                f"URL={last_url or '<空>'}，店铺={bool(ready.get('shop'))}，"
                f"退款管理={bool(ready.get('refund_list'))}，"
                f"未收货退款页签={int(ready.get('tab_count') or 0)}，"
                f"语义读取失败次数={probe_errors}"
            )
        obstruction = self.dismiss_obstructions(window)
        return {
            "ok": True,
            "refreshed": True,
            "notification_closed": int(obstruction.get("closed") or 0),
            "write_performed": False,
        }

    @staticmethod
    def _scan_expression(start: int = 0, end: int = 100) -> str:
        # Native JavaScript prompt replies are truncated at roughly 1 KiB on
        # this Edge build. Return compact row arrays and read at most ten rows
        # per prompt; start=end=0 is a small pager-only probe.
        return f"""(()=>{{const text=document.body.innerText||'',segments=text.split(/售后单号\\s+/).slice(1),rows=[];for(const segment of segments){{const lines=segment.split(/\\n/).map(x=>x.trim()).filter(Boolean),a=(segment.match(/^(\\d+)/)||[])[1]||'',o=(segment.match(/订单编号\\s*(\\d+)/)||[])[1]||'';if(!a||!o||rows.some(x=>x[0]===a))continue;const ship=(segment.match(/商家发货\\s*(已发货|已收货|未发货)/)||[])[1]||'',status=(segment.match(/售后状态\\s*([^\\n]+)/)||[])[1]||'',amount=(segment.match(/退款总额\\s*￥?([\\d.]+)/)||[])[1]||'',time=(segment.match(/申请时间\\s*([0-9:-]+(?:\\s+[0-9:]+)?)/)||[])[1]||'',mask=(lines.includes('同意退款')?1:0)|(lines.includes('拦截快递')?2:0)|(lines.includes('协商退货退款')?4:0)|(lines.includes('拒绝申请')?8:0)|(lines.includes('查看详情')?16:0);rows.push([a,o,ship==='已发货'?'F':ship==='已收货'?'R':ship==='未发货'?'N':'',status==='退款待处理'?'P':'O',amount,time,mask])}}const visible=e=>{{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'}},controls=[...document.querySelectorAll('button,a')].filter(visible),isDisabled=e=>e.disabled||e.getAttribute('aria-disabled')==='true'||String(e.className||'').includes('disabled'),pos=e=>{{const r=e.getBoundingClientRect();return {{x:Math.round(screenX+r.left+r.width/2),y:Math.round(screenY+(outerHeight-innerHeight)+r.top+r.height/2)}}}},nexts=controls.filter(e=>String(e.className||'').includes('next-pagination-next')||/下一页/.test((e.getAttribute('aria-label')||'')+(e.getAttribute('title')||''))||((e.innerText||'').trim()==='>'&&String(e.className||'').includes('pagination'))),prevs=controls.filter(e=>String(e.className||'').includes('next-pagination-prev')||/上一页/.test((e.getAttribute('aria-label')||'')+(e.getAttribute('title')||''))||((e.innerText||'').trim()==='<'&&String(e.className||'').includes('pagination'))),current=[...document.querySelectorAll('[aria-current=page],.next-pagination-item.current,.next-pagination-item.active')].find(visible),next=nexts.length===1?nexts[0]:null,prev=prevs.length===1?prevs[0]:null;return {{total:rows.length,rows:rows.slice({start},{end}),pagination:{{current_page:parseInt((current&&current.innerText)||'1',10)||1,next_count:nexts.length,has_next:!!next&&!isDisabled(next),next:next?pos(next):null,prev_count:prevs.length,has_prev:!!prev&&!isDisabled(prev),prev:prev?pos(prev):null}}}}}})()"""

    @staticmethod
    def _records_from_scan_payload(
        payload: dict[str, Any]
    ) -> list[QianniuAftersaleRecord]:
        raw_rows = payload.get("rows") or []
        if not isinstance(raw_rows, list):
            raise QianniuUiaError("千牛售后列表脚本返回格式异常")
        actions = (
            (1, "同意退款"),
            (2, "拦截快递"),
            (4, "协商退货退款"),
            (8, "拒绝申请"),
            (16, "查看详情"),
        )
        shipping = {"F": "已发货", "R": "已收货", "N": "未发货"}
        records: list[QianniuAftersaleRecord] = []
        for item in raw_rows:
            if isinstance(item, dict):
                records.append(record_from_raw(dict(item)))
                continue
            if not isinstance(item, list) or len(item) != 7:
                raise QianniuUiaError("千牛售后紧凑行格式异常")
            mask = int(item[6] or 0)
            records.append(
                record_from_raw(
                    {
                        "aftersale_no": item[0],
                        "order_no": item[1],
                        "shipping_status": shipping.get(str(item[2]), ""),
                        "aftersale_status": (
                            "退款待处理" if str(item[3]) == "P" else "非退款待处理"
                        ),
                        "refund_amount": item[4],
                        "refund_type": UNRECEIVED_REFUND_LABEL,
                        "application_time": item[5],
                        "available_actions": [
                            label for bit, label in actions if mask & bit
                        ],
                    }
                )
            )
        return records

    @staticmethod
    def _first_row_identity(payload: dict[str, Any]) -> str:
        rows = payload.get("rows") or []
        if not rows:
            return ""
        first = rows[0]
        if isinstance(first, dict):
            return str(first.get("aftersale_no") or "")
        try:
            return str(first[0] or "")
        except (IndexError, TypeError, KeyError):
            return ""

    def _wait_page_turn(
        self,
        window: Any,
        *,
        previous_first: str,
        pagination: dict[str, Any],
        direction: str,
    ) -> dict[str, Any]:
        """点击上/下一页并轮询等待列表内容真正变化。

        真实页面上点击后固定等 1 秒并不够：React 列表翻页要等接口返回与
        重渲染，过早读取会拿到旧一页的内容，被上层误判为“翻页未生效”。
        这里轮询至本页首行售后单号变化为止；超时后先清一次右下角消息
        遮挡、重新校准按钮位置再补点一次，仍未变化则把结果交回上层签名
        校验去报错。
        """

        def _probe_changed() -> dict[str, Any] | None:
            current = self._probe(window, self._scan_expression(0, 10))
            current_first = self._first_row_identity(current)
            if current_first and current_first != previous_first:
                return current
            return None

        for _attempt in range(2):
            self.dismiss_obstructions(window)
            self.prompt.mouse.click(coords=self._screen_point(pagination, direction))
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                time.sleep(0.5)
                changed = _probe_changed()
                if changed is not None:
                    return changed
            # 8 秒未变化：可能点击落空或按钮被重渲染移动，读取最新分页再试。
            fresh = self._probe(window, self._scan_expression(0, 10))
            pagination = dict(fresh.get("pagination") or {})
            if int(pagination.get(f"{direction}_count") or 0) != 1:
                return fresh
        return fresh

    def scan_unreceived_refunds(
        self, *, refresh: bool = True
    ) -> list[QianniuAftersaleRecord]:
        if refresh:
            self.refresh_refund_list()
        self.ensure_unreceived_refund_filter()
        window = self.window(allowed_kinds={"refund_list"})
        records: list[QianniuAftersaleRecord] = []
        seen_pages: set[tuple[str, ...]] = set()
        payload = self._probe(window, self._scan_expression(0, 10))
        # Refreshing the list can preserve the operator's current page. Walk
        # back to page 1 before collecting so a run started on page 2 never
        # silently omits newer records.
        for _rewind in range(100):
            pagination = dict(payload.get("pagination") or {})
            if not pagination.get("has_prev"):
                break
            if int(pagination.get("prev_count") or 0) != 1:
                raise QianniuUiaError("千牛售后上一页按钮未唯一识别")
            prev_point = self._screen_point(pagination, "prev")
            if prev_point[1] > 1200:
                self.browser.press("Control+End")
                time.sleep(0.6)
                payload = self._probe(window, self._scan_expression(0, 10))
                pagination = dict(payload.get("pagination") or {})
                if not pagination.get("has_prev"):
                    break
            payload = self._wait_page_turn(
                window,
                previous_first=self._first_row_identity(payload),
                pagination=pagination,
                direction="prev",
            )
        else:
            raise QianniuUiaError("千牛售后回到第一页超过100次，已停止采集")

        for _page_index in range(100):
            observed_total = int(payload.get("total") or 0)
            page_records = self._records_from_scan_payload(payload)
            for start in range(10, observed_total, 10):
                chunk_payload = self._probe(
                    window,
                    self._scan_expression(start, min(start + 10, observed_total)),
                )
                if int(chunk_payload.get("total") or 0) != observed_total:
                    raise QianniuUiaError(
                        "千牛售后列表在分块读取期间发生变化，请稍后重试"
                    )
                page_records.extend(
                    self._records_from_scan_payload(chunk_payload)
                )
            signature = tuple(item.aftersale_no for item in page_records)
            if signature in seen_pages:
                raise QianniuUiaError("千牛售后翻页后页面内容未变化")
            seen_pages.add(signature)
            records.extend(page_records)
            pagination = dict(payload.get("pagination") or {})
            if not pagination.get("has_next"):
                break
            if int(pagination.get("next_count") or 0) != 1:
                raise QianniuUiaError("千牛售后下一页按钮未唯一识别")
            # The pager is below the long list. DOM geometry is document-
            # relative until the browser is scrolled, so an off-screen point
            # (live observed y=4720) cannot be clicked directly.
            next_point = self._screen_point(pagination, "next")
            if next_point[1] > 1200:
                self.browser.press("Control+End")
                time.sleep(0.6)
                payload = self._probe(window, self._scan_expression(0, 10))
                pagination = dict(payload.get("pagination") or {})
                if not pagination.get("has_next"):
                    break
                if int(pagination.get("next_count") or 0) != 1:
                    raise QianniuUiaError("滚动后千牛售后下一页按钮未唯一识别")
            payload = self._wait_page_turn(
                window,
                previous_first=self._first_row_identity(payload),
                pagination=pagination,
                direction="next",
            )
        else:
            raise QianniuUiaError("千牛售后翻页超过100页，已停止采集")
        identities = [item.aftersale_no for item in records]
        if len(identities) != len(set(identities)):
            raise QianniuUiaError("千牛售后列表出现重复售后单号")
        return records

    def open_detail(self, order_no: str, aftersale_no: str) -> dict[str, Any]:
        order = str(order_no or "").strip()
        aftersale = str(aftersale_no or "").strip()
        if not re.fullmatch(r"\d{15,24}", order):
            raise QianniuUiaError("千牛订单号格式无效")
        if not re.fullmatch(r"\d{15,24}", aftersale):
            raise QianniuUiaError("千牛售后单号格式无效")
        detail_url = QIANNIU_REFUND_DETAIL_URL.format(aftersale_no=aftersale)
        try:
            self.browser.goto(detail_url)
        except Exception as exc:
            raise QianniuUiaError(f"千牛详情页导航失败：{exc}") from exc
        deadline = time.monotonic() + max(
            12.0, float(self.config.launch_verify_timeout_seconds)
        )
        current_url = ""
        while time.monotonic() < deadline:
            try:
                window = self.window(allowed_kinds={"refund_detail"})
            except QianniuUiaError:
                window = None
            if window is not None:
                current_url = self._current_url(window)
                if detail_aftersale_no(current_url) == aftersale:
                    break
            time.sleep(0.25)
        if detail_aftersale_no(current_url) != aftersale:
            raise QianniuUiaError("千牛详情页导航后未进入目标售后单详情页")
        return {
            "ok": True,
            "record": {"order_no": order, "aftersale_no": aftersale},
            "detail_url": current_url,
            "write_performed": False,
        }

    def _ensure_detail_window(self, aftersale_no: str) -> Any:
        aftersale = str(aftersale_no or "").strip()
        if not re.fullmatch(r"\d{15,24}", aftersale):
            raise QianniuUiaError("千牛售后单号格式无效")
        detail_url = QIANNIU_REFUND_DETAIL_URL.format(aftersale_no=aftersale)
        try:
            self.browser.goto(detail_url)
        except Exception as exc:
            raise QianniuUiaError(f"千牛详情页自动恢复失败：{exc}") from exc
        return self._window_with_retry(
            {"refund_detail"},
            timeout_seconds=max(12.0, float(self.config.launch_verify_timeout_seconds)),
        )

    def _detail_snapshot(
        self,
        window: Any,
        *,
        order_no: str,
        aftersale_no: str,
        include_logistics: bool,
    ) -> dict[str, Any]:
        current_url = self._current_url(window)
        if detail_aftersale_no(current_url) != str(aftersale_no):
            raise QianniuUiaError("当前千牛详情页不是目标售后单")
        logistics_part = (
            "const start=t.indexOf('发货物流信息：'),end=t.indexOf('同意退款：',start),"
            "logistics=start>=0?t.slice(start+7,end>start?end:start+900).trim():'',"
            "lines=logistics.split(/\\n/).map(x=>x.trim()).filter(Boolean);"
            if include_logistics
            else "const logistics='',lines=[];"
        )
        expression = f"""(()=>{{const t=document.body.innerText||'';{logistics_part}const buttons=[...document.querySelectorAll('button')].map(e=>(e.innerText||'').trim()),actions=['同意退款','拦截快递','协商退货退款','拒绝申请'].filter(x=>buttons.includes(x)),m=t.match(/售后状态[：:]?\\s*([^\\n]+)/),status=((m&&m[1])||(t.includes('退款待处理')?'退款待处理':(t.includes('请处理退款申请')?'请处理退款申请':''))).trim(),refundType=t.includes('未收货退款')?'未收货退款':'',carrierLines=lines.filter(x=>/^[\\u4e00-\\u9fffA-Za-z0-9·—_-]{{1,20}}(?:快递|速递|物流)\\s+[A-Za-z0-9]{{8,30}}(?:复制)?$/.test(x)).slice(0,3);return {{shop:t.includes('{self.config.shop_name}'),order:t.includes('{order_no}'),aftersale:'{aftersale_no}',aftersale_status:status,refund_type:refundType,actions:actions,processable:(status.includes('退款待处理')||status.includes('请处理退款申请'))&&actions.includes('拦截快递'),logistics_text:logistics.slice(0,450),latest_logistics_text:lines[0]||'',carrier_lines:carrierLines}}}})()"""
        payload = self._probe(window, expression)
        if not payload.get("shop") or not payload.get("order"):
            raise QianniuUiaError("千牛详情读取前店铺或订单身份复核失败")
        return {
            "ok": True,
            "shop_name": self.config.shop_name,
            "order_no": str(order_no),
            "aftersale_no": str(aftersale_no),
            "detail_url": current_url,
            "aftersale_status": str(payload.get("aftersale_status") or "").strip(),
            "refund_type": str(payload.get("refund_type") or "").strip(),
            "available_actions": list(payload.get("actions") or []),
            "processable": bool(payload.get("processable")),
            "logistics_text": str(payload.get("logistics_text") or "").strip(),
            "latest_logistics_text": str(
                payload.get("latest_logistics_text") or ""
            ).strip(),
            "carrier_lines": list(payload.get("carrier_lines") or []),
            "write_performed": False,
        }

    def read_current_state(
        self, *, order_no: str, aftersale_no: str
    ) -> dict[str, Any]:
        """Read the minimum live state used to detect an operator race."""

        window = self._ensure_detail_window(aftersale_no)
        return self._detail_snapshot(
            window,
            order_no=order_no,
            aftersale_no=aftersale_no,
            include_logistics=False,
        )

    def read_detail(
        self, *, order_no: str, aftersale_no: str
    ) -> dict[str, Any]:
        window = self._ensure_detail_window(aftersale_no)
        obstruction_result = self.dismiss_obstructions(window)
        result = self._detail_snapshot(
            window,
            order_no=order_no,
            aftersale_no=aftersale_no,
            include_logistics=True,
        )
        result["obstruction_cleanup"] = obstruction_result
        if not result["processable"]:
            return result
        logistics_text = str(result.get("logistics_text") or "").strip()
        carrier_source = "\n".join(
            str(item or "") for item in result.get("carrier_lines") or []
        ) or logistics_text
        carrier, tracking_no = self._parse_logistics_carrier_line(carrier_source)
        if not re.fullmatch(r"[A-Z0-9]{8,30}", tracking_no):
            raise QianniuUiaError("千牛详情未读取到唯一有效物流单号")
        if not logistics_text:
            raise QianniuUiaError("千牛详情发货物流信息为空")
        result.update({"carrier": carrier, "tracking_no": tracking_no})
        return result

    def read_logistics_trajectory(
        self, *, order_no: str, aftersale_no: str
    ) -> dict[str, Any]:
        """Hover Tmall's logistics balloon and count its direct LI nodes.

        The real page hides the balloon as soon as the mouse leaves “查看”.
        Keep it alive by first hovering that exact span, locating the one
        visible vertical dot-step list, then moving inside the balloon before
        performing the stable count readback.
        """

        window = self._ensure_detail_window(aftersale_no)
        self.dismiss_obstructions(window)
        self.read_current_state(order_no=order_no, aftersale_no=aftersale_no)
        view_expression = """(()=>{const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'},items=[...document.querySelectorAll('span')].filter(e=>visible(e)&&(e.innerText||'').trim()==='查看'&&String(e.className||'').includes('BalloonText_view'));if(items.length!==1)return {ready:false,count:items.length};const e=items[0],r=e.getBoundingClientRect();return {ready:true,count:1,point:{x:Math.round(screenX+r.left+r.width/2),y:Math.round(screenY+(outerHeight-innerHeight)+r.top+r.height/2)}}})()"""
        target = self._probe(window, view_expression)
        if not target.get("ready"):
            raise QianniuUiaError(
                f"千牛发货物流信息“查看”控件未唯一识别：{target.get('count', 0)}"
            )
        self.prompt.mouse.move(coords=self._screen_point(target, "point"))
        time.sleep(0.65)
        # Real-page verification shows the balloon is anchored immediately
        # above “查看”. Reading DOM before moving into it shifts focus to the
        # address bar and destroys the hover, so enter the balloon directly.
        view_point = self._screen_point(target, "point")
        self.prompt.mouse.move(coords=(view_point[0], max(20, view_point[1] - 140)))
        time.sleep(0.35)
        list_expression = """(()=>{const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'},lists=[...document.querySelectorAll('ol.next-step.next-step-dot.next-step-vertical')].filter(visible);if(lists.length!==1)return {ready:false,count:lists.length};const e=lists[0],items=[...e.querySelectorAll(':scope > li')],text=x=>(x.innerText||'').trim();return {ready:items.length>0,count:lists.length,trajectory_count:items.length,first_text:text(items[0]).slice(0,260),last_text:text(items[items.length-1]).slice(0,260)}})()"""
        stable = self._probe(window, list_expression)
        after_count = int(stable.get("trajectory_count") or 0)
        if not stable.get("ready") or after_count <= 0:
            raise QianniuUiaError("鼠标移入物流浮层后未读取到唯一稳定的走件列表")
        return {
            "ok": True,
            "logistics_trajectory_count": after_count,
            "first_logistics_trajectory": str(stable.get("first_text") or ""),
            "last_logistics_trajectory": str(stable.get("last_text") or ""),
            "logistics_trajectory_read_complete": True,
            "write_performed": False,
        }

    def _action_target(self, window: Any, text: str) -> dict[str, Any]:
        expression = f"""(()=>{{const visible=e=>{{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'}},buttons=[...document.querySelectorAll('button')].filter(e=>visible(e)&&(e.innerText||'').trim()==='{text}');if(buttons.length!==1)return {{ready:false,count:buttons.length}};const e=buttons[0],r=e.getBoundingClientRect();return {{ready:true,count:1,inViewport:r.top>=0&&r.bottom<=innerHeight-20,point:{{x:Math.round(screenX+r.left+r.width/2),y:Math.round(screenY+(outerHeight-innerHeight)+r.top+r.height/2)}}}}}})()"""
        target: dict[str, Any] = {}
        for _ in range(8):
            target = self._probe(window, expression)
            if not target.get("ready"):
                if int(target.get("count") or 0) == 0:
                    raise QianniuActionUnavailable(
                        f"千牛详情操作“{text}”已不存在"
                    )
                raise QianniuUiaError(f"千牛详情操作“{text}”未唯一识别")
            if target.get("inViewport"):
                return target
            self.prompt.mouse.scroll(coords=(1200, 760), wheel_dist=3)
            time.sleep(0.25)
        raise QianniuUiaError(f"千牛详情操作“{text}”未进入可点击区域")

    def _confirm_dialog_if_present(
        self,
        window: Any,
        *,
        wait_seconds: float = 3.0,
        action_text: str = "",
    ) -> dict[str, Any]:
        action = str(action_text or "").replace("\\", "\\\\").replace("'", "\\'")
        expression = f"""(()=>{{const visible=e=>{{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'}},all=[...document.querySelectorAll('[role=dialog],.next-dialog')].filter(visible),leaf=all.filter(d=>!all.some(x=>x!==d&&d.contains(x))),scoped=leaf.filter(d=>!'{action}'||(d.innerText||'').includes('{action}')),dialogs=scoped.length?scoped:leaf,buttons=dialogs.flatMap(d=>[...d.querySelectorAll('button,[role=button]')].filter(e=>visible(e)&&!e.disabled)),accepted=buttons.filter(e=>{{const t=(e.innerText||'').replace(/\\s+/g,'').trim();return !/取消|关闭|返回/.test(t)&&(/确认|确定|提交/.test(t)||t==='{action}')}}),unique=[...new Set(accepted)],texts=[...new Set(buttons.map(e=>(e.innerText||'').replace(/\\s+/g,'').trim()).filter(Boolean))].slice(0,8),dialog_texts=dialogs.map(d=>(d.innerText||'').replace(/\\s+/g,' ').trim().slice(0,160)).slice(0,3);if(all.length===0)return {{present:false,dialogs:0,confirms:0}};if(unique.length!==1)return {{present:true,dialogs:dialogs.length,confirms:unique.length,button_texts:texts,dialog_texts}};const e=unique[0],r=e.getBoundingClientRect();return {{present:true,dialogs:dialogs.length,confirms:1,text:(e.innerText||'').trim(),button_texts:texts,point:{{x:Math.round(screenX+r.left+r.width/2),y:Math.round(screenY+(outerHeight-innerHeight)+r.top+r.height/2)}}}}}})()"""
        deadline = time.monotonic() + max(0.1, float(wait_seconds))
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self._probe(window, expression)
            if last.get("present"):
                if int(last.get("confirms") or 0) != 1:
                    dialog_texts = [
                        str(item or "").replace(" ", "").strip()
                        for item in (last.get("dialog_texts") or [])
                    ]
                    success_feedback = INTERCEPT_SUCCESS_FEEDBACK.replace(" ", "")
                    if (
                        action_text == INTERCEPT_EXPRESS
                        and any(success_feedback in item for item in dialog_texts)
                    ):
                        return {
                            "present": True,
                            "clicked": False,
                            "button_text": "",
                            "terminal_verified": True,
                            "terminal_feedback": INTERCEPT_SUCCESS_FEEDBACK,
                        }
                    raise QianniuUiaError(
                        "千牛确认弹窗存在，但确认按钮未唯一识别："
                        f"dialogs={last.get('dialogs', 0)}, "
                        f"buttons={last.get('button_texts') or []}, "
                        f"text={last.get('dialog_texts') or []}"
                    )
                # A coordinate click can be swallowed while the probe's own
                # native prompt is still closing.  Re-read the live dialog
                # position and click again until it is observed to disappear;
                # never report success on an unverified click.
                for attempt in range(1, 4):
                    self.prompt.mouse.click(
                        coords=self._screen_point(last, "point")
                    )
                    time.sleep(0.8)
                    after = self._probe(window, expression)
                    if not after.get("present"):
                        return {
                            "present": True,
                            "clicked": True,
                            "button_text": str(last.get("text") or ""),
                            "click_attempts": attempt,
                        }
                    if int(after.get("confirms") or 0) != 1:
                        raise QianniuUiaError(
                            "千牛确认弹窗点击后状态异常："
                            f"dialogs={after.get('dialogs', 0)}, "
                            f"buttons={after.get('button_texts') or []}"
                        )
                    last = after
                raise QianniuUiaError(
                    "千牛确认弹窗点击 3 次后仍未关闭，未标记完成"
                )
            time.sleep(0.15)
        return {"present": False, "clicked": False, "button_text": ""}

    def execute_intercept(
        self, *, order_no: str, aftersale_no: str
    ) -> dict[str, Any]:
        """Submit Tmall express interception and stop; refund remains manual.

        The caller owns the external prerequisites (group message and DingTalk
        registration).  This method deliberately performs only the final
        Qianniu click and its optional confirmation.
        """

        window = self._ensure_detail_window(aftersale_no)
        self.dismiss_obstructions(window)
        state = self.read_current_state(order_no=order_no, aftersale_no=aftersale_no)
        if not state.get("processable"):
            return {
                "ok": True,
                "externally_handled": True,
                "reason": "平台状态已变化或拦截快递已由人工处理",
                "state": state,
                "business_write_performed": False,
            }
        try:
            target = self._action_target(window, INTERCEPT_EXPRESS)
        except QianniuActionUnavailable:
            return {
                "ok": True,
                "externally_handled": True,
                "reason": "拦截快递按钮已由人工处理后消失",
                "state": state,
                "business_write_performed": False,
            }
        self.prompt.mouse.click(coords=self._screen_point(target, "point"))
        confirmation = self._confirm_dialog_if_present(
            window, action_text=INTERCEPT_EXPRESS
        )
        return {
            "ok": True,
            "action": INTERCEPT_EXPRESS,
            "order_no": str(order_no),
            "aftersale_no": str(aftersale_no),
            "detail_url": state["detail_url"],
            "confirmation": confirmation,
            "terminal_feedback": str(
                confirmation.get("terminal_feedback") or ""
            ),
            "terminal": "EXPRESS_INTERCEPTION_SUBMITTED",
            "manual_refund_followup": True,
            "business_write_performed": True,
        }

    def _verify_refund_completed(self, window: Any) -> dict[str, Any]:
        """Refuse false success: the page must show the completed refund state.

        2026-08-29 live incident: a swallowed confirm click still reported
        success and the task was marked COMPLETED while the refund had not
        happened.  Require the 同意退款 button to vanish AND the completion
        text to appear before the caller may report business success.
        """

        expression = """(()=>{const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'},agree=[...document.querySelectorAll('button')].filter(e=>visible(e)&&(e.innerText||'').trim()==='同意退款'),t=document.body.innerText||'';return {agree_remaining:agree.length,refund_success:t.includes('退款成功')||t.includes('退款完毕')}})()"""
        deadline = time.monotonic() + 10.0
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self._probe(window, expression)
            if not int(last.get("agree_remaining") or 0) and last.get(
                "refund_success"
            ):
                return {"verified": True, **last}
            time.sleep(0.5)
        raise QianniuUiaError(
            "千牛同意退款后页面未出现退款成功状态，未标记完成："
            f"agree_remaining={last.get('agree_remaining')}, "
            f"refund_success={last.get('refund_success')}"
        )

    def execute_agree_refund(
        self, *, order_no: str, aftersale_no: str
    ) -> dict[str, Any]:
        """Agree the narrowly permitted pre-shipment refund branch."""

        window = self._ensure_detail_window(aftersale_no)
        self.dismiss_obstructions(window)
        state = self.read_current_state(order_no=order_no, aftersale_no=aftersale_no)
        if not state.get("processable"):
            return {
                "ok": True,
                "externally_handled": True,
                "reason": "平台状态已变化或退款操作已由人工处理",
                "state": state,
                "business_write_performed": False,
            }
        try:
            target = self._action_target(window, AGREE_REFUND)
        except QianniuActionUnavailable:
            return {
                "ok": True,
                "externally_handled": True,
                "reason": "同意退款按钮已由人工处理后消失",
                "state": state,
                "business_write_performed": False,
            }
        self.prompt.mouse.click(coords=self._screen_point(target, "point"))
        confirmation = self._confirm_dialog_if_present(
            window, wait_seconds=5.0, action_text=AGREE_REFUND
        )
        if not confirmation.get("clicked"):
            raise QianniuUiaError("千牛同意退款后未出现可确认弹窗，未标记完成")
        refund_outcome = self._verify_refund_completed(window)
        return {
            "ok": True,
            "action": AGREE_REFUND,
            "order_no": str(order_no),
            "aftersale_no": str(aftersale_no),
            "detail_url": state["detail_url"],
            "confirmation": confirmation,
            "refund_outcome": refund_outcome,
            "terminal": "REFUND_CONFIRMED",
            "business_write_performed": True,
        }

    def close_detail(self, *, order_no: str, aftersale_no: str) -> dict[str, Any]:
        """Close every active tab for this order and leave the shop list open."""
        detail_url = QIANNIU_REFUND_DETAIL_URL.format(aftersale_no=str(aftersale_no))
        try:
            cleanup = self.browser.close_or_restore_list(detail_url)
        except Exception as exc:
            raise QianniuUiaError(f"关闭千牛详情后恢复退款列表失败：{exc}") from exc
        # 列表页导航刚完成时，UIA 地址栏与页面文本的更新存在滞后；立即做
        # 店铺校验会把刚恢复的列表窗口误判为 0 个。轮询至多 10 秒。
        window = None
        last_error: Exception | None = None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                window = self.window(allowed_kinds={"refund_list"})
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)
        if window is None:
            raise QianniuUiaError(
                f"关闭千牛详情后恢复退款列表失败：{last_error}"
            )
        return {
            "ok": True,
            "closed_tabs": int(cleanup.get("closed_tabs") or 0),
            "url": self._current_url(window),
            "list_restored": True,
            "business_write_performed": False,
        }
