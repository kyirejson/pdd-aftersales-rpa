"""Dedicated Edge window launcher for a configured Qianniu shop.

This module intentionally remains independent from the PDD shop collection:
Qianniu windows use their own Edge profile and never enter the PDD work-order
or after-sale scanners.
"""

from __future__ import annotations

import subprocess
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .edge_launcher import EdgeShopLauncher, find_edge
from .edge_uia import PddEdgeUiaSession


class QianniuEdgeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QianniuEdgeConfig:
    enabled: bool
    shop_id: str
    shop_name: str
    edge_profile: str
    user_data_dir: str
    url: str
    login_url: str
    allowed_hosts: tuple[str, ...]
    window_title_pattern: str
    launch_verify_timeout_seconds: float
    refresh_verify_timeout_seconds: float
    cdp_endpoint: str

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> "QianniuEdgeConfig":
        data = dict(raw or {})
        hosts = data.get("allowed_hosts") or (
            "loginmyseller.taobao.com",
            "login.taobao.com",
            "myseller.taobao.com",
            "seller.taobao.com",
        )
        return cls(
            enabled=bool(data.get("enabled", True)),
            shop_id=str(data.get("id") or "qianniu_01").strip(),
            shop_name=str(data.get("shop_name") or "").strip(),
            edge_profile=str(data.get("edge_profile") or "Default").strip(),
            user_data_dir=str(
                data.get("user_data_dir")
                or "runtime/edge_profiles/qianniu_edge"
            ).strip(),
            url=str(data.get("url") or "").strip(),
            login_url=str(
                data.get("login_url") or "https://loginmyseller.taobao.com/"
            ).strip(),
            allowed_hosts=tuple(
                str(host or "").strip().lower() for host in hosts if str(host or "").strip()
            ),
            window_title_pattern=str(
                data.get("window_title_pattern") or ".*Microsoft.*Edge"
            ).strip(),
            launch_verify_timeout_seconds=max(
                0.0,
                min(float(data.get("launch_verify_timeout_seconds", 12)), 30.0),
            ),
            refresh_verify_timeout_seconds=max(
                5.0,
                min(float(data.get("refresh_verify_timeout_seconds", 30)), 60.0),
            ),
            cdp_endpoint=str(
                data.get("cdp_endpoint") or "http://127.0.0.1:9224"
            ).rstrip("/"),
        )

    def validate(self) -> list[str]:
        reasons: list[str] = []
        if not self.shop_id:
            reasons.append("千牛店铺 ID 不能为空")
        if not self.shop_name:
            reasons.append("千牛店铺名称不能为空")
        if (
            not self.edge_profile
            or any(token in self.edge_profile for token in ('"', "\r", "\n", "/", "\\"))
            or self.edge_profile in {".", ".."}
        ):
            reasons.append("千牛 Edge Profile 目录配置无效")
        if (
            not self.user_data_dir
            or any(token in self.user_data_dir for token in ('"', "\r", "\n"))
        ):
            reasons.append("千牛 Edge 独立用户目录配置无效")
        if not self.allowed_hosts:
            reasons.append("千牛允许域名不能为空")
        for host in self.allowed_hosts:
            if host != "taobao.com" and not host.endswith(".taobao.com"):
                reasons.append(f"千牛允许域名不是淘宝官方域名：{host}")
        for label, value in (("售后页", self.url), ("登录页", self.login_url)):
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.hostname.lower() not in self.allowed_hosts
            ):
                reasons.append(
                    f"千牛{label}必须是配置白名单内的 https://*.taobao.com 地址"
                )
        try:
            cdp = urlsplit(self.cdp_endpoint)
            if cdp.scheme != "http" or cdp.hostname not in {"127.0.0.1", "localhost"} or not cdp.port:
                reasons.append("千牛 CDP 地址必须是带独立端口的本机 HTTP 地址")
        except ValueError:
            reasons.append("千牛 CDP 地址格式无效")
        return reasons

    def resolved_user_data_dir(self) -> Path:
        target = Path(self.user_data_dir)
        if not target.is_absolute():
            target = Path(__file__).resolve().parent.parent / target
        return target.resolve()


def build_qianniu_launch_command(
    edge: Path, config: QianniuEdgeConfig
) -> list[str]:
    reasons = config.validate()
    if reasons:
        raise QianniuEdgeError("；".join(reasons))
    port = urlsplit(config.cdp_endpoint).port
    return [
        str(edge),
        f"--user-data-dir={config.resolved_user_data_dir()}",
        f"--profile-directory={config.edge_profile}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        "--new-window",
        "--start-maximized",
        config.url,
    ]


class QianniuEdgeWindow:
    """Open/reuse one dedicated Qianniu Edge window without business writes."""

    def __init__(
        self,
        config: QianniuEdgeConfig,
        evidence_dir: Path,
        *,
        session_factory: Callable[[dict[str, Any]], Any] = PddEdgeUiaSession,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.config = config
        self.evidence_dir = Path(evidence_dir)
        self.session_factory = session_factory
        self.popen = popen

    def _session(self) -> Any:
        return self.session_factory(
            {"window_title_pattern": self.config.window_title_pattern}
        )

    def _allowed_url(self, value: str) -> bool:
        parsed = urlsplit(str(value or ""))
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.hostname.lower() in self.config.allowed_hosts
        )

    @staticmethod
    def _window_title(window: Any) -> str:
        try:
            return str(window.window_text() or "")
        except Exception:
            return ""

    def _candidate_windows(self, session: Any) -> list[tuple[Any, str]]:
        candidates: list[tuple[Any, str]] = []
        for window in session._windows():
            try:
                current_url = str(session._current_url(window) or "")
            except Exception:
                continue
            if self._allowed_url(current_url):
                candidates.append((window, current_url))
        return candidates

    def _select_window(
        self, candidates: list[tuple[Any, str]]
    ) -> tuple[Any, str] | None:
        if not candidates:
            return None
        exact_shop = [
            item
            for item in candidates
            if self.config.shop_name in self._window_title(item[0])
        ]
        if len(exact_shop) == 1:
            return exact_shop[0]
        if len(candidates) == 1:
            return candidates[0]
        raise QianniuEdgeError(
            "检测到多个未能按店铺名唯一确认的千牛窗口，已禁止重复打开"
        )

    def _result(
        self,
        session: Any,
        window: Any,
        url: str,
        *,
        opened: bool,
    ) -> dict[str, Any]:
        title = self._window_title(window)
        page_shop_verified = False
        exact_name_reader = getattr(session, "_has_exact_name", None)
        if callable(exact_name_reader):
            try:
                page_shop_verified = bool(
                    exact_name_reader(int(window.handle), self.config.shop_name)
                )
            except Exception:
                page_shop_verified = False
        verified = self.config.shop_name in title or page_shop_verified
        maximized = EdgeShopLauncher._maximize_window(window)
        host = (urlsplit(url).hostname or "").lower()
        login_required = not verified and (
            host.startswith("login") or "login" in urlsplit(url).path.lower()
        )
        return {
            "ok": verified or login_required,
            "shop_id": self.config.shop_id,
            "shop_name": self.config.shop_name,
            "action": "open" if opened else ("reuse" if verified else "reuse_unverified"),
            "opened": opened,
            "verified": verified,
            "login_required": login_required,
            "maximized": maximized,
            "profile": self.config.edge_profile,
            "url": url,
            "window_title": title,
            "note": (
                "千牛店铺窗口已确认"
                if verified
                else "千牛独立店铺窗口已打开，请在该窗口登录示例天猫旗舰店"
            ),
            "write_performed": False,
        }

    def open_window(self) -> dict[str, Any]:
        if not self.config.enabled:
            raise QianniuEdgeError("千牛店铺窗口未启用")
        reasons = self.config.validate()
        if reasons:
            raise QianniuEdgeError("；".join(reasons))

        session = self._session()
        existing = self._select_window(self._candidate_windows(session))
        if existing:
            return self._result(session, existing[0], existing[1], opened=False)

        known_handles: set[int] = set()
        for window in session._windows():
            try:
                known_handles.add(int(window.handle))
            except Exception:
                continue

        edge = find_edge()
        self.config.resolved_user_data_dir().mkdir(parents=True, exist_ok=True)
        command = build_qianniu_launch_command(edge, self.config)
        popen_kwargs: dict[str, Any] = {"cwd": str(edge.parent)}
        if os.name == "nt":
            # A dedicated user-data-dir creates a separate Edge process rather
            # than handing the URL to one of the PDD Edge processes.  Detach
            # that GUI process from a short-lived worker/terminal so finishing
            # a launcher command cannot close the operator's Qianniu window.
            popen_kwargs.update(
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.DETACHED_PROCESS
                ),
                close_fds=True,
            )
        self.popen(command, **popen_kwargs)
        deadline = time.monotonic() + self.config.launch_verify_timeout_seconds
        last_url = ""
        while True:
            new_candidates: list[tuple[Any, str]] = []
            for window, current_url in self._candidate_windows(session):
                last_url = current_url
                try:
                    if int(window.handle) not in known_handles:
                        new_candidates.append((window, current_url))
                except Exception:
                    continue
            selected = self._select_window(new_candidates)
            if selected:
                return self._result(session, selected[0], selected[1], opened=True)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.35)
        return {
            "ok": False,
            "shop_id": self.config.shop_id,
            "shop_name": self.config.shop_name,
            "action": "open_unverified",
            "opened": True,
            "verified": False,
            "login_required": True,
            "maximized": False,
            "profile": self.config.edge_profile,
            "url": last_url or self.config.url,
            "note": "千牛窗口已按独立 Profile 启动，但暂未读取到登录页；窗口保持打开",
            "write_performed": False,
        }
