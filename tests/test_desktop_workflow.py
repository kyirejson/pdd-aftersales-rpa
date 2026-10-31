from pdd_rpa.desktop_workflow import DesktopWorkflowRunner


def test_guarded_wdt_node_runs_only_for_exact_warehouse():
    node = {"when": {"wdt_warehouse": "示例供应链代发总仓"}}
    assert DesktopWorkflowRunner._node_enabled(
        node, {"wdt_warehouse": "示例供应链代发总仓"}
    )
    assert not DesktopWorkflowRunner._node_enabled(
        node, {"wdt_warehouse": "示例自营仓"}
    )
