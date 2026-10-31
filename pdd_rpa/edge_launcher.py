from __future__ import annotations

import ctypes
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .connectors.pdd_edge_readonly import ReadOnlySession, WindowsEdgeReadOnlySession, select_shop_snapshot
from .edge_uia import PddEdgeUiaSession, classify_pdd_url


EDGE_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)


class EdgeLaunchError(RuntimeError):
    pass


def find_edge() -> Path:
    for path in EDGE_CANDIDATES:
        if path.exists():
            return path
    raise EdgeLaunchError("未找到Microsoft Edge可执行文件")


def build_launch_command(edge: Path, profile: str, url: str) -> list[str]:
    if not profile or any(token in profile for token in ('"', "\r", "\n")):
        raise EdgeLaunchError("Edge Profile目录配置无效")
    if not url.startswith("https://mms.pinduoduo.com/"):
        raise EdgeLaunchError("只允许打开拼多多商家后台地址")
    return [
        str(edge),
        f"--profile-directory={profile}",
        "--new-window",
        "--start-maximized",
        url,
    ]


class EdgeShopLauncher:
    def __init__(
        self,
        pdd_config: dict[str, Any],
        session_factory: Callable[[dict[str, Any]], Any] = PddEdgeUiaSession,
        popen: Callable[..., Any] = subprocess.Popen,
    ):
        self.pdd_config = pdd_config
        self.session_factory = session_factory
        self.popen = popen

    @staticmethod
    def _maximize_window(window: Any) -> bool:
        """Maximize an existing Edge top-level window without activating page controls."""
        handle = int(getattr(window, "handle", 0) or 0)
        try:
            is_maximized = getattr(window, "is_maximized", None)
            if callable(is_maximized) and bool(is_maximized()):
                return True
            is_minimized = getattr(window, "is_minimized", None)
            restore = getattr(window, "restore", None)
            if callable(is_minimized) and bool(is_minimized()) and callable(restore):
                restore()
            maximize = getattr(window, "maximize", None)
            if callable(maximize):
                maximize()
            if callable(is_maximized) and bool(is_maximized()):
                return True
            if handle:
                user32 = ctypes.windll.user32
                user32.ShowWindow(handle, 3)  # SW_MAXIMIZE
                time.sleep(0.15)
                if bool(user32.IsZoomed(handle)):
                    return True
            if callable(is_maximized):
                return bool(is_maximized())
            return bool(handle)
        except Exception:
            return False

    def open_shops(self, shops: list[dict[str, Any]]) -> list[dict[str, Any]]:
        edge = find_edge()
        url = self.pdd_config.get("list_url", "")
        session = self.session_factory(self.pdd_config)
        is_uia = all(
            hasattr(session, name)
            for name in ("_windows", "_current_url", "window_for_shop")
        )
        snapshots = [] if is_uia else session.snapshots()
        diagnostics = [] if is_uia else (session.diagnostics() if hasattr(session, "diagnostics") else [])
        results: list[dict[str, Any]] = []
        for shop in shops:
            if is_uia:
                try:
                    existing_window = session.window_for_shop(shop, "list")
                    maximized = self._maximize_window(existing_window)
                    results.append({
                        "shop_id": shop["id"], "shop_name": shop["name"],
                        "action": "reuse", "opened": False, "verified": True,
                        "maximized": maximized,
                        "engine": "edge_uia",
                    })
                    continue
                except Exception:
                    existing = next(
                        (
                            window for window in session._windows()
                            if shop["name"] in window.window_text()
                            and classify_pdd_url(session._current_url(window)) in {"list", "detail", "login"}
                        ),
                        None,
                    )
                    if existing:
                        maximized = self._maximize_window(existing)
                        existing_kind = classify_pdd_url(session._current_url(existing))
                        if existing_kind == "detail" and hasattr(
                            session, "restore_shop_list_anchor"
                        ):
                            try:
                                recovery = session.restore_shop_list_anchor(
                                    shop, preferred_window=existing
                                )
                                results.append({
                                    "shop_id": shop["id"], "shop_name": shop["name"],
                                    "action": "reuse_restored", "opened": False,
                                    "verified": bool(recovery.get("list_ready")),
                                    "maximized": maximized,
                                    "list_recovery": recovery,
                                    "engine": "edge_uia",
                                })
                                continue
                            except Exception as exc:
                                results.append({
                                    "shop_id": shop["id"], "shop_name": shop["name"],
                                    "action": "reuse_unverified", "opened": False,
                                    "verified": False,
                                    "maximized": maximized,
                                    "login_required": False,
                                    "note": f"详情窗口未关闭，导航回工单列表失败：{exc}",
                                    "engine": "edge_uia",
                                })
                                continue
                        results.append({
                            "shop_id": shop["id"], "shop_name": shop["name"],
                            "action": "reuse_unverified", "opened": False,
                            "verified": False,
                            "maximized": maximized,
                            "login_required": existing_kind == "login",
                            "note": "存在对应 UIA 窗口；同步前会按安全登录恢复步骤处理登录页",
                            "engine": "edge_uia",
                        })
                        continue
            try:
                select_shop_snapshot(snapshots, shop)
                results.append({
                    "shop_id": shop["id"],
                    "shop_name": shop["name"],
                    "action": "reuse",
                    "opened": False,
                    "verified": True,
                })
                continue
            except Exception:
                pass
            existing_unverified = next(
                (
                    item
                    for item in diagnostics
                    if shop["name"] in str(item.get("title", ""))
                    and str(item.get("url_origin_path", "")).startswith(
                        "https://mms.pinduoduo.com/"
                    )
                ),
                None,
            )
            if existing_unverified:
                results.append({
                    "shop_id": shop["id"],
                    "shop_name": shop["name"],
                    "action": "reuse_unverified",
                    "opened": False,
                    "verified": False,
                    "login_required": bool(existing_unverified.get("login_required")),
                    "note": "已存在对应窗口；登录完成后再以页面店铺名复核",
                })
                continue
            known_handles: set[int] = set()
            if is_uia:
                for window in session._windows():
                    try:
                        known_handles.add(int(window.handle))
                    except Exception:
                        continue
            command = build_launch_command(edge, shop["edge_profile"], url)
            self.popen(command, cwd=str(edge.parent))
            opened_result = {
                "shop_id": shop["id"],
                "shop_name": shop["name"],
                "action": "open",
                "opened": True,
                "verified": False,
                "note": "窗口已按Profile启动，正在等待工单列表页复核",
            }
            if is_uia:
                try:
                    timeout = max(
                        0.0,
                        min(
                            float(self.pdd_config.get("launch_verify_timeout_seconds", 12)),
                            30.0,
                        ),
                    )
                except (TypeError, ValueError):
                    timeout = 12.0
                deadline = time.monotonic() + timeout
                last_error = ""
                navigation_attempted = False
                while True:
                    try:
                        list_window = session.window_for_shop(shop, "list")
                        maximized = self._maximize_window(list_window)
                        opened_result.update(
                            verified=bool(list_window),
                            maximized=maximized,
                            note="窗口已按Profile启动并确认进入对应店铺工单列表",
                        )
                        break
                    except Exception as exc:
                        last_error = str(exc)
                    if not navigation_attempted and hasattr(session, "navigate_url"):
                        new_windows = []
                        for candidate in session._windows():
                            try:
                                if int(candidate.handle) not in known_handles:
                                    new_windows.append(candidate)
                            except Exception:
                                continue
                        if len(new_windows) == 1:
                            launched_window = new_windows[0]
                            launched_kind = classify_pdd_url(
                                session._current_url(launched_window)
                            )
                            if launched_kind == "login":
                                maximized = self._maximize_window(launched_window)
                                opened_result.update(
                                    login_required=True,
                                    maximized=maximized,
                                    note="窗口已按Profile启动，但当前需要登录；窗口保持打开",
                                )
                                break
                            if launched_kind != "list":
                                try:
                                    session.navigate_url(launched_window, url)
                                    navigation_attempted = True
                                    opened_result["navigation_attempted"] = True
                                    continue
                                except Exception as exc:
                                    last_error = str(exc)
                    if time.monotonic() >= deadline:
                        opened_result["note"] = (
                            "窗口已启动但工单列表未确认；窗口将保持打开，不执行关闭"
                        )
                        opened_result["verification_error"] = last_error
                        break
                    time.sleep(0.5)
            results.append(opened_result)
            time.sleep(0.8)
        return results
