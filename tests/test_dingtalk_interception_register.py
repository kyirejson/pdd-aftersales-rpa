from pathlib import Path

import pytest

from pdd_rpa.dingtalk_interception_register import (
    DINGTALK_TARGET_JUNFU,
    DINGTALK_TARGET_SIKU_DISTRIBUTION,
    DingTalkInterceptionRegisterConfig,
    DingTalkInterceptionRegisterError,
    DingTalkInterceptionRegisterWriter,
    dingtalk_row_values,
    dingtalk_target_key_for_warehouse,
    format_dingtalk_date,
    resolve_dingtalk_target_config,
)


def configured_writer(layout=DINGTALK_TARGET_JUNFU):
    writer = object.__new__(DingTalkInterceptionRegisterWriter)
    writer.config = DingTalkInterceptionRegisterConfig.from_raw(
        {"enabled": True, "target_key": layout, "layout": layout}
    )
    return writer


def test_dingtalk_date_has_no_zero_padded_month_or_day():
    assert format_dingtalk_date("2026-08-12") == "2026/8/12"


def test_dingtalk_values_skip_selectable_refund_reason_column():
    values = dingtalk_row_values(
        {
            "record_date": "2026-08-12",
            "tracking_no": "YT123",
            "carrier": "圆通快递",
            "order_no": "260812-1",
            "refund_reason": "主观原因不想要",
            "amount": 19.8,
        },
        "rpa",
        written_on="2026-08-13",
    )
    assert values == {
        "date": "2026/8/13",
        "registrar": "rpa",
        "group_name": "",
        "tracking_no": "YT123",
        "order_no": "260812-1",
        "amount": "19.80",
    }
    assert "carrier" not in values
    assert "refund_reason" not in values


def test_config_has_cdp_endpoint_and_no_screen_coordinate_options():
    config = DingTalkInterceptionRegisterConfig.from_raw({"enabled": True})
    assert config.cdp_endpoint == "http://127.0.0.1:9222"
    assert config.playwright_user_data_dir == ""
    assert not hasattr(config, "start_row")
    assert not hasattr(config, "row_scroll_attempts")


def test_exact_warehouse_routing_and_target_config_inheritance():
    raw = {
        "enabled": True,
        "cdp_endpoint": "http://127.0.0.1:9222",
        "targets": {
            "junfu": {
                "warehouse_aliases": ["示例自营仓"],
                "url": "https://example.test/junfu",
                "layout": "junfu",
            },
            "siku_distribution": {
                "warehouse_aliases": ["示例供应链分销", "示例供应链代发总仓"],
                "url": "https://example.test/siku",
                "layout": "siku_distribution",
            },
        },
    }
    assert dingtalk_target_key_for_warehouse(raw, " 示例自营仓 ") == "junfu"
    assert (
        dingtalk_target_key_for_warehouse(raw, "示例供应链代发总仓")
        == "siku_distribution"
    )
    assert dingtalk_target_key_for_warehouse(raw, "示例自营仓-其他") == ""
    selected = resolve_dingtalk_target_config(raw, "siku_distribution")
    assert selected["enabled"] is True
    assert selected["cdp_endpoint"] == "http://127.0.0.1:9222"
    assert selected["url"] == "https://example.test/siku"
    assert selected["target_key"] == "siku_distribution"


def test_document_url_key_ignores_query_string():
    assert DingTalkInterceptionRegisterWriter._document_url_key(
        "https://example.com/dingtalk-doc-placeholder?utm_scene=team_space"
    ) == "/dingtalk-doc-placeholder"


def test_selected_row_reads_the_dingtalk_selection_display():
    class FakeSelection:
        def count(self):
            return 1

        def inner_text(self):
            return "A23876"

    class FakeSheet:
        def locator(self, selector):
            assert selector == '[data-testid="SelectionDisplayContainer"]'
            return FakeSelection()

    writer = configured_writer()
    assert writer._selected_row(FakeSheet()) == 23876


def test_selected_row_refuses_ambiguous_dom_state():
    class FakeSelections:
        def count(self):
            return 2

    class FakeSheet:
        def locator(self, _selector):
            return FakeSelections()

    writer = object.__new__(DingTalkInterceptionRegisterWriter)
    with pytest.raises(DingTalkInterceptionRegisterError, match="不唯一"):
        writer._selected_row(FakeSheet())


def test_selected_row_accepts_duplicate_visible_nodes_with_same_address():
    class FakeSelection:
        def __init__(self, value):
            self.value = value

        def is_visible(self):
            return True

        def inner_text(self):
            return self.value

    class FakeSelections:
        def __init__(self):
            self.items = [FakeSelection("A23876"), FakeSelection("A23876")]

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class FakeSheet:
        def locator(self, _selector):
            return FakeSelections()

    writer = configured_writer()
    assert writer._selected_row(FakeSheet()) == 23876


def test_last_reference_offset_uses_date_tracking_or_order_and_ignores_other_columns():
    offset = DingTalkInterceptionRegisterWriter._last_reference_offset(
        [
            "日期\t登记员\t快递单号\t快递公司\t原始订单号",
            "\t\tYT001\t圆通\t",
            "\t人工甲\t\t申通\t",
            "\t\t\t\t260819-4",
            "2026/8/20\t\t\t中通\t",
            "\t人工乙\t\t韵达\t",
        ],
        6,
    )
    assert offset == 4


def test_find_last_reference_row_scans_a_c_and_e_and_ignores_b_d():
    writer = configured_writer()
    calls = []
    writer._focus_grid = lambda grid: calls.append(("focus", grid))
    writer._press = lambda page, key, label: calls.append(("press", key, label))
    writer._selected_row = lambda sheet: 6

    def copy_range(_page, _sheet, _grid, address, stage):
        calls.append(("copy", address, stage))
        return [
            "日期\t登记员\t快递单号\t快递公司\t原始订单号",
            "\t\tYT001\t圆通\t260819-1",
            "\t人工甲\t\t申通\t",
            "2026/8/20\t\t\t\t",
            "\t\t\t中通\t",
            "\t\t\t\t",
        ]

    writer._copy_range = copy_range

    assert writer._find_last_reference_row("page", "sheet", "grid") == 4
    assert (
        "copy",
        "A1:E6",
        "读取钉钉登记目标列",
    ) in calls


def test_online_duplicate_requires_an_exact_tracking_number_match():
    contains = DingTalkInterceptionRegisterWriter._contains_tracking
    assert contains(["", " 465580779071899 ", "another"], "465580779071899")
    assert not contains(["4655807790718990", "46558077907189"], "465580779071899")


def test_append_uses_cdp_and_never_constructs_desktop_executor():
    config = DingTalkInterceptionRegisterConfig.from_raw({"enabled": True})
    writer = configured_writer()
    writer.config = config

    assert not hasattr(writer, "desktop")
    assert writer.config.cdp_endpoint == "http://127.0.0.1:9222"


def test_writer_source_never_closes_the_user_edge_browser():
    source = Path(__file__).parents[1].joinpath(
        "pdd_rpa", "dingtalk_interception_register.py"
    ).read_text(encoding="utf-8")
    assert "browser.close()" not in source
    assert "bounding_box()" not in source
    assert "click(position=" not in source


def test_append_skips_carrier_and_refund_reason_columns():
    writer = configured_writer()
    calls = []
    writer._read_cells = lambda *_args: {}
    writer._select_address = lambda _page, _sheet, address: calls.append(
        ("address", address)
    )
    writer._paste_and_tab = lambda _page, value, label: calls.append(
        ("paste_tab", value, label)
    )
    writer._paste_current_cell = lambda _page, value, label: calls.append(
        ("paste", value, label)
    )
    writer._assert_current_cell = lambda _page, value, label: calls.append(
        ("assert", value, label)
    )
    writer._press = lambda _page, key, label: calls.append(("press", key, label))

    row = writer._append_after_last_reference(
        None,
        None,
        None,
        {
            "date": "2026/8/19",
            "registrar": "rpa",
            "tracking_no": "YT123",
            "order_no": "260819-1",
            "amount": "19.80",
        },
        False,
        last_row=23876,
    )

    assert row == 23877
    assert ("press", "Tab", "跳过快递公司列") in calls
    assert not any("写入快递公司" in call for call in calls)
    assert ("press", "Tab", "跳过退款原因列") in calls


def test_siku_last_reference_scans_a_e_f_h_j_and_ignores_other_columns():
    writer = configured_writer(DINGTALK_TARGET_SIKU_DISTRIBUTION)
    calls = []
    writer._focus_grid = lambda grid: calls.append(("focus", grid))
    writer._press = lambda page, key, label: calls.append(("press", key, label))
    writer._selected_row = lambda sheet: 5

    def copy_range(_page, _sheet, _grid, address, stage):
        calls.append(("copy", address, stage))
        return [
            "日期\t登记员\t其他\t其他\t群名\t快递单号\t其他\t订单号\t其他\t金额",
            "\t\t仅C有值\t\t\t\t\t\t\t",
            "\t\t\t\t分销群\tYT-1\t\tORDER-1\t\t19.80",
            "\t\t\tD列人工值\t\t\t\t\t\t",
            "2026/8/26\t\t\t\t\t\t\t\t\t",
        ]

    writer._copy_range = copy_range
    assert writer._find_last_reference_row("page", "sheet", "grid") == 5
    assert ("copy", "A1:J5", "读取钉钉登记目标列") in calls


def test_siku_append_writes_only_a_b_e_f_h_j():
    writer = configured_writer(DINGTALK_TARGET_SIKU_DISTRIBUTION)
    calls = []
    writer._read_cells = lambda *_args: {}
    writer._select_address = lambda _page, _sheet, address: calls.append(
        ("address", address)
    )
    writer._paste_and_tab = lambda _page, value, label: calls.append(
        ("paste_tab", value, label)
    )
    writer._paste_current_cell = lambda _page, value, label: calls.append(
        ("paste", value, label)
    )
    writer._assert_current_cell = lambda _page, value, label: calls.append(
        ("assert", value, label)
    )

    row = writer._append_after_last_reference(
        None,
        None,
        None,
        {
            "date": "2026/8/26",
            "registrar": "rpa",
            "group_name": "示例供应商 A",
            "tracking_no": "YT123",
            "order_no": "260826-1",
            "amount": "19.80",
        },
        True,
        last_row=100,
    )

    assert row == 101
    assert [item[1] for item in calls if item[0] == "address"] == [
        "A101",
        "A101",
        "E101",
        "F101",
        "H101",
        "J101",
    ]
    pasted = [item[1] for item in calls if item[0] in {"paste", "paste_tab"}]
    assert pasted == ["2026/8/26", "rpa", "示例供应商 A", "YT123", "260826-1", "19.80"]
    assert not any(item[1] == "重复" for item in calls if len(item) > 1)
