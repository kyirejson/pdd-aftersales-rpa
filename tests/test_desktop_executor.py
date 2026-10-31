from contextlib import nullcontext

import pytest

from pdd_rpa.desktop_executor import DesktopComponentExecutor, DesktopExecutionError


def valid_context(status="已取消"):
    return {
        "tracking_no": "YT4876561331824",
        "order_no": "260000-157704829460496",
        "wdt_order_id": "JY123",
        "wdt_result_count": 1,
        "wdt_platform": "拼多多",
        "wdt_original_order": "260000-215444076475629,260000-157704829460496",
        "wdt_order_status": status,
        "wdt_waybill": "YT4876561331824",
    }


@pytest.mark.parametrize("status", ["未审核", "已取消", "已审核", "待客审"])
def test_wdt_four_status_allowlist(status):
    context = valid_context(status)
    result = DesktopComponentExecutor._assert_query(
        context, {"expected_platform": "拼多多", "allowed_statuses": ["未审核", "已取消", "已审核", "待客审"]}
    )
    assert result["eligible"] is True
    assert context["wdt_eligible"] is True
    assert result["unique_result_verified_by"] == "visual_result_count_tracking_input_platform"


def test_shipped_no_trace_assertion_does_not_require_platform_when_platform_is_not_read():
    context = valid_context("待客审")
    context.pop("wdt_platform")

    result = DesktopComponentExecutor._assert_query(
        context,
        {"allowed_statuses": ["未审核", "已取消", "已审核", "待客审"]},
    )

    assert result["eligible"] is True
    assert context["wdt_eligible"] is True
    assert result["unique_result_verified_by"] == "visual_result_count_tracking_input"


def test_shipped_status_enters_verified_interception_branch_instead_of_failing():
    context = valid_context("已发货")
    context.pop("wdt_platform")

    result = DesktopComponentExecutor._assert_query(
        context,
        {
            "allowed_statuses": ["未审核", "已取消", "已审核", "待客审"],
            "interception_statuses": ["已发货"],
        },
    )

    assert result["eligible"] is False
    assert result["requires_interception"] is True
    assert context["wdt_eligible"] is False
    assert context["wdt_requires_interception"] is True


def test_multiple_wdt_rows_fail_closed():
    context = valid_context()
    context["wdt_result_count"] = 2
    with pytest.raises(DesktopExecutionError):
        DesktopComponentExecutor._assert_query(context, {"allowed_statuses": ["已取消"]})


def test_visual_zero_fails_closed():
    context = valid_context()
    context["wdt_result_count"] = 0
    with pytest.raises(DesktopExecutionError, match="结果数量不是1"):
        DesktopComponentExecutor._assert_query(context, {"allowed_statuses": ["已取消"]})


def test_in_transit_exact_tracking_platform_fallback_allows_visual_zero():
    context = valid_context()
    context["wdt_result_count"] = 0
    context["wdt_logistics_company"] = "韵达一拼多多2"
    result = DesktopComponentExecutor._assert_query(
        context,
        {
            "expected_platform": "拼多多",
            "skip_status_check": True,
            "allow_exact_tracking_fallback": True,
            "require_original_order_match": True,
        },
    )
    assert result["eligible"] is True
    assert result["unique_result_verified_by"] == "tracking_input_platform_original_order_carrier"


def test_in_transit_does_not_require_unused_order_status():
    context = valid_context("")
    context["wdt_warehouse"] = "示例自营仓"
    context["wdt_logistics_company"] = "中通一拼多多"

    result = DesktopComponentExecutor._assert_query(
        context,
        {
            "expected_platform": "拼多多",
            "skip_status_check": True,
            "allow_exact_tracking_fallback": True,
            "require_original_order_match": True,
        },
    )

    assert result["eligible"] is True
    assert result["order_status"] == ""
    assert context["wdt_eligible"] is True


def test_in_transit_exact_tracking_platform_fallback_rejects_wrong_platform():
    context = valid_context()
    context["wdt_result_count"] = 0
    context["wdt_platform"] = "淘宝"
    context["wdt_logistics_company"] = "韵达一拼多多2"
    with pytest.raises(DesktopExecutionError, match="平台与预期不一致"):
        DesktopComponentExecutor._assert_query(
            context,
            {
                "expected_platform": "拼多多",
                "skip_status_check": True,
                "allow_exact_tracking_fallback": True,
                "require_original_order_match": True,
            },
        )


def test_in_transit_rejects_a_result_bound_to_another_original_order():
    context = valid_context("")
    context["wdt_logistics_company"] = "中通一拼多多"
    context["wdt_original_order"] = "260000-246135142532074"
    with pytest.raises(DesktopExecutionError, match="原始订单号与拼多多不一致"):
        DesktopComponentExecutor._assert_query(
            context,
            {
                "expected_platform": "拼多多",
                "skip_status_check": True,
                "allow_exact_tracking_fallback": True,
                "require_original_order_match": True,
            },
        )


def test_non_allowlisted_status_fails_closed():
    with pytest.raises(DesktopExecutionError):
        DesktopComponentExecutor._assert_query(valid_context("已完成"), {"allowed_statuses": ["已取消"]})


def test_clipboard_set_uses_unicode_clipboard_format():
    class FakeClipboard:
        CF_UNICODETEXT = 13

        def __init__(self):
            self.writes = []

        def OpenClipboard(self):
            return None

        def EmptyClipboard(self):
            return None

        def SetClipboardData(self, clipboard_format, value):
            self.writes.append((clipboard_format, value))

        def CloseClipboard(self):
            return None

    executor = object.__new__(DesktopComponentExecutor)
    clipboard = FakeClipboard()
    executor.win32clipboard = clipboard

    executor._clipboard_set("钉钉\u200b中文")

    assert clipboard.writes == [(clipboard.CF_UNICODETEXT, "钉钉\u200b中文")]


def test_clipboard_restore_does_not_mask_the_operation_error():
    executor = object.__new__(DesktopComponentExecutor)
    executor._clipboard_get = lambda: "old"
    executor._clipboard_set = lambda _value: (_ for _ in ()).throw(RuntimeError("restore failed"))

    with pytest.raises(ValueError, match="business failure"):
        with executor._preserve_clipboard():
            raise ValueError("business failure")


def test_clipboard_restore_failure_does_not_turn_successful_read_into_failure():
    executor = object.__new__(DesktopComponentExecutor)
    executor._clipboard_get = lambda: "old"
    executor._clipboard_set = lambda _value: (_ for _ in ()).throw(RuntimeError("restore failed"))

    with executor._preserve_clipboard():
        value = "已发货"

    assert value == "已发货"


def test_wdt_row_scan_uses_clipboard_sequence_without_writing_sentinel(monkeypatch):
    class Rect:
        left = 0
        top = 0
        right = 1000
        bottom = 700

        def width(self):
            return self.right - self.left

        def height(self):
            return self.bottom - self.top

    class Window:
        def rectangle(self):
            return Rect()

    class Clipboard:
        def __init__(self):
            self.sequence = 1

        def GetClipboardSequenceNumber(self):
            return self.sequence

    class Keyboard:
        def __init__(self, clipboard):
            self.clipboard = clipboard

        def send_keys(self, keys):
            assert keys == "^c"
            self.clipboard.sequence += 1

    executor = object.__new__(DesktopComponentExecutor)
    executor.win32clipboard = Clipboard()
    executor.keyboard = Keyboard(executor.win32clipboard)
    executor.mouse = type("Mouse", (), {"click": lambda self, coords: None})()
    executor._clipboard_get = lambda: "已发货"
    executor._clipboard_set = lambda _value: (_ for _ in ()).throw(AssertionError("sentinel write"))
    executor._semantic_role = lambda _component: "wdt_order_status"
    monkeypatch.setattr("pdd_rpa.desktop_executor.time.sleep", lambda _seconds: None)

    values = executor._scan_wdt_row_values(
        {
            "locator": {"relative_point": {"x_ratio": 0.5, "y_ratio": 0.3}},
            "config": {"scan_span_px": 24, "scan_step_px": 8},
        },
        Window(),
        stop_when=lambda value: value == "已发货",
    )

    assert values == ["已发货"]


def test_wdt_wait_ignores_stale_row_until_exact_tracking_is_visible(monkeypatch):
    executor = object.__new__(DesktopComponentExecutor)
    executor._resolve_window = lambda _component: (object(), {}, {})
    executor._visual_grid_count = lambda _window, _config: 1
    reads = iter([["OLD465500000000000"], ["40000000000000"]])
    executor._scan_wdt_row_values = lambda *_args, **_kwargs: next(reads)
    executor._preserve_clipboard = nullcontext
    monkeypatch.setattr("pdd_rpa.desktop_executor.time.sleep", lambda _seconds: None)

    context = {"tracking_no": "40000000000000"}
    result = executor._wait_for_wdt_query_result(
        {
            "component_id": "CMP-WDT-WAIT-RESULT",
            "config": {
                "minimum_wait_seconds": 0,
                "max_wait_seconds": 2,
                "poll_interval_seconds": 0.1,
            },
        },
        context,
    )

    assert result is True
    assert context["_wdt_result_tracking_synced"] == "40000000000000"
    assert context["_wdt_result_visual_count_seen"] == 1


def test_wdt_wait_times_out_instead_of_accepting_stale_tracking(monkeypatch):
    executor = object.__new__(DesktopComponentExecutor)
    executor._resolve_window = lambda _component: (object(), {}, {})
    executor._visual_grid_count = lambda _window, _config: 1
    executor._scan_wdt_row_values = lambda *_args, **_kwargs: ["OLD465500000000000"]
    executor._preserve_clipboard = nullcontext
    clock = iter([0.0, 0.0, 0.0, 2.0])
    monkeypatch.setattr(
        "pdd_rpa.desktop_executor.time.monotonic",
        lambda: next(clock, 2.0),
    )
    monkeypatch.setattr("pdd_rpa.desktop_executor.time.sleep", lambda _seconds: None)

    with pytest.raises(DesktopExecutionError, match="仍未同步到物流单号"):
        executor._wait_for_wdt_query_result(
            {
                "component_id": "CMP-WDT-WAIT-RESULT",
                "config": {
                    "minimum_wait_seconds": 0,
                    "max_wait_seconds": 1,
                    "poll_interval_seconds": 0.1,
                },
            },
            {"tracking_no": "40000000000000"},
        )


def test_click_component_does_not_touch_clipboard():
    class DummyWindow:
        handle = 1

    class DummyMouse:
        def __init__(self):
            self.calls = []

        def click(self, coords):
            self.calls.append(coords)

    executor = object.__new__(DesktopComponentExecutor)
    executor.mouse = DummyMouse()
    executor._resolve = lambda component: (DummyWindow(), (10, 20))

    result = executor.execute(
        {"component_type": "ClickDesktopElement", "config": {"after_wait_seconds": 0}},
        {},
    )

    assert result is True
    assert executor.mouse.calls == [(10, 20)]


def test_wdt_tracking_input_uses_direct_keys_without_clipboard(monkeypatch):
    class DummyWindow:
        handle = 1

    class DummyMouse:
        def __init__(self):
            self.calls = []

        def click(self, coords):
            self.calls.append(coords)

    class DummyKeyboard:
        def __init__(self):
            self.calls = []

        def send_keys(self, value, **kwargs):
            self.calls.append((value, kwargs))

    executor = object.__new__(DesktopComponentExecutor)
    executor.mouse = DummyMouse()
    executor.keyboard = DummyKeyboard()
    executor._resolve = lambda _component: (DummyWindow(), (10, 20))
    executor._last_resolved_target = None
    executor._clipboard_get = lambda: (_ for _ in ()).throw(AssertionError("clipboard read"))
    executor._clipboard_set = lambda _value: (_ for _ in ()).throw(AssertionError("clipboard write"))
    monkeypatch.setattr("pdd_rpa.desktop_executor.time.sleep", lambda _seconds: None)

    context = {"tracking_no": "YT9600017141082"}
    result = executor.execute(
        {
            "component_id": "CMP-WDT_WAYBILL_INPUT",
            "component_type": "InputDesktopText",
            "name": "输入物流单号",
            "config": {"value_variable": "tracking_no", "input_strategy": "direct_keys_no_clipboard"},
        },
        context,
    )

    assert result == "YT9600017141082"
    assert executor.mouse.calls == [(10, 20)]
    assert [value for value, _kwargs in executor.keyboard.calls] == [
        "^a",
        "YT9600017141082",
    ]
    assert "wdt_waybill" not in context
    assert context["_wdt_tracking_input_value"] == "YT9600017141082"
    assert context["_wdt_tracking_input_mode"] == "direct_keys_result_verified"
    assert context["_wdt_tracking_input_verified"] is False


def test_wdt_tracking_input_defers_proof_to_result_row(monkeypatch):
    class DummyWindow:
        handle = 1

    class DummyMouse:
        def click(self, coords):
            return None

    class DummyKeyboard:
        def send_keys(self, value, **kwargs):
            return None

    executor = object.__new__(DesktopComponentExecutor)
    executor.mouse = DummyMouse()
    executor.keyboard = DummyKeyboard()
    executor._resolve = lambda _component: (DummyWindow(), (10, 20))
    executor._last_resolved_target = None
    executor._clipboard_get = lambda: (_ for _ in ()).throw(AssertionError("clipboard read"))
    executor._clipboard_set = lambda _value: (_ for _ in ()).throw(AssertionError("clipboard write"))
    monkeypatch.setattr("pdd_rpa.desktop_executor.time.sleep", lambda _seconds: None)

    context = {"tracking_no": "YT9600017141082"}
    assert executor.execute(
        {
            "component_id": "CMP-WDT_WAYBILL_INPUT",
            "component_type": "InputDesktopText",
            "name": "输入物流单号",
            "config": {
                "value_variable": "tracking_no",
                "input_strategy": "direct_keys_no_clipboard",
            },
        },
        context,
    ) == "YT9600017141082"


def test_empty_wdt_status_is_a_read_error_not_a_normal_skip():
    context = valid_context("")
    with pytest.raises(DesktopExecutionError, match="订单状态为空"):
        DesktopComponentExecutor._assert_query(context, {"allowed_statuses": ["已取消"]})


def test_visual_grid_count_detects_an_unselected_white_wdt_row():
    image_module = pytest.importorskip("PIL.Image")
    draw_module = pytest.importorskip("PIL.ImageDraw")

    class DummyWindow:
        def capture_as_image(self):
            return image

    image = image_module.new("RGB", (1920, 1040), "white")
    draw = draw_module.Draw(image)
    # Current WDT puts text near y=338 while the saved row anchor is y=310.
    # This is intentionally a white, unselected row: no coloured background.
    draw.rectangle((80, 330, 480, 348), fill=(25, 70, 160))
    # WDT renders the `合计` footer immediately after the first data row. It
    # must never become a second query result.
    draw.rectangle((80, 372, 180, 382), fill=(45, 45, 45))

    assert DesktopComponentExecutor._visual_grid_count(DummyWindow()) == 1


def test_search_component_uses_query_semantic_names():
    component = {"component_id": "CMP-WDT_SEARCH", "component_type": "ClickDesktopElement"}
    spec = DesktopComponentExecutor._semantic_spec(object.__new__(DesktopComponentExecutor), component)
    assert "搜索" in spec["names"]
    assert "查询" in spec["names"]
    assert "Button" in spec["control_types"]


def test_search_component_refuses_coordinate_fallback():
    class DummyWindow:
        handle = 1

    executor = object.__new__(DesktopComponentExecutor)
    executor._resolve_window = lambda component: (DummyWindow(), {}, {})
    executor._find_semantic_target = lambda component, window: None

    with pytest.raises(DesktopExecutionError, match="拒绝坐标兜底"):
        executor._resolve(
            {
                "component_id": "CMP-WDT_SEARCH",
                "component_type": "ClickDesktopElement",
            }
        )


def test_coordinate_fallback_accepts_exact_small_reference_capture():
    class DummyRect:
        left = 0
        top = 0

        @staticmethod
        def width():
            return 896

        @staticmethod
        def height():
            return 648

    class DummyWindow:
        @staticmethod
        def rectangle():
            return DummyRect()

    executor = object.__new__(DesktopComponentExecutor)
    executor._resolve_window = lambda component: (
        DummyWindow(),
        {"resolution_fallback": True},
        {"reference_size": {"width": 896, "height": 648}},
    )
    executor._find_semantic_target = lambda component, window: None
    executor._verify_coordinate_template = lambda *args: None

    window, point = executor._resolve(
        {"component_id": "CMP-WDT_TRACKING_NO", "component_type": "ReadClipboard"}
    )

    assert isinstance(window, DummyWindow)
    assert point is None


def test_image_similarity_is_exact_for_identical_rgb_images():
    pytest.importorskip("PIL")
    from PIL import Image

    image = Image.new("RGB", (8, 8), (80, 120, 160))

    assert DesktopComponentExecutor._image_similarity(image, image.copy()) == 1.0


def test_wdt_logistics_company_rejects_warehouse_and_accepts_carrier_value():
    assert not DesktopComponentExecutor._looks_like_logistics_company("示例自营仓")
    assert DesktopComponentExecutor._looks_like_logistics_company("中通—拼多多")
    assert DesktopComponentExecutor._looks_like_wdt_warehouse("示例自营仓")
    assert not DesktopComponentExecutor._looks_like_wdt_warehouse("中通—拼多多")


def _ocr_line(text, top, words):
    return {
        "text": text,
        "words": [
            {"text": value, "left": left, "top": top, "width": width, "height": 14}
            for value, left, width in words
        ],
    }


def test_wecom_group_search_accepts_group_and_rejects_global_search_row():
    group = "【快递】示例团队&中通对接群"
    group_words = [(character, 50 + index * 18, 16) for index, character in enumerate(group)]
    payload = {
        "lines": [
            _ocr_line("\u7fa4\u804a", 10, [("\u7fa4", 14, 16), ("\u804a", 32, 16)]),
            _ocr_line(group, 62, group_words),
            _ocr_line("\u8fdb\u5165\u5168\u5c40\u641c\u7d22", 125, [("\u8fdb", 20, 16), ("\u5165", 38, 16)]),
            _ocr_line(group, 154, group_words),
        ]
    }

    result = DesktopComponentExecutor._wecom_exact_search_candidate_from_ocr(
        payload, group, 400, 194
    )

    assert result is not None
    assert result["observed_text"] == group
    assert result["row"]["top"] == 62.0
    assert result["global_search_top"] == 125.0


def test_wecom_group_search_rejects_group_found_only_below_global_search():
    group = "【快递】示例团队&中通对接群"
    group_words = [(character, 50 + index * 18, 16) for index, character in enumerate(group)]
    payload = {
        "lines": [
            _ocr_line("\u8fdb\u5165\u5168\u5c40\u641c\u7d22", 95, [("\u8fdb", 20, 16), ("\u5165", 38, 16)]),
            _ocr_line(group, 130, group_words),
        ]
    }

    result = DesktopComponentExecutor._wecom_exact_search_candidate_from_ocr(
        payload, group, 400, 194
    )

    assert result is None


def test_chat_group_match_tolerates_one_ocr_glyph_per_name_segment_only():
    expected = "【快递】示例团队&中通对接群"
    assert DesktopComponentExecutor._chat_group_text_matches(
        expected, "\uff08\u5feb\u9012\u3015\u793a\u4f8b\u56e2\u961f&\u4e2d\u901a\u5bf9\u63a5\u8a54"
    )
    assert not DesktopComponentExecutor._chat_group_text_matches(
        expected, "\u5feb\u9012示例团队\u56fe\u4e66\u4e13\u8425\u5e97"
    )


def test_wecom_group_search_accepts_bounded_ocr_drift_in_unique_chat_row():
    expected = "示例供应商 D"
    observed = "示例供应南 D"  # one OCR glyph drift: 商 -> 南
    words = [(character, 60 + index * 20, 18) for index, character in enumerate(observed)]
    payload = {
        "lines": [
            _ocr_line(observed, 58, words),
            _ocr_line("\u8fdb\u5165\u5168\u5c40\u641c\u7d22", 116, [("\u8fdb", 20, 16), ("\u5165", 38, 16)]),
        ]
    }

    result = DesktopComponentExecutor._wecom_exact_search_candidate_from_ocr(
        payload, expected, 360, 190
    )

    assert result is not None
    assert result["match_mode"] == "ocr_text"
    assert result["observed_text"] == observed


def test_wecom_group_search_uses_topmost_text_line_for_one_result_card():
    expected = "示例供应商 B"
    payload = {
        "lines": [
            _ocr_line("示例供应商 B（51）", 48, [("时", 66, 16), ("光", 84, 16), ("学", 102, 16)]),
            _ocr_line(
                "群聊名称：【分销】无锡示例团队&示例供应商 B",
                72,
                [("时", 210, 16), ("光", 228, 16), ("学", 246, 16)],
            ),
            _ocr_line("进入全局搜索", 130, [("进", 20, 16), ("入", 38, 16)]),
        ]
    }

    result = DesktopComponentExecutor._wecom_exact_search_candidate_from_ocr(
        payload, expected, 400, 190
    )

    assert result is not None
    assert result["match_mode"] == "ocr_topmost_text"
    assert result["row"]["top"] == 48.0


def test_wecom_group_match_ignores_live_member_count_suffix():
    expected = "【分销】示例供应商 G 对接群"

    assert DesktopComponentExecutor._chat_group_text_matches(
        expected, "【分销】示例供应商 G 对接群（32）"
    )


def test_wecom_real_ocr_drift_keeps_global_search_out_of_unique_fallback():
    expected = "【分销】示例供应商 G 对接群"
    payload = {
        "lines": [
            _ocr_line(
                "〔分〕启艾轩&匹厍蕊书0香冂笙〕",
                52,
                [("?", 60, 18)],
            ),
            _ocr_line("02〕", 53, [("02", 308, 18), ("〕", 327, 3)]),
            _ocr_line(
                "进入全到索〔Ctrl+Alt+F〕",
                116,
                [("进", 20, 16), ("入", 38, 16), ("F", 150, 12)],
            ),
        ]
    }

    result = DesktopComponentExecutor._wecom_exact_search_candidate_from_ocr(
        payload, expected, 400, 190
    )

    assert result is not None
    assert result["match_mode"] == "unique_chat_row"
    assert result["row"]["top"] == 52.0


def test_wecom_group_search_uses_only_one_chat_row_when_name_is_unreadable():
    expected = "示例供应商 D"
    observed = "示例供应商 D"  # OCR payload has no usable text match in this case
    words = [("?", 60 + index * 20, 18) for index, _ in enumerate(observed)]
    payload = {
        "lines": [
            _ocr_line("", 58, words),
            _ocr_line("\u8fdb\u5165\u5168\u5c40\u641c\u7d22", 116, [("\u8fdb", 20, 16), ("\u5165", 38, 16)]),
        ]
    }

    result = DesktopComponentExecutor._wecom_exact_search_candidate_from_ocr(
        payload, expected, 360, 190
    )

    assert result is not None
    assert result["match_mode"] == "unique_chat_row"


def test_wecom_group_search_rejects_multiple_unreadable_chat_rows():
    expected = "示例供应商 D"
    payload = {
        "lines": [
            _ocr_line("", 58, [("?", 60, 18)]),
            _ocr_line("", 82, [("?", 60, 18)]),
            _ocr_line("\u8fdb\u5165\u5168\u5c40\u641c\u7d22", 116, [("\u8fdb", 20, 16), ("\u5165", 38, 16)]),
        ]
    }

    result = DesktopComponentExecutor._wecom_exact_search_candidate_from_ocr(
        payload, expected, 360, 190
    )

    assert result is None
