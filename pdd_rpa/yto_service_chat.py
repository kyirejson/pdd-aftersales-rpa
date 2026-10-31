"""Playwright driver for the dedicated YTO customer-service Edge profile."""

from __future__ import annotations

import subprocess
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .yto_credentials import (
    DEFAULT_YTO_CREDENTIAL_FILE,
    YtoCredentialError,
    YtoCredentials,
    load_yto_credentials_preferred,
)


class YtoServiceChatError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class YtoServiceChatConfig:
    enabled: bool
    url: str
    login_url: str
    edge_profile: str
    playwright_user_data_dir: str
    source_edge_user_data_dir: str
    source_profile_directory: str
    cdp_endpoint: str
    expected_group_name: str
    connect_timeout_seconds: float
    action_timeout_seconds: float
    credential_file: str

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> "YtoServiceChatConfig":
        data = dict(raw or {})
        return cls(
            enabled=bool(data.get("enabled", True)),
            url=str(data.get("url") or "https://kh.yto.net.cn/new/home").strip(),
            login_url=str(data.get("login_url") or "https://kh.yto.net.cn/new/login").strip(),
            edge_profile=str(data.get("edge_profile") or "YTO Playwright").strip(),
            playwright_user_data_dir=str(
                data.get("playwright_user_data_dir")
                or "runtime/edge_profiles/yto_playwright"
            ).strip(),
            source_edge_user_data_dir=str(
                data.get("source_edge_user_data_dir")
                or Path.home() / "AppData/Local/Microsoft/Edge/User Data"
            ).strip(),
            source_profile_directory=str(
                data.get("source_profile_directory") or "Profile 20"
            ).strip(),
            cdp_endpoint=str(
                data.get("cdp_endpoint") or "http://127.0.0.1:9223"
            ).rstrip("/"),
            expected_group_name=str(
                data.get("expected_group_name") or "示例客户服务群"
            ).strip(),
            connect_timeout_seconds=max(
                3.0, float(data.get("connect_timeout_seconds", 15))
            ),
            action_timeout_seconds=max(
                2.0, float(data.get("action_timeout_seconds", 12))
            ),
            credential_file=str(
                data.get("credential_file") or DEFAULT_YTO_CREDENTIAL_FILE
            ).strip(),
        )

    def validate(self) -> list[str]:
        reasons: list[str] = []
        for label, value in (("主页", self.url), ("登录页", self.login_url)):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith("yto.net.cn"):
                reasons.append(f"圆通{label}必须属于 https://*.yto.net.cn")
        if not self.playwright_user_data_dir:
            reasons.append("圆通 Playwright 用户目录不能为空")
        if not self.source_edge_user_data_dir:
            reasons.append("圆通登录态来源目录不能为空")
        if not self.source_profile_directory:
            reasons.append("圆通登录态来源 Profile 不能为空")
        if not self.expected_group_name:
            reasons.append("圆通专属客服群名不能为空")
        if not self.credential_file:
            reasons.append("圆通自动登录凭据文件不能为空")
        port = urlsplit(self.cdp_endpoint).port
        if not port:
            reasons.append("圆通 CDP 地址必须包含独立端口")
        return reasons


class YtoServiceChatWriter:
    """Open the YTO service panel and optionally send one interception message."""

    def __init__(self, config: YtoServiceChatConfig, evidence_dir: Path) -> None:
        self.config = config
        self.evidence_dir = Path(evidence_dir) / "yto_service_chat"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def _load_credentials(self) -> YtoCredentials:
        try:
            credentials, _source = load_yto_credentials_preferred(
                self.config.credential_file
            )
            return credentials
        except YtoCredentialError as exc:
            raise YtoServiceChatError(str(exc)) from exc

    def credential_readiness(self) -> dict[str, Any]:
        try:
            credentials, source = load_yto_credentials_preferred(
                self.config.credential_file
            )
            return {
                "ready": bool(credentials.username and credentials.password),
                "source": source,
                "windows_user": str(Path.home().name),
                "credential_target": "PDD_RPA/YTO_CUSTOMER_SERVICE",
            }
        except YtoCredentialError as exc:
            return {
                "ready": False,
                "source": "unavailable",
                "windows_user": str(Path.home().name),
                "credential_target": "PDD_RPA/YTO_CUSTOMER_SERVICE",
                "error": str(exc),
            }

    def open_window(self) -> dict[str, Any]:
        """Open/reuse YTO and recover its DPAPI-backed login automatically."""
        if not self.config.enabled:
            raise YtoServiceChatError("圆通客户管家未启用")
        playwright, browser = self._connect()
        try:
            existing = {
                id(page)
                for context in browser.contexts
                for page in context.pages
                if (urlsplit(str(page.url or "")).hostname or "").endswith("yto.net.cn")
                and urlsplit(str(page.url or "")).path.rstrip("/") in {"/new/home", "/new/login"}
            }
            page = self._find_page(browser)
            page.set_default_timeout(int(self.config.action_timeout_seconds * 1000))
            page.bring_to_front()
            trace = self._ensure_login(page)
            login_required = bool(self._login_required(page))
            return {
                "ok": True,
                "action": "reuse" if id(page) in existing else "open",
                "opened": id(page) not in existing,
                "verified": True,
                "profile": self.config.edge_profile,
                "url": str(page.url or self.config.url),
                "login_required": login_required,
                "login_recovered": any(
                    item.get("role") in {"dpapi_credentials_filled", "login_button"}
                    for item in trace
                ),
                "trace": trace,
                "write_performed": False,
            }
        finally:
            # The Edge process and its pages remain open after Playwright detaches.
            playwright.stop()

    def preflight(self, expected_group: str) -> dict[str, Any]:
        playwright, browser = self._connect()
        try:
            page = self._find_page(browser)
            page.set_default_timeout(int(self.config.action_timeout_seconds * 1000))
            page.bring_to_front()
            trace = self._ensure_service_conversation(page, expected_group)
            evidence = self._save_evidence(page, "preflight")
            return {
                "ok": True,
                "platform": "yto",
                "group_name": expected_group,
                "group_header": expected_group,
                "sent": False,
                "trace": trace,
                "evidence": str(evidence),
            }
        finally:
            playwright.stop()

    def send(self, expected_group: str, message: str) -> dict[str, Any]:
        value = str(message or "").strip()
        if not value:
            raise YtoServiceChatError("圆通拦截消息不能为空")
        playwright, browser = self._connect()
        try:
            page = self._find_page(browser)
            page.set_default_timeout(int(self.config.action_timeout_seconds * 1000))
            page.bring_to_front()
            trace = self._ensure_service_conversation(page, expected_group)
            scope, message_input = self._message_input(page)
            existing_echoes = self._visible_delivery_echo_count(
                scope, value, message_input
            )
            message_input.click()
            tag = str(message_input.evaluate("el => el.tagName.toLowerCase()") or "")
            if tag in {"textarea", "input"}:
                message_input.fill(value)
            else:
                message_input.press("Control+A")
                message_input.press_sequentially(value, delay=8)
            trace.append({"role": "message_input", "value": value})
            send = self._send_button(scope, message_input)
            send_box = send.bounding_box()
            send.click()
            trace.append({"role": "send_button", "value": True, "box": send_box})
            # Give the DOM click a short opportunity to clear the editor. If
            # it did not, Enter is safe as a one-shot fallback because the
            # original unsent text is still present. Never press Enter after
            # an already-successful click, avoiding duplicate messages.
            time.sleep(0.6)
            if self._editor_text(message_input):
                message_input.click()
                message_input.press("Enter")
                trace.append({"role": "send_enter_fallback", "value": True})
            deadline = time.monotonic() + self.config.action_timeout_seconds
            sent_echo = False
            editor_cleared = False
            while time.monotonic() < deadline:
                editor_cleared = not self._editor_text(message_input)
                if (
                    editor_cleared
                    and self._visible_delivery_echo_count(scope, value, message_input)
                    > existing_echoes
                ):
                    sent_echo = True
                    break
                time.sleep(0.25)
            if not sent_echo:
                state = "已清空" if editor_cleared else "仍有文本"
                raise YtoServiceChatError(
                    f"圆通发送按钮已点击，但输入框{state}且聊天历史未新增本次拦截消息"
                )
            evidence = self._save_evidence(page, "intercept")
            return {
                "ok": True,
                "platform": "yto",
                "group_name": expected_group,
                "message": value,
                "sent": True,
                "target_verified": True,
                "read_back": True,
                "sent_echo": value,
                "trace": trace,
                "evidence": str(evidence),
            }
        finally:
            playwright.stop()

    def _connect(self) -> tuple[Any, Any]:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise YtoServiceChatError("缺少 Playwright，无法控制圆通专用浏览器") from exc
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.connect_over_cdp(
                self.config.cdp_endpoint,
                timeout=int(self.config.connect_timeout_seconds * 1000),
            )
        except PlaywrightError:
            self._launch_edge()
            deadline = time.monotonic() + self.config.connect_timeout_seconds
            while True:
                try:
                    browser = playwright.chromium.connect_over_cdp(
                        self.config.cdp_endpoint, timeout=1_500
                    )
                    break
                except PlaywrightError as exc:
                    if time.monotonic() >= deadline:
                        playwright.stop()
                        raise YtoServiceChatError(
                            f"无法启动或连接圆通独立配置“{self.config.edge_profile}”"
                        ) from exc
                    time.sleep(0.35)
        return playwright, browser

    def _launch_edge(self) -> None:
        root = Path(self.config.playwright_user_data_dir).expanduser()
        if not root.is_absolute():
            root = (Path.cwd() / root).resolve()
        self._bootstrap_profile(root)
        edge_candidates = (
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        )
        edge = next((item for item in edge_candidates if item.is_file()), None)
        if not edge:
            raise YtoServiceChatError("未找到 Microsoft Edge")
        port = urlsplit(self.config.cdp_endpoint).port or 9223
        subprocess.Popen(
            [
                str(edge),
                f"--user-data-dir={root}",
                f"--profile-directory={self.config.source_profile_directory}",
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                "--remote-allow-origins=*",
                "--no-first-run",
                "--new-window",
                self.config.url,
            ],
            cwd=str(edge.parent),
        )

    def _bootstrap_profile(self, root: Path) -> None:
        """Prepare one isolated persistent YTO session.

        A migration can legitimately arrive without the source Edge profile.
        In that case create a clean isolated profile and let the operator log
        in once in the dedicated window.  Subsequent runs reuse that session;
        no browser credentials or cookies are read by the migration helper.
        """

        source_root = Path(self.config.source_edge_user_data_dir).expanduser()
        source_profile = source_root / self.config.source_profile_directory
        target_profile = root / self.config.source_profile_directory
        marker = root / ".yto_profile_ready"
        if marker.is_file() and target_profile.is_dir():
            return
        root.mkdir(parents=True, exist_ok=True)
        if not source_profile.is_dir():
            target_profile.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                "source=clean-profile\n"
                f"created={datetime.now().isoformat()}\n",
                encoding="utf-8",
            )
            return
        try:
            local_state = source_root / "Local State"
            if local_state.is_file():
                shutil.copy2(local_state, root / "Local State")
            shutil.copytree(
                source_profile,
                target_profile,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    # These live files are locked while the operator's normal
                    # Edge is open.  They are not required because Profile 20
                    # already contains the saved username/password; the
                    # isolated browser can click Login once and create its own
                    # cookies and session files.
                    "Network",
                    "Sessions",
                    "Cache",
                    "Code Cache",
                    "GPUCache",
                    "DawnCache",
                    "GrShaderCache",
                    "ShaderCache",
                    "Crashpad",
                    "*.tmp",
                ),
            )
            marker.write_text(
                f"source={source_profile}\ncreated={datetime.now().isoformat()}\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise YtoServiceChatError(
                "复制 Profile 20 登录态失败；请关闭该配置窗口后重试"
            ) from exc

    def _find_page(self, browser: Any) -> Any:
        for context in browser.contexts:
            for page in context.pages:
                parsed = urlsplit(str(page.url or ""))
                host = parsed.hostname or ""
                if host.endswith("yto.net.cn") and parsed.path.rstrip("/") in {
                    "/new/home", "/new/login"
                }:
                    return page
        contexts = list(browser.contexts)
        if not contexts:
            raise YtoServiceChatError("圆通独立浏览器没有可用上下文")
        page = contexts[0].new_page()
        page.goto(self.config.url, wait_until="domcontentloaded")
        return page

    def _ensure_login(self, page: Any) -> list[dict[str, Any]]:
        """Recover the dedicated YTO session without relying on Edge autofill."""

        trace: list[dict[str, Any]] = []
        parsed = urlsplit(str(page.url or ""))
        if not parsed.hostname or not parsed.hostname.endswith("yto.net.cn"):
            page.goto(self.config.url, wait_until="domcontentloaded")
        # The home route may redirect to login after its session guard runs.
        page.wait_for_timeout(900)
        if self._login_required(page):
            self._refresh_login_page(page)
            trace.append({"role": "login_page_refreshed", "value": True})
            if self._login_required(page):
                inputs = page.locator(
                    'input[type="text"][placeholder="帐号"], '
                    'input[type="text"][placeholder="账号"], '
                    'input[type="password"]'
                )
                if inputs.count() != 2:
                    raise YtoServiceChatError(
                        f"圆通登录账号/密码控件不唯一：{inputs.count()}"
                    )
                trace.extend(
                    self._submit_login(page, inputs, self._load_credentials())
                )
        if "new/home" not in str(page.url or ""):
            page.goto(self.config.url, wait_until="domcontentloaded")
            page.wait_for_timeout(700)
            if self._login_required(page):
                raise YtoServiceChatError(
                    "圆通登录态未生效；请在独立配置窗口完成本次验证码后重试"
                )
        return trace

    def _ensure_service_conversation(self, page: Any, expected_group: str) -> list[dict[str, Any]]:
        expected = str(expected_group or self.config.expected_group_name).strip()
        if expected != self.config.expected_group_name:
            raise YtoServiceChatError(
                f"圆通目标会话不在专用配置白名单：{expected}"
            )
        trace = self._ensure_login(page)
        scope = self._scope_containing_text(page, expected)
        if scope is None:
            service_scope, service = self._find_service_entry(page)
            service.click()
            trace.append({"role": "service_entry", "value": True})
            deadline = time.monotonic() + self.config.action_timeout_seconds
            while time.monotonic() < deadline:
                scope = self._scope_containing_text(page, expected)
                if scope is not None:
                    break
                time.sleep(0.25)
        if scope is None:
            raise YtoServiceChatError(f"圆通专属客服群头校验失败：{expected}")
        trace.append({"role": "group_header", "value": expected})
        return trace

    @staticmethod
    def _refresh_login_page(page: Any) -> None:
        """Refresh the login form once to clear stale empty-field errors."""

        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(700)

    def _submit_login(
        self,
        page: Any,
        inputs: Any,
        credentials: YtoCredentials | None = None,
    ) -> list[dict[str, Any]]:
        """Submit the populated YTO form through the actual clickable control."""

        # Replay credentials as real sequential keystrokes and blur each field
        # so the Vue form observes the same event sequence as manual typing.
        # Values remain in memory only and are never added to traces.
        values = (
            [credentials.username, credentials.password]
            if credentials is not None
            else [str(inputs.nth(index).input_value() or "") for index in range(2)]
        )
        for index in range(2):
            field = inputs.nth(index)
            value = values[index]
            if not value:
                raise YtoServiceChatError("圆通自动登录凭据不完整")
            field.click()
            field.press("Control+A")
            field.press("Backspace")
            field.press_sequentially(value, delay=35)
            field.press("Tab")
            page.wait_for_timeout(100)

        trace: list[dict[str, Any]] = [
            {
                "role": (
                    "dpapi_credentials_filled"
                    if credentials is not None
                    else "autofill_replayed"
                ),
                "value": True,
            }
        ]
        checkboxes: list[Any] = []
        for scope in self._scopes(page):
            locator = scope.locator('input[type="checkbox"]')
            for index in range(locator.count()):
                item = locator.nth(index)
                try:
                    if item.is_visible():
                        checkboxes.append(item)
                except Exception:
                    continue
        if len(checkboxes) == 1 and not checkboxes[0].is_checked():
            checkboxes[0].check()
            trace.append({"role": "privacy_checkbox", "value": True})

        # Some builds keep the login button disabled until the privacy model
        # has observed the checkbox event. Locate the enabled button only after
        # that state change, never before it.
        matches: list[Any] = []
        selector = (
            'button:has-text("登录"), '
            'input[type="submit"][value*="登录"], '
            '[role="button"]:has-text("登录")'
        )
        for scope in self._scopes(page):
            locator = scope.locator(selector)
            for index in range(locator.count()):
                item = locator.nth(index)
                try:
                    if item.is_visible() and item.is_enabled():
                        matches.append(item)
                except Exception:
                    continue
        if len(matches) != 1:
            raise YtoServiceChatError(f"圆通登录按钮不唯一或不可用：{len(matches)}")

        before_url = str(page.url or "")
        matches[0].scroll_into_view_if_needed()
        matches[0].click()
        login_trace = {
            "role": "login_button",
            "value": True,
            "before_path": urlsplit(before_url).path,
        }
        trace.append(login_trace)
        if self._wait_for_login_success(page, 3.0):
            login_trace["after_path"] = urlsplit(str(page.url or "")).path
            return trace

        # Some builds attach the submit handler to the form rather than the
        # visible button.  Press Enter once in the populated password field,
        # but never repeat form submissions beyond this single fallback.
        password = inputs.nth(max(0, min(inputs.count(), 2) - 1))
        password.press("Enter")
        trace.append({"role": "login_enter_fallback", "value": True})
        if not self._wait_for_login_success(page, 27.0):
            captcha = page.locator('input[type="text"][placeholder*="验证码"]')
            visible_captcha = any(
                captcha.nth(index).is_visible()
                for index in range(captcha.count())
            )
            if visible_captcha:
                raise YtoServiceChatError(
                    "圆通账号密码已自动填写，但平台要求图形验证码；"
                    "请在圆通窗口完成本次验证码，登录态随后将自动复用"
                )
            raise YtoServiceChatError(
                "圆通登录按钮已点击但登录未生效；请检查页面提示后重试"
            )
        login_trace["after_path"] = urlsplit(str(page.url or "")).path
        return trace

    def _wait_for_login_success(self, page: Any, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            if not self._login_required(page):
                return True
            for scope in self._scopes(page):
                try:
                    matches = scope.get_by_text("专属客服", exact=True)
                    for index in range(matches.count()):
                        if matches.nth(index).is_visible():
                            return True
                except Exception:
                    continue
            time.sleep(0.25)
        return False

    @staticmethod
    def _login_required(page: Any) -> bool:
        return "/login" in str(page.url or "")

    @staticmethod
    def _scopes(page: Any) -> list[Any]:
        return [
            page,
            *[frame for frame in page.frames if frame != page.main_frame],
        ]

    def _scope_containing_text(self, page: Any, text: str) -> Any | None:
        for scope in self._scopes(page):
            try:
                locator = scope.get_by_text(text, exact=True)
                if any(locator.nth(index).is_visible() for index in range(locator.count())):
                    return scope
            except Exception:
                continue
        return None

    @staticmethod
    def _visible_exact_text_count(scope: Any, text: str) -> int:
        locator = scope.get_by_text(text, exact=True)
        count = 0
        for index in range(locator.count()):
            try:
                if locator.nth(index).is_visible():
                    count += 1
            except Exception:
                continue
        return count

    @staticmethod
    def _editor_text(message_input: Any) -> str:
        """Return the live editor value for input, textarea or contenteditable."""

        tag = str(message_input.evaluate("el => el.tagName.toLowerCase()") or "")
        if tag in {"input", "textarea"}:
            return str(message_input.input_value() or "").strip()
        return str(
            message_input.evaluate("el => (el.innerText || el.textContent || '')") or ""
        ).strip()

    @classmethod
    def _visible_delivery_echo_count(
        cls,
        scope: Any,
        text: str,
        message_input: Any,
    ) -> int:
        """Count exact echoes in chat history, explicitly excluding the editor."""

        input_box = message_input.bounding_box()
        if not input_box:
            return 0
        history_bottom = float(input_box["y"]) - 2.0
        locator = scope.get_by_text(text, exact=True)
        count = 0
        for index in range(locator.count()):
            try:
                item = locator.nth(index)
                box = item.bounding_box()
                if (
                    item.is_visible()
                    and box
                    and float(box["y"]) + float(box["height"]) <= history_bottom
                ):
                    count += 1
            except Exception:
                continue
        return count

    def _find_unique_visible_text(self, page: Any, text: str) -> tuple[Any, Any]:
        matches: list[tuple[Any, Any]] = []
        for scope in self._scopes(page):
            locator = scope.get_by_text(text, exact=True)
            for index in range(locator.count()):
                item = locator.nth(index)
                if item.is_visible():
                    matches.append((scope, item))
        if len(matches) != 1:
            raise YtoServiceChatError(f"圆通页面“{text}”控件不唯一：{len(matches)}")
        return matches[0]

    def _find_service_entry(self, page: Any) -> tuple[Any, Any]:
        """Select the visible bottom-right 专属客服 entry, never a hidden copy."""
        matches: list[tuple[Any, Any, dict[str, float]]] = []
        viewport = page.viewport_size or {"width": 1920, "height": 1080}
        for scope in self._scopes(page):
            locator = scope.get_by_text("专属客服", exact=True)
            for index in range(locator.count()):
                item = locator.nth(index)
                try:
                    box = item.bounding_box()
                    if item.is_visible() and box:
                        matches.append((scope, item, box))
                except Exception:
                    continue
        bottom_right = [
            item
            for item in matches
            if item[2]["x"] >= float(viewport["width"]) * 0.55
            and item[2]["y"] >= float(viewport["height"]) * 0.45
        ]
        if len(bottom_right) != 1:
            raise YtoServiceChatError(
                f"圆通右下角专属客服入口不唯一：{len(bottom_right)}（页面可见共 {len(matches)}）"
            )
        scope, item, _box = bottom_right[0]
        return scope, item

    def _unique_visible_text(self, scope: Any, text: str) -> Any:
        locator = scope.get_by_text(text, exact=True)
        matches = [locator.nth(i) for i in range(locator.count()) if locator.nth(i).is_visible()]
        if len(matches) != 1:
            raise YtoServiceChatError(f"圆通会话“{text}”控件不唯一：{len(matches)}")
        return matches[0]

    def _send_button(self, scope: Any, message_input: Any) -> Any:
        """Bind 发送 to the enabled button nearest the active message editor."""
        candidates: list[tuple[Any, dict[str, float]]] = []
        for selector in ('button:has-text("发送")', '[role="button"]:has-text("发送")'):
            locator = scope.locator(selector)
            for index in range(locator.count()):
                item = locator.nth(index)
                try:
                    box = item.bounding_box()
                    if item.is_visible() and item.is_enabled() and box:
                        candidates.append((item, box))
                except Exception:
                    continue
        unique: dict[tuple[int, int, int, int], tuple[Any, dict[str, float]]] = {}
        for item, box in candidates:
            key = tuple(int(round(box[name])) for name in ("x", "y", "width", "height"))
            unique[key] = (item, box)
        input_box = message_input.bounding_box()
        nearby = list(unique.values())
        if input_box:
            nearby = [
                item
                for item in nearby
                if item[1]["x"] >= input_box["x"]
                and item[1]["y"] >= input_box["y"] - 80
                and item[1]["y"] <= input_box["y"] + input_box["height"] + 120
            ]
        if len(nearby) != 1:
            raise YtoServiceChatError(f"圆通当前会话发送按钮不唯一：{len(nearby)}")
        return nearby[0][0]

    def _message_input(self, page: Any) -> tuple[Any, Any]:
        selectors = ("textarea", '[contenteditable="true"]', '[role="textbox"]')
        matches: list[tuple[float, Any, Any]] = []
        for scope in self._scopes(page):
            for selector in selectors:
                locator = scope.locator(selector)
                for index in range(locator.count()):
                    item = locator.nth(index)
                    if not item.is_visible():
                        continue
                    box = item.bounding_box() or {}
                    if float(box.get("height") or 0) < 20:
                        continue
                    matches.append((float(box.get("y") or 0), scope, item))
        if not matches:
            raise YtoServiceChatError("圆通专属客服未找到消息输入框")
        _, scope, item = max(matches, key=lambda entry: entry[0])
        return scope, item

    def _save_evidence(self, page: Any, prefix: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.evidence_dir / f"{prefix}_{stamp}.png"
        page.screenshot(path=str(path), full_page=False)
        return path


__all__ = [
    "YtoServiceChatConfig",
    "YtoServiceChatError",
    "YtoServiceChatWriter",
]
