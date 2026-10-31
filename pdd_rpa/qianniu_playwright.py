"""Direct CDP bridge for the dedicated Qianniu Edge profile.

Qianniu used to execute DOM reads by typing a ``javascript:prompt(...)`` URL
into Edge's address bar.  A lost leading character turns that text into a web
search and destroys the business page.  This bridge evaluates the same
bounded expressions through Playwright/CDP, so no script text ever reaches the
address bar.
"""

from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import urlsplit

from .qianniu_edge import QianniuEdgeConfig, QianniuEdgeWindow
from .yto_credentials import load_yto_windows_credential


QIANNIU_CREDENTIAL_TARGET = "PDD_RPA/QIANNIU_SIKU_ZHENXUAN"


class QianniuPlaywrightError(RuntimeError):
    pass


class QianniuLoginRequired(QianniuPlaywrightError):
    pass


class QianniuPlaywrightBridge:
    def __init__(
        self,
        config: QianniuEdgeConfig,
        *,
        launcher_factory: Callable[..., Any] = QianniuEdgeWindow,
    ) -> None:
        self.config = config
        self.launcher_factory = launcher_factory
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._page: Any | None = None

    def _allowed(self, value: str) -> bool:
        parsed = urlsplit(str(value or ""))
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.hostname.lower() in self.config.allowed_hosts
        )

    def _connect_once(self) -> None:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(
                self.config.cdp_endpoint,
                timeout=2_000,
            )
        except Exception:
            self._playwright.stop()
            self._playwright = None
            self._browser = None
            raise

    def _connect(self) -> None:
        if self._browser is not None:
            return
        try:
            self._connect_once()
            return
        except Exception:
            # The normal launcher is the sole owner of this dedicated profile
            # and now always starts it with the configured CDP endpoint.
            self.launcher_factory(self.config, self.config.resolved_user_data_dir()).open_window()
        deadline = time.monotonic() + max(5.0, self.config.launch_verify_timeout_seconds)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._connect_once()
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.35)
        raise QianniuPlaywrightError(
            f"无法连接千牛专用 Edge CDP：{self.config.cdp_endpoint}；"
            "请关闭旧版未带 CDP 参数的千牛窗口后重试"
        ) from last_error

    def _all_pages(self) -> list[Any]:
        self._connect()
        return [
            page
            for context in self._browser.contexts
            for page in context.pages
        ]

    @staticmethod
    def _is_login_page(page: Any) -> bool:
        parsed = urlsplit(str(page.url or ""))
        return bool(
            parsed.hostname
            and (
                parsed.hostname.startswith("login")
                or "login" in parsed.path.lower()
            )
        )

    def _recover_saved_login(self, page: Any) -> dict[str, Any]:
        """Submit saved credentials without exposing their values to logs."""

        trace: list[str] = ["login_page_detected"]
        login_scope = page
        for frame in page.frames:
            try:
                if frame.locator("input[type=password]:visible").count() == 1:
                    login_scope = frame
                    trace.append("login_form_frame_selected")
                    break
            except Exception:
                continue
        for label in ("账号密码登录", "密码登录", "账号登录"):
            try:
                tab = login_scope.get_by_text(label, exact=True)
                if tab.count() == 1 and tab.is_visible():
                    tab.click()
                    trace.append("account_login_selected")
                    break
            except Exception:
                continue
        deadline = time.monotonic() + 12.0
        ready = False
        while time.monotonic() < deadline:
            try:
                ready = bool(
                    login_scope.evaluate(
                        """()=>{const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'},p=[...document.querySelectorAll('input[type=password]')].filter(visible),u=[...document.querySelectorAll('input:not([type=password]):not([type=hidden])')].filter(e=>visible(e)&&['text','tel','email',''].includes((e.type||'').toLowerCase()));return p.length===1&&u.length>=1&&Boolean(p[0].value)&&u.some(e=>Boolean(e.value))}"""
                    )
                )
            except Exception:
                ready = False
            if ready:
                break
            time.sleep(0.4)
        if not ready:
            try:
                credentials = load_yto_windows_credential(
                    target=QIANNIU_CREDENTIAL_TARGET
                )
                password = login_scope.locator("input[type=password]:visible")
                accounts = login_scope.locator(
                    "input:not([type=password]):not([type=hidden]):visible"
                )
                if password.count() != 1 or accounts.count() < 1:
                    raise QianniuLoginRequired("千牛账号或密码输入框未唯一识别")
                accounts.first.fill(credentials.username)
                password.fill(credentials.password)
                ready = True
                trace.append("windows_credentials_filled")
            except QianniuLoginRequired:
                raise
            except Exception as exc:
                raise QianniuLoginRequired(
                    "千牛登录页没有 Edge 自动填充，Windows 千牛凭据也不可用"
                ) from exc
        buttons = []
        for label in ("登录", "登 录"):
            try:
                locator = login_scope.get_by_role("button", name=label, exact=True)
                buttons.extend(
                    locator.nth(index)
                    for index in range(locator.count())
                    if locator.nth(index).is_visible()
                )
            except Exception:
                continue
        if len(buttons) != 1:
            raise QianniuLoginRequired(
                f"千牛登录按钮未唯一识别：{len(buttons)}"
            )
        buttons[0].click()
        trace.append("login_clicked")
        # Taobao can spend more than 30 seconds completing its cross-domain
        # login redirect even when the submitted credentials are accepted.
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if not self._is_login_page(page) and self._allowed(str(page.url or "")):
                trace.append("login_recovered")
                return {"status": "recovered", "trace": trace}
            time.sleep(0.5)
        raise QianniuLoginRequired(
            "千牛已提交 Edge 保存的账号密码，但仍停留在登录页；可能需要验证码、扫码或风控确认"
        )

    def ensure_page(self, target_url: str | None = None) -> Any:
        pages = self._all_pages()
        allowed = [page for page in pages if self._allowed(str(page.url or ""))]
        target = str(target_url or "").strip()
        page = None
        if target:
            exact = [page for page in allowed if str(page.url or "") == target]
            if len(exact) == 1:
                page = exact[0]
        if page is None and len(allowed) == 1:
            page = allowed[0]
        if page is None and self._page in pages:
            page = self._page
        if page is None and pages:
            # This includes the one-time legacy failure where the dedicated
            # page became a Bing search for ``avascript:...``. Reuse that page
            # and navigate it back instead of opening duplicate windows.
            page = pages[0]
        if page is None:
            contexts = self._browser.contexts
            context = contexts[0] if contexts else self._browser.new_context()
            page = context.new_page()
        if target and str(page.url or "") != target:
            page.goto(target, wait_until="domcontentloaded", timeout=30_000)
        if self._is_login_page(page):
            self._recover_saved_login(page)
            if target and str(page.url or "") != target:
                page.goto(target, wait_until="domcontentloaded", timeout=30_000)
        if not self._allowed(str(page.url or "")):
            recovery = target or self.config.url
            page.goto(recovery, wait_until="domcontentloaded", timeout=30_000)
        page.bring_to_front()
        self._page = page
        return page

    def current_url(self) -> str:
        return str(self.ensure_page().url or "")

    def goto(self, url: str) -> str:
        if not self._allowed(url):
            raise QianniuPlaywrightError("拒绝导航到千牛白名单外地址")
        page = self.ensure_page(url)
        return str(page.url or "")

    def reload(self) -> None:
        self.ensure_page().reload(wait_until="domcontentloaded", timeout=30_000)

    def evaluate(self, expression: str) -> dict[str, Any]:
        value = self.ensure_page().evaluate(expression)
        if not isinstance(value, dict):
            raise QianniuPlaywrightError("千牛页面脚本未返回对象")
        return value

    def press(self, key: str) -> None:
        self.ensure_page().keyboard.press(key)

    def close_or_restore_list(self, detail_url: str) -> dict:
        """Leave exactly one list tab; tolerate tabs that already changed.

        原实现按“当前页必须是目标详情页”做严格断言，但清理与采集是两个
        独立进程，其间页面可能已被平台自动重定向或上一任务的清理动过。
        清理的目标只是“结束时留下一个列表页”，因此按 URL 找到详情 tab
        就关它；找不到就确认/恢复列表页，不再把状态差异当失败。
        """
        pages = self._all_pages()
        if not pages:
            raise QianniuPlaywrightError("千牛浏览器已无可用的页面")
        detail_pages = [page for page in pages if str(page.url or "") == detail_url]
        if len(detail_pages) > 1:
            raise QianniuPlaywrightError("千牛详情页数量异常，拒绝自动清理")
        if detail_pages and len(pages) > 1:
            # 详情与列表并存：只关详情 tab，保留列表。
            detail_pages[0].close()
            self._page = None
            self.ensure_page(self.config.url)
            return {"closed_tabs": 1, "navigated": 0}
        page = self.ensure_page()
        if str(page.url or "") != self.config.url:
            self.goto(self.config.url)
            return {"closed_tabs": 0, "navigated": 1}
        return {"closed_tabs": 0, "navigated": 0, "already_list": True}

    def close(self) -> None:
        if self._playwright is not None:
            self._playwright.stop()
        self._playwright = None
        self._browser = None
        self._page = None


__all__ = [
    "QianniuLoginRequired",
    "QIANNIU_CREDENTIAL_TARGET",
    "QianniuPlaywrightBridge",
    "QianniuPlaywrightError",
]
