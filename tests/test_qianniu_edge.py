from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pdd_rpa.qianniu_edge import (
    QianniuEdgeConfig,
    QianniuEdgeError,
    QianniuEdgeWindow,
    build_qianniu_launch_command,
)


class FakeWindow:
    def __init__(self, handle: int, title: str, url: str):
        self.handle = handle
        self._title = title
        self.url = url
        self.maximized = False

    def window_text(self):
        return self._title

    def is_maximized(self):
        return self.maximized

    def is_minimized(self):
        return False

    def maximize(self):
        self.maximized = True


class FakeSession:
    def __init__(self, windows):
        self.windows = windows

    def _windows(self):
        return list(self.windows)

    @staticmethod
    def _current_url(window):
        return window.url


def configured(**overrides):
    raw = {
        "enabled": True,
        "id": "qianniu_01",
        "shop_name": "示例天猫旗舰店",
        "edge_profile": "Default",
        "user_data_dir": "runtime/edge_profiles/qianniu_edge_test",
        "url": "https://myseller.taobao.com/home.htm/trade-platform/refund-list",
        "login_url": "https://loginmyseller.taobao.com/",
        "allowed_hosts": [
            "loginmyseller.taobao.com",
            "login.taobao.com",
            "myseller.taobao.com",
        ],
        "launch_verify_timeout_seconds": 0,
    }
    raw.update(overrides)
    return QianniuEdgeConfig.from_raw(raw)


class QianniuEdgeTests(unittest.TestCase):
    def test_exact_shop_configuration_builds_isolated_maximized_command(self):
        config = configured()
        command = build_qianniu_launch_command(Path("msedge.exe"), config)

        self.assertEqual(config.validate(), [])
        self.assertIn("--profile-directory=Default", command)
        self.assertTrue(
            any(item.startswith("--user-data-dir=") for item in command)
        )
        self.assertIn("--new-window", command)
        self.assertIn("--start-maximized", command)
        self.assertEqual(
            command[-1],
            "https://myseller.taobao.com/home.htm/trade-platform/refund-list",
        )

    def test_rejects_non_taobao_target(self):
        config = configured(url="https://example.com/", allowed_hosts=["example.com"])
        with self.assertRaises(QianniuEdgeError):
            build_qianniu_launch_command(Path("msedge.exe"), config)

    def test_reuses_login_window_without_opening_duplicate(self):
        window = FakeWindow(
            15,
            "千牛卖家登录 - Microsoft Edge",
            "https://loginmyseller.taobao.com/",
        )
        popen_calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            launcher = QianniuEdgeWindow(
                configured(),
                Path(temp_dir),
                session_factory=lambda _raw: FakeSession([window]),
                popen=lambda *args, **kwargs: popen_calls.append((args, kwargs)),
            )
            result = launcher.open_window()

        self.assertTrue(result["ok"])
        self.assertFalse(result["opened"])
        self.assertFalse(result["verified"])
        self.assertTrue(result["login_required"])
        self.assertTrue(result["maximized"])
        self.assertEqual(popen_calls, [])

    def test_reuses_verified_shop_window(self):
        window = FakeWindow(
            16,
            "示例天猫旗舰店 - 千牛工作台 - Microsoft Edge",
            "https://myseller.taobao.com/home.htm",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = QianniuEdgeWindow(
                configured(),
                Path(temp_dir),
                session_factory=lambda _raw: FakeSession([window]),
            ).open_window()

        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["action"], "reuse")
        self.assertTrue(result["maximized"])

    def test_opens_new_profile_when_no_qianniu_window_exists(self):
        windows = []

        def popen(command, **_kwargs):
            self.assertIn("--profile-directory=Default", command)
            self.assertTrue(
                any(item.startswith("--user-data-dir=") for item in command)
            )
            windows.append(
                FakeWindow(
                    17,
                    "千牛卖家登录 - Microsoft Edge",
                    "https://loginmyseller.taobao.com/",
                )
            )

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "pdd_rpa.qianniu_edge.find_edge",
            return_value=Path("C:/Edge/msedge.exe"),
        ):
            result = QianniuEdgeWindow(
                configured(),
                Path(temp_dir),
                session_factory=lambda _raw: FakeSession(windows),
                popen=popen,
            ).open_window()

        self.assertTrue(result["ok"])
        self.assertTrue(result["opened"])
        self.assertTrue(result["login_required"])
        self.assertTrue(result["maximized"])


if __name__ == "__main__":
    unittest.main()
