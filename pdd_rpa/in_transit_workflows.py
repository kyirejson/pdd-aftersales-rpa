from __future__ import annotations

from typing import Any

from .components import validate_workflow


def _linear(workflow_id: str, name: str, description: str, steps: list[tuple[str, str, str]]) -> dict[str, Any]:
    nodes = [
        {"node_id": node_id, "component_type": component_type, "name": label}
        for node_id, component_type, label in steps
    ]
    edges = [
        {"from": nodes[index]["node_id"], "to": nodes[index + 1]["node_id"]}
        for index in range(len(nodes) - 1)
    ]
    return validate_workflow(
        {
            "workflow_id": workflow_id,
            "name": name,
            "description": description,
            "enabled": False,
            "nodes": nodes,
            "edges": edges,
        }
    )


def definitions() -> list[dict[str, Any]]:
    common = [
        ("refresh", "RefreshPage", "刷新拼多多店铺页"),
        ("pending", "EnsurePendingFilter", "确保待处理已选中"),
        ("detail", "OpenWorkOrderDetail", "点击立即处理"),
        ("wdt_company", "ReadWdtLogisticsCompany", "查询并读取旺店通物流公司"),
        ("classify", "ClassifyFulfillmentRoute", "判断三类发货路由"),
    ]
    workflows = [
        _linear(
            "WF-PDD-IN-TRANSIT-ROUTER",
            "在途无理由退款总路由",
            "采集订单后按旺店通物流公司进入三个独立分支；未知或冲突时转人工。",
            common,
        ),
        _linear(
            "WF-PDD-IN-TRANSIT-DIRECT",
            "平台直发拦截与退款判断",
            "向快递群发送拦截，回读成功后按最新物流地点白名单决定同意或拒绝。",
            common
            + [
                ("resolve", "ResolveInterceptionGroup", "解析快递群"),
                ("send", "SendGroupInterception", "发送拦截消息"),
                ("verify", "VerifyGroupMessage", "回读验证消息"),
                ("logistics", "ReadLatestPddLogistics", "读取拼多多最新物流"),
                ("location", "MatchLocationWhitelist", "匹配可退款地点"),
                ("agree", "AgreeRefund", "命中地点时同意退款"),
                ("reject", "RejectRefundFixedReason", "未命中时固定原因拒绝"),
            ],
        ),
        _linear(
            "WF-PDD-IN-TRANSIT-JST",
            "聚水潭供应商拦截",
            "按快递单号查供应商，向供应商群发送并回读，随后固定原因拒绝退款。",
            common
            + [
                ("supplier", "QueryJstSupplierByTracking", "聚水潭查询供应商"),
                ("resolve", "ResolveInterceptionGroup", "解析供应商群"),
                ("send", "SendGroupInterception", "发送拦截消息"),
                ("verify", "VerifyGroupMessage", "回读验证消息"),
                ("reject", "RejectRefundFixedReason", "固定原因拒绝退款"),
            ],
        ),
        _linear(
            "WF-PDD-IN-TRANSIT-DROPSHIP",
            "旺店通代发供应商拦截",
            "从订单日志读取选仓公司，向对应供应商群发送并回读，随后固定原因拒绝退款。",
            common
            + [
                ("supplier", "ReadWdtWarehouseLogCompany", "读取订单日志选仓公司"),
                ("resolve", "ResolveInterceptionGroup", "解析供应商群"),
                ("send", "SendGroupInterception", "发送拦截消息"),
                ("verify", "VerifyGroupMessage", "回读验证消息"),
                ("reject", "RejectRefundFixedReason", "固定原因拒绝退款"),
            ],
        ),
    ]
    workflows.append(
        validate_workflow(
            {
                "workflow_id": "WF-WDT-IN-TRANSIT-LOOKUP",
                "name": "旺店通在途订单查询",
                "description": "按拼多多详情取得的物流单号查询旺店通，输入后立即回读并核对唯一结果、原始订单、平台和物流公司；不套用无轨迹退款状态白名单。",
                "enabled": False,
                "nodes": [
                    {"node_id": "activate", "component_id": "CMP-WDT-ACTIVATE", "component_type": "ActivateDesktopWindow", "name": "激活旺店通"},
                    {"node_id": "management", "component_id": "CMP-WDT_ORDER_MANAGEMENT", "component_type": "ClickDesktopElement", "name": "打开订单管理"},
                    {"node_id": "input", "component_id": "CMP-WDT_WAYBILL_INPUT", "component_type": "InputDesktopText", "name": "输入物流单号"},
                    {"node_id": "search", "component_id": "CMP-WDT_SEARCH", "component_type": "ClickDesktopElement", "name": "搜索"},
                    {"node_id": "wait", "component_id": "CMP-WDT-WAIT-RESULT", "component_type": "WaitDesktopState", "name": "等待查询结果"},
                    {"node_id": "result_count", "component_id": "CMP-WDT-RESULT-COUNT", "component_type": "ReadDesktopGridRow", "name": "识别查询结果行数"},
                    {"node_id": "waybill", "component_id": "CMP-WDT_WAYBILL", "component_type": "ReadDesktopText", "name": "回读结果物流单号"},
                    {"node_id": "order", "component_id": "CMP-WDT_ORDER_ID", "component_type": "ReadDesktopText", "name": "读取旺店通订单"},
                    {"node_id": "platform", "component_id": "CMP-WDT_PLATFORM", "component_type": "ReadDesktopText", "name": "读取平台"},
                    {"node_id": "original", "component_id": "CMP-WDT_ORIGINAL_ORDER", "component_type": "ReadDesktopText", "name": "读取原始订单"},
                    {"node_id": "warehouse", "component_id": "CMP-WDT-WAREHOUSE", "component_type": "ReadDesktopText", "name": "读取旺店通仓库"},
                    {"node_id": "company", "component_id": "CMP-WDT-LOGISTICS-COMPANY", "component_type": "ReadDesktopText", "name": "读取物流公司"},
                    {"node_id": "result_count_confirm", "component_id": "CMP-WDT-RESULT-COUNT", "component_type": "ReadDesktopGridRow", "name": "再次确认查询结果行数"},
                    {"node_id": "assert", "component_id": "CMP-WDT-ASSERT-IN-TRANSIT", "component_type": "AssertDesktopQueryResult", "name": "校验唯一订单及平台"},
                ],
                "edges": [
                    {"from": a, "to": b}
                    for a, b in zip(
                        ["activate", "management", "input", "search", "wait", "result_count", "waybill", "order", "platform", "original", "warehouse", "company", "result_count_confirm"],
                        ["management", "input", "search", "wait", "result_count", "waybill", "order", "platform", "original", "warehouse", "company", "result_count_confirm", "assert"],
                    )
                ],
            }
        )
    )
    return workflows
