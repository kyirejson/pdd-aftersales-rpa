import unittest
from pathlib import Path
from unittest.mock import patch

from pdd_rpa.connectors.pdd_edge_readonly import WindowSnapshot
from pdd_rpa.edge_launcher import EdgeLaunchError, EdgeShopLauncher, build_launch_command


SHOP = {"id": "shop_01", "name": "示例书店 A", "edge_profile": "Profile"}
URL = "https://mms.pinduoduo.com/aftersales/work_order/list?msfrom=mms_sidenav"


class EmptySession:
    def __init__(self, config): pass
    def snapshots(self): return []
    def diagnostics(self): return []


class ExistingSession:
    def __init__(self, config): pass
    def snapshots(self):
        return [WindowSnapshot(1, "Edge", URL, "示例书店 A\n工单管理\n暂无数据")]
    def diagnostics(self): return []


class LoginSession:
    def __init__(self, config): pass
    def snapshots(self): return []
    def diagnostics(self):
        return [{
            "title": "拼多多商家后台 - 示例书店 A - Microsoft Edge",
            "url_origin_path": "https://mms.pinduoduo.com/login/",
            "login_required": True,
        }]


class FakeUiaWindow:
    handle = 9

    def __init__(self):
        self.maximized = False

    def window_text(self):
        return f"{SHOP['name']} - Microsoft Edge"

    def is_maximized(self):
        return self.maximized

    def maximize(self):
        self.maximized = True


class DetailOnlyUiaSession:
    def __init__(self, config):
        self.window = FakeUiaWindow()
        self.restored = False

    def _windows(self):
        return [self.window]

    def _current_url(self, window):
        return LIST_URL if self.restored else "https://mms.pinduoduo.com/aftersales/work_order/tododetail?id=1"

    def window_for_shop(self, shop, kind):
        if kind == "list" and self.restored:
            return self.window
        raise RuntimeError("list missing")

    def restore_shop_list_anchor(self, shop, preferred_window=None):
        assert preferred_window is self.window
        self.restored = True
        return {
            "closed": 0,
            "navigated": 1,
            "list_ready": True,
            "handled": True,
        }


class LaunchUiaSession:
    def __init__(self, config):
        self.window = FakeUiaWindow()
        self.opened = False
        self.url = "edge://newtab/"
        self.navigation_calls = []

    def _windows(self):
        return [self.window] if self.opened else []

    def _current_url(self, window):
        return self.url

    def window_for_shop(self, shop, kind):
        if self.opened and kind == "list" and self.url == URL:
            return self.window
        raise RuntimeError("list missing")

    def navigate_url(self, window, url):
        self.navigation_calls.append(url)
        self.url = url


class EdgeLauncherTests(unittest.TestCase):
    def test_command_binds_exact_profile_and_safe_domain(self):
        command = build_launch_command(Path("msedge.exe"), "Profile 1", URL)
        self.assertIn("--profile-directory=Profile 1", command)
        self.assertIn("--start-maximized", command)
        self.assertEqual(command[-1], URL)

    def test_non_pdd_url_is_rejected(self):
        with self.assertRaises(EdgeLaunchError):
            build_launch_command(Path("msedge.exe"), "Profile", "https://example.com/")

    @patch("pdd_rpa.edge_launcher.find_edge", return_value=Path("msedge.exe"))
    @patch("pdd_rpa.edge_launcher.time.sleep", return_value=None)
    def test_missing_window_launches_one_profile(self, _sleep, _edge):
        calls = []
        launcher = EdgeShopLauncher(
            {"list_url": URL},
            session_factory=EmptySession,
            popen=lambda command, cwd: calls.append((command, cwd)),
        )
        result = launcher.open_shops([SHOP])
        self.assertEqual(len(calls), 1)
        self.assertTrue(result[0]["opened"])
        self.assertFalse(result[0]["verified"])

    @patch("pdd_rpa.edge_launcher.find_edge", return_value=Path("msedge.exe"))
    def test_verified_existing_window_is_reused(self, _edge):
        launcher = EdgeShopLauncher(
            {"list_url": URL},
            session_factory=ExistingSession,
            popen=lambda *args, **kwargs: self.fail("不应重复启动窗口"),
        )
        result = launcher.open_shops([SHOP])
        self.assertEqual(result[0]["action"], "reuse")

    @patch("pdd_rpa.edge_launcher.find_edge", return_value=Path("msedge.exe"))
    def test_existing_login_window_is_not_duplicated(self, _edge):
        launcher = EdgeShopLauncher(
            {"list_url": URL},
            session_factory=LoginSession,
            popen=lambda *args, **kwargs: self.fail("登录窗口已存在，不应重复启动"),
        )
        result = launcher.open_shops([SHOP])
        self.assertEqual(result[0]["action"], "reuse_unverified")
        self.assertTrue(result[0]["login_required"])

    @patch("pdd_rpa.edge_launcher.find_edge", return_value=Path("msedge.exe"))
    def test_existing_detail_is_navigated_in_place_and_never_closed(self, _edge):
        session = DetailOnlyUiaSession({})
        launcher = EdgeShopLauncher(
            {"list_url": URL},
            session_factory=lambda config: session,
            popen=lambda *args, **kwargs: self.fail("详情窗口应原地恢复，不应重复启动"),
        )

        result = launcher.open_shops([SHOP])

        self.assertEqual(result[0]["action"], "reuse_restored")
        self.assertTrue(result[0]["verified"])
        self.assertTrue(result[0]["maximized"])
        self.assertTrue(session.window.maximized)
        self.assertTrue(session.restored)

    @patch("pdd_rpa.edge_launcher.find_edge", return_value=Path("msedge.exe"))
    @patch("pdd_rpa.edge_launcher.time.sleep", return_value=None)
    def test_new_blank_profile_window_is_navigated_and_verified(self, _sleep, _edge):
        session = LaunchUiaSession({})

        def launch(command, cwd):
            session.opened = True

        launcher = EdgeShopLauncher(
            {"list_url": URL, "launch_verify_timeout_seconds": 1},
            session_factory=lambda config: session,
            popen=launch,
        )

        result = launcher.open_shops([SHOP])

        self.assertEqual(session.navigation_calls, [URL])
        self.assertTrue(result[0]["verified"])
        self.assertTrue(result[0]["maximized"])
        self.assertTrue(session.window.maximized)
        self.assertTrue(result[0]["navigation_attempted"])


if __name__ == "__main__":
    unittest.main()
