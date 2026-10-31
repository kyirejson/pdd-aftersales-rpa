from pdd_rpa.in_transit_routing import DEFAULT_CONFIG
from pdd_rpa.qianniu_routing import decide_qianniu_interception


def test_junfu_uses_qianniu_detail_carrier_when_wdt_label_has_no_route_suffix():
    result = decide_qianniu_interception(
        {
            "order_no": "5127711625084043334",
            "wdt_logistics_company": "韵达一淘1",
            "wdt_warehouse": "示例自营仓",
            "carrier": "韵达快递",
            "tracking_no": "40000000000000",
            "latest_logistics_text": "包裹正在等待揽收",
            "wdt_product_names": [],
        },
        DEFAULT_CONFIG,
    )

    assert result["ok"] is True
    assert result["route_type"] == "QIANNIU_DIRECT_CARRIER"
    assert result["group"]["platform"] == "wechat"
    assert result["group"]["group_name"] == "示例韵达沟通群"
    assert result["message"] == "40000000000000+拦截"
    assert "final_action" not in result


def test_product_warehouse_keeps_existing_product_name_route():
    config = {
        **DEFAULT_CONFIG,
        "product_groups": [
            {
                "keyword": "示例品类 H",
                "warehouse": "示例供应链代发总仓",
                "platform": "wecom",
                "account_alias": "企业微信",
                "group_name": "分销1-示例团队&示例供应商 H 对接群",
                "message_mode": "group_chat",
                "enabled": True,
            }
        ],
    }
    result = decide_qianniu_interception(
        {
            "order_no": "5127711625084043334",
            "wdt_logistics_company": "未知标签",
            "wdt_warehouse": "示例供应链代发总仓",
            "carrier": "韵达快递",
            "tracking_no": "40000000000000",
            "wdt_product_names": ["示例品类 H同步练习"],
        },
        config,
    )

    assert result["ok"] is True
    assert result["group"]["group_name"] == "分销1-示例团队&示例供应商 H 对接群"
    assert "final_action" not in result
