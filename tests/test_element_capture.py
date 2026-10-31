from pathlib import Path

from pdd_rpa.element_capture import ElementCaptureService


def test_uia_capture_builds_edge_component_without_dom_selector():
    capture = {
        "engine": "edge_uia",
        "element": {
            "name": "立即处理",
            "control_type": "Text",
            "automation_id": "",
            "class_name": "",
            "rect": {"x": 100, "y": 200, "width": 60, "height": 20},
        },
        "ancestry": [{"control_type": "Document", "name": "工单管理"}],
    }
    component = ElementCaptureService.component_from_capture(capture, "点击立即处理")
    assert component["component_type"] == "ClickElement"
    assert component["locator"]["text"] == "立即处理"
    assert component["locator"]["uia_control_type"] == "Text"
    assert component["locator"]["engine"] == "edge_uia"
    assert not component["locator"].get("selector")


def test_active_pdd_uia_modules_do_not_use_javascript_or_whole_page_clipboard():
    root = Path(__file__).resolve().parents[1] / "pdd_rpa"
    for filename in ("edge_uia.py", "browser_executor.py", "detail_collector.py", "element_capture.py"):
        source = (root / filename).read_text(encoding="utf-8")
        assert "prompt_json" not in source
        assert "execute_script" not in source
        assert 'send_keys("^a")' not in source
        assert "win32clipboard" not in source
