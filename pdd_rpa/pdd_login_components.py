"""Captured, non-secret UIA definitions for PDD login recovery.

These definitions describe the safe clicks used by the runtime.  They contain
no account name, password, cookie, token, or verification data.  The runtime
still rechecks uniqueness against the live UIA tree before every click.
"""

from __future__ import annotations

from typing import Any


ACCOUNT_TAB_ID = "CMP-PDD-LOGIN-ACCOUNT-TAB"
LOGIN_BUTTON_ID = "CMP-PDD-LOGIN-SUBMIT"
POPUP_CLOSE_ID = "CMP-PDD-LOGIN-POPUP-CLOSE"
WORK_ORDER_URL_ID = "CMP-PDD-LOGIN-WORK-ORDER-URL"


def component_definitions() -> list[dict[str, Any]]:
    return [
        {
            "component_id": ACCOUNT_TAB_ID,
            "component_type": "ClickElement",
            "name": "点击账号登录",
            "application": "Edge",
            "locator": {
                "text": "账号登录",
                "name": "账号登录",
                "uia_control_type": "Text",
                "engine": "edge_uia",
                "require_unique": True,
                "require_visible": True,
            },
            "config": {"page_kind": "login", "action": "click"},
            "before_assertions": ["Edge窗口标题包含完整店铺名", "当前URL为拼多多登录页"],
            "after_assertions": ["账号登录表单可见"],
            "on_failure": "MANUAL_REVIEW",
        },
        {
            "component_id": LOGIN_BUTTON_ID,
            "component_type": "ClickElement",
            "name": "点击登录",
            "application": "Edge",
            "locator": {
                "text": "登录",
                "name": "登录",
                "uia_control_type": "Button",
                "class_name": "BTN_outerWrapper_5-155-0 BTN_danger_5-155-0 BTN_large_5-155-0 BTN_outerWrapperBtn_5-155-0",
                "engine": "edge_uia",
                "require_unique": True,
                "require_visible": True,
            },
            "config": {"page_kind": "login", "action": "click", "credentials_source": "user_profile_only"},
            "before_assertions": ["账号登录表单已显示", "不读取或写入凭据"],
            "after_assertions": ["URL离开登录页"],
            "on_failure": "MANUAL_REVIEW",
        },
        {
            "component_id": POPUP_CLOSE_ID,
            "component_type": "ClickElement",
            "name": "关闭登录后强制弹窗",
            "application": "Edge",
            "locator": {
                "class_name": "MDL_headerCloseIcon",
                "uia_control_type": "Image",
                "engine": "edge_uia",
                "require_unique": True,
                "require_visible": True,
            },
            "config": {
                "page_kind": "post_login_modal",
                "class_contains": "MDL_headerCloseIcon",
                "action": "click",
            },
            "before_assertions": ["弹窗容器唯一", "关闭控件唯一"],
            "after_assertions": ["弹窗关闭"],
            "on_failure": "MANUAL_REVIEW",
        },
        {
            "component_id": WORK_ORDER_URL_ID,
            "component_type": "GoToPage",
            "name": "进入工单管理页",
            "application": "Edge",
            "locator": {
                "url": "https://mms.pinduoduo.com/aftersales/work_order/list?msfrom=mms_sidenav",
                "engine": "edge_uia",
            },
            "config": {"allowlisted_host": "mms.pinduoduo.com", "allowlisted_path": "/aftersales/work_order/list"},
            "before_assertions": ["已完成登录", "强制弹窗已关闭"],
            "after_assertions": ["当前页面为工单管理列表", "店铺身份一致"],
            "on_failure": "MANUAL_REVIEW",
        },
    ]


def workflow_definition() -> dict[str, Any]:
    nodes = [
        {"node_id": "detect_login", "component_type": "Condition", "name": "检测是否需要登录", "config": {"url_kind": "login"}},
        {"node_id": "account_tab", "component_id": ACCOUNT_TAB_ID, "component_type": "ClickElement", "name": "点击账号登录"},
        {"node_id": "login", "component_id": LOGIN_BUTTON_ID, "component_type": "ClickElement", "name": "点击登录"},
        {"node_id": "close_popup", "component_id": POPUP_CLOSE_ID, "component_type": "ClickElement", "name": "关闭登录后强制弹窗"},
        {"node_id": "goto_list", "component_id": WORK_ORDER_URL_ID, "component_type": "GoToPage", "name": "进入工单管理页"},
        {"node_id": "assert_identity", "component_type": "AssertShopIdentity", "name": "校验店铺身份"},
    ]
    return {
        "workflow_id": "WF-PDD-LOGIN-RECOVERY",
        "name": "拼多多登录恢复（UIA）",
        "description": "同步前检测登录页；点击账号登录和登录，安全关闭唯一强制弹窗，再进入白名单工单 URL。运行时不读取、不填写凭据。",
        "enabled": False,
        "nodes": nodes,
        "edges": [
            {"from": "detect_login", "to": "account_tab"},
            {"from": "account_tab", "to": "login"},
            {"from": "login", "to": "close_popup"},
            {"from": "close_popup", "to": "goto_list"},
            {"from": "goto_list", "to": "assert_identity"},
        ],
    }

