from __future__ import annotations

from pywinauto import Desktop, mouse

from pdd_rpa.components import validate_component, validate_workflow
from pdd_rpa.config import load_config
from pdd_rpa.db import Repository
from pdd_rpa.desktop_capture import DesktopCaptureService


CAPTURES = [
    ("wdt_order_management", "点击订单管理", "ClickDesktopElement", (735, 91), {}),
    ("wdt_waybill_input", "输入物流单号", "InputDesktopText", (1368, 175), {"value_variable": "tracking_no"}),
    ("wdt_search", "点击搜索", "ClickDesktopElement", (1717, 175), {"after_wait_seconds": 0.5}),
    # 2026-08-22 WDT flagship grid, verified against a real unique result.
    # Capture current cell centres; guarded readers scan only within a column.
    ("wdt_order_status", "读取订单状态", "ReadDesktopText", (535, 307), {"output_variable": "wdt_order_status", "preferred_offset_px": 0, "scan_span_px": 24}),
    ("wdt_waybill", "回读旺店通物流单号", "ReadDesktopText", (1850, 307), {"output_variable": "wdt_waybill", "preferred_offset_px": 0, "scan_span_px": 24}),
]


def main() -> None:
    config = load_config()
    repository = Repository(config.database_path)
    repository.initialize()
    capture = DesktopCaptureService(config.evidence_dir)
    nodes = []
    window = Desktop(backend="win32").window(title_re="^旺店通ERP旗舰版$")
    if window.is_minimized():
        window.restore()
    window.set_focus()

    activate = validate_component({
        "component_id": "CMP-WDT-ACTIVATE",
        "component_type": "ActivateDesktopWindow",
        "name": "激活旺店通ERP旗舰版",
        "application": "旺店通ERP旗舰版",
        "config": {"window": {"title": "旺店通ERP旗舰版", "reference_size": {"width": 1920, "height": 1040}}},
    })
    repository.save_component(activate)
    nodes.append({"node_id": "activate", "component_id": activate["component_id"], "component_type": activate["component_type"], "name": activate["name"]})

    for key, name, component_type, point, component_config in CAPTURES:
        if key == "wdt_search":
            component_config = {
                **component_config,
                "after_wait_seconds": 1.0,
                "wait_for_window_ready": True,
                "response_timeout_seconds": 5,
                "click_strategy": "win32_point_no_clipboard",
            }
        mouse.move(coords=point)
        captured = capture._capture_now("旺店通ERP旗舰版")
        component = capture.component_from_capture(captured, name, component_type)
        component["component_id"] = f"CMP-{key.upper()}"
        component["config"] = component_config
        repository.save_component(component)
        nodes.append({"node_id": key, "component_id": component["component_id"], "component_type": component_type, "name": name})
        if key == "wdt_search":
            wait = validate_component({
                "component_id": "CMP-WDT-WAIT-RESULT",
                "component_type": "WaitDesktopState",
                "name": "等待旺店通查询结果",
                "application": "旺店通ERP旗舰版",
                "config": {
                    "seconds": 8,
                    "poll_for_grid": True,
                    "max_wait_seconds": 8,
                    "poll_interval_seconds": 0.25,
                },
            })
            repository.save_component(wait)
            nodes.append({"node_id": "wdt_wait", "component_id": wait["component_id"], "component_type": wait["component_type"], "name": wait["name"]})
            mouse.move(coords=(100, 310))
            grid_capture = capture._capture_now("旺店通ERP旗舰版")
            grid = capture.component_from_capture(grid_capture, "识别查询结果行数", "ReadDesktopGridRow")
            grid["component_id"] = "CMP-WDT-RESULT-COUNT"
            grid["config"] = {"first_row_y_ratio": 310 / 1040, "row_height_ratio": 64 / 1040, "left_ratio": 40 / 1920, "right_ratio": 1800 / 1920, "half_height": 10, "color_distance": 35, "min_colored_ratio": 0.3, "max_rows": 6, "output_variable": "wdt_result_count"}
            repository.save_component(grid)
            nodes.append({"node_id": "wdt_result_count", "component_id": grid["component_id"], "component_type": grid["component_type"], "name": grid["name"]})

    assertion = validate_component({
        "component_id": "CMP-WDT-ASSERT-REFUNDABLE",
        "component_type": "AssertDesktopQueryResult",
        "name": "校验唯一订单及退款状态",
        "application": "旺店通ERP旗舰版",
        "config": {"allowed_statuses": ["未审核", "已取消", "已审核", "待客审"]},
    })
    repository.save_component(assertion)
    nodes.append({"node_id": "wdt_assert", "component_id": assertion["component_id"], "component_type": assertion["component_type"], "name": assertion["name"]})
    workflow = validate_workflow({
        "workflow_id": "WF-WDT-SHIPPED-NO-TRACE-CHECK",
        "name": "旺店通物流单号与退款状态校验",
        "description": "物流单号查询唯一结果，回读同一物流单号及订单状态后判断是否可退款",
        "enabled": False,
        "version": 6,
        "nodes": nodes,
        "edges": [{"from": nodes[i]["node_id"], "to": nodes[i + 1]["node_id"]} for i in range(len(nodes) - 1)],
    })
    repository.save_workflow(workflow)
    print(f"saved {len(nodes)} nodes to {workflow['workflow_id']}")


if __name__ == "__main__":
    main()
