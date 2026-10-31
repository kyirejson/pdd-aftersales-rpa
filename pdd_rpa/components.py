from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any


class ComponentValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    component_type: str
    name: str
    category: str
    description: str
    write_capable: bool = False


CATALOG = (
    ComponentSpec("EnsurePendingFilter", "勾选待处理筛选", "拼多多", "刷新后检查工单筛选，并确保待处理卡片处于选中状态", True),
    ComponentSpec("RefreshPage", "刷新页面", "浏览器", "刷新已绑定店铺页面，并等待总数与卡片连续两次稳定"),
    ComponentSpec("ActivateWindow", "激活窗口", "浏览器", "激活指定店铺的Edge窗口"),
    ComponentSpec("AssertShopIdentity", "校验店铺", "安全", "核验Profile、URL和页面完整店铺名"),
    ComponentSpec("GoToPage", "打开页面", "浏览器", "打开白名单内页面"),
    ComponentSpec("ForEachPage", "遍历分页", "页面", "依次遍历数字分页并恢复原页码"),
    ComponentSpec("FindWorkOrderCard", "查找工单卡片", "页面", "按订单号和创建时间唯一定位"),
    ComponentSpec("ClickElement", "点击元素", "页面", "点击唯一且未遮挡的页面元素", True),
    ComponentSpec("ReadText", "读取文本", "页面", "读取元素文本，不修改页面"),
    ComponentSpec("ReadDetailFields", "读取详情字段", "拼多多", "读取运单号、物流和退款字段"),
    ComponentSpec("CloseDetailTab", "关闭详情标签", "浏览器", "关闭已绑定的详情页"),
    ComponentSpec("SaveEvidence", "保存证据", "审计", "保存截图和结构化证据"),
    ComponentSpec("Condition", "条件判断", "逻辑", "根据规则选择流程分支"),
    ComponentSpec("ManualApproval", "人工审批", "安全", "暂停并等待人工批准"),
    ComponentSpec("ActivateDesktopWindow", "激活桌面窗口", "桌面应用", "按进程和窗口标题激活桌面客户端"),
    ComponentSpec("ClickDesktopElement", "点击桌面元素", "桌面应用", "按捕获的控件特征点击桌面元素", True),
    ComponentSpec("InputDesktopText", "输入桌面文本", "桌面应用", "聚焦捕获的输入框并写入变量文本", True),
    ComponentSpec("ReadDesktopText", "读取桌面文本", "桌面应用", "读取捕获控件的文本或值"),
    ComponentSpec("ReadDesktopGridRow", "读取桌面表格行", "旺店通", "读取唯一查询结果的订单、平台、状态和物流单号"),
    ComponentSpec("WaitDesktopState", "等待桌面状态", "桌面应用", "等待加载完成或控件状态变化"),
    ComponentSpec("EvaluateAllowedSet", "白名单判断", "逻辑", "判断字段是否属于允许值集合"),
    ComponentSpec("AssertDesktopQueryResult", "校验旺店通结果", "旺店通", "校验唯一结果、平台、原始单号、物流单号和状态白名单"),
    ComponentSpec("OpenWorkOrderDetail", "打开工单详情", "拼多多", "按订单号唯一定位并打开立即处理详情"),
    ComponentSpec("WaitBrowserState", "等待页面状态", "拼多多", "等待页面出现指定阶段文本"),
    ComponentSpec("VerifyRefundSuccess", "验证退款成功", "拼多多", "回读同一订单和工单ID的退款成功状态"),
    ComponentSpec("ReadWdtLogisticsCompany", "读取旺店通物流公司", "旺店通", "查询订单并读取物流公司原始文本"),
    ComponentSpec("ClassifyFulfillmentRoute", "判断发货路由", "逻辑", "按可配置规则区分平台直发、聚水潭发货和旺店通代发"),
    ComponentSpec("QueryJstSupplierByTracking", "聚水潭查询供应商", "聚水潭", "按快递单号查询唯一订单并读取供应商"),
    ComponentSpec("ReadWdtWarehouseLogCompany", "读取选仓公司", "旺店通", "打开订单日志并读取货品选仓库后的公司名"),
    ComponentSpec("ResolveInterceptionGroup", "解析拦截群", "逻辑", "按快递公司或供应商匹配唯一平台、账号和群"),
    ComponentSpec("SendGroupInterception", "发送拦截消息", "消息平台", "使用真实账号向唯一目标群发送拦截单号", True),
    ComponentSpec("VerifyGroupMessage", "回读群消息", "消息平台", "回读群聊并核验账号、群名、单号和发送时间"),
    ComponentSpec("ReadLatestPddLogistics", "读取最新物流", "拼多多", "回到工单详情读取最新一条物流轨迹"),
    ComponentSpec("MatchLocationWhitelist", "匹配退款地点", "逻辑", "按快递公司匹配可配置地点关键词白名单"),
    ComponentSpec("RejectRefundFixedReason", "固定原因拒绝退款", "拼多多", "选择全局固定的拒绝原因并提交", True),
    ComponentSpec("AgreeRefund", "同意退款", "拼多多", "经证据和地点白名单校验后提交同意退款", True),
)

CATALOG_BY_TYPE = {item.component_type: item for item in CATALOG}


def catalog() -> list[dict[str, Any]]:
    return [
        {
            "component_type": item.component_type,
            "name": item.name,
            "category": item.category,
            "description": item.description,
            "write_capable": item.write_capable,
        }
        for item in CATALOG
    ]


def new_component_id(component_type: str) -> str:
    prefix = re.sub(r"[^A-Z0-9]+", "_", component_type.upper()).strip("_")
    return f"CMP-{prefix}-{uuid.uuid4().hex[:8].upper()}"


def validate_component(definition: dict[str, Any]) -> dict[str, Any]:
    component_type = str(definition.get("component_type", ""))
    if component_type not in CATALOG_BY_TYPE:
        raise ComponentValidationError(f"未知组件类型：{component_type}")
    name = str(definition.get("name", "")).strip()
    if not name:
        raise ComponentValidationError("组件名称不能为空")
    component_id = str(definition.get("component_id", "")).strip() or new_component_id(
        component_type
    )
    locator = definition.get("locator") or {}
    if component_type in {"ClickElement", "ReadText"}:
        if not isinstance(locator, dict) or not any(
            locator.get(key)
            for key in (
                "selector",
                "text",
                "aria_label",
                "anchor_text",
                "automation_id",
                "class_name",
                "uia_control_type",
            )
        ):
            raise ComponentValidationError("页面元素组件至少需要一种定位方式")
    if component_type in {"ClickDesktopElement", "InputDesktopText", "ReadDesktopText"}:
        if not isinstance(locator, dict) or not locator.get("window") or not any(
            locator.get(key) for key in ("name", "automation_id", "class_name", "relative_point", "template_path")
        ):
            raise ComponentValidationError("桌面元素组件缺少窗口或控件定位信息")
    result = {
        "component_id": component_id,
        "component_type": component_type,
        "name": name,
        "application": definition.get("application", "Edge"),
        "locator": locator,
        "config": definition.get("config") or {},
        "before_assertions": list(definition.get("before_assertions") or []),
        "after_assertions": list(definition.get("after_assertions") or []),
        "timeout_seconds": int(definition.get("timeout_seconds", 15)),
        "retry_times": int(definition.get("retry_times", 1)),
        "on_failure": definition.get("on_failure", "MANUAL_REVIEW"),
        "enabled": bool(definition.get("enabled", True)),
        "version": int(definition.get("version", 1)),
        "write_capable": CATALOG_BY_TYPE[component_type].write_capable,
    }
    if result["timeout_seconds"] < 1 or result["timeout_seconds"] > 300:
        raise ComponentValidationError("组件超时时间必须在1到300秒之间")
    if result["retry_times"] < 0 or result["retry_times"] > 5:
        raise ComponentValidationError("组件重试次数必须在0到5之间")
    return result


def validate_workflow(definition: dict[str, Any]) -> dict[str, Any]:
    workflow_id = str(definition.get("workflow_id", "")).strip() or (
        f"WF-{uuid.uuid4().hex[:8].upper()}"
    )
    name = str(definition.get("name", "")).strip()
    if not name:
        raise ComponentValidationError("流程名称不能为空")
    nodes = list(definition.get("nodes") or [])
    if not nodes:
        raise ComponentValidationError("流程至少需要一个组件节点")
    node_ids = [str(node.get("node_id", "")) for node in nodes]
    if any(not node_id for node_id in node_ids) or len(node_ids) != len(set(node_ids)):
        raise ComponentValidationError("流程节点ID不能为空或重复")
    return {
        "workflow_id": workflow_id,
        "name": name,
        "description": str(definition.get("description", "")),
        "nodes": nodes,
        "edges": list(definition.get("edges") or []),
        "enabled": bool(definition.get("enabled", False)),
        "version": int(definition.get("version", 1)),
    }
