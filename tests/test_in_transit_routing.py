from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from pdd_rpa.db import Repository
from pdd_rpa.group_dispatch import GroupDispatchService
from pdd_rpa.in_transit_routing import (
    DEFAULT_CONFIG,
    _extract_bracket_location,
    classify_logistics_company,
    decide_interception,
    latest_logistics_mentions_wuxi,
    validate_config,
)


def configured():
    config = deepcopy(DEFAULT_CONFIG)
    config["supported_warehouses"] = ["示例自营仓"]
    config["carrier_groups"] = [
        {"carrier": "中通", "aliases": ["中通快递"], "platform": "wecom", "account_alias": "拦截号", "group_name": "中通拦截群", "enabled": True}
    ]
    config["supplier_groups"] = [
        {"supplier": "甲供应商", "aliases": ["甲公司"], "platform": "qq", "account_alias": "拦截号", "group_name": "甲供应商群", "enabled": True}
    ]
    config["location_whitelists"] = [
        {"carrier": "中通", "aliases": ["中通快递"], "keywords": ["义乌转运中心", "金华市"]}
    ]
    config["fixed_reject"] = {"option_text": "物流仍在正常运输", "explanation_template": "已联系拦截"}
    return validate_config(config)


def test_classification_three_routes():
    config = configured()
    assert classify_logistics_company("中通-拼多多", config)["route_type"] == "DIRECT_CARRIER"
    assert classify_logistics_company("圆通(示例供应链发货)", config)["route_type"] == "JST_SUPPLIER"
    assert classify_logistics_company("韵达代发", config)["route_type"] == "WDT_DROPSHIP"


def test_direct_route_agrees_only_on_configured_location():
    base = {"wdt_warehouse": "示例自营仓", "wdt_logistics_company": "中通-拼多多", "carrier": "中通", "tracking_no": "ZT001"}
    agreed = decide_interception({**base, "latest_logistics_text": "快件已发往义乌转运中心（运输中）"}, configured())
    rejected = decide_interception({**base, "latest_logistics_text": "快件运输中"}, configured())
    assert agreed["final_action"] == "AGREE_REFUND"
    assert agreed["matched_location"] == "义乌转运中心"
    assert agreed["destination_text"] == "义乌转运中心"
    assert agreed["message"] == "ZT001+\u62e6\u622a"
    assert rejected["final_action"] == "REJECT_REFUND"
    assert rejected["fixed_reject"]["option_text"] == "物流仍在正常运输"


def test_direct_route_only_matches_location_after_forwarding_phrase():
    base = {"wdt_warehouse": "示例自营仓", "wdt_logistics_company": "中通-拼多多", "carrier": "中通", "tracking_no": "ZT001"}
    # A city before the marker or in a later help/phone sentence must not
    # override the destination after “快件已发往”.
    text = "快件已发往义乌转运中心（客服热线：无锡）"
    result = decide_interception({**base, "latest_logistics_text": text}, configured())
    assert result["final_action"] == "AGREE_REFUND"
    assert result["destination_text"] == "义乌转运中心"

    other = decide_interception(
        {**base, "latest_logistics_text": "快件已发往广州转运中心（客服热线：无锡）"},
        configured(),
    )
    assert other["final_action"] == "REJECT_REFUND"
    assert other["matched_location"] == ""
    assert other["destination_text"] == "广州转运中心"


def test_bracket_city_has_priority_and_requires_exact_city_name():
    base = {"wdt_warehouse": "示例自营仓", "wdt_logistics_company": "中通-拼多多", "carrier": "中通", "tracking_no": "ZT001"}
    suzhou = decide_interception(
        {**base, "latest_logistics_text": "【苏州市】已离开江苏省苏州市吴中区（物流更新）"},
        configured(),
    )
    # The test fixture deliberately has a custom whitelist; only its exact
    # bracket labels can agree.
    assert suzhou["final_action"] == "REJECT_REFUND"
    assert suzhou["matched_location"] == ""

    configured_default = validate_config(DEFAULT_CONFIG)
    agreed = decide_interception(
        {**base, "latest_logistics_text": "【示例城市甲市】已离开江苏省示例城市甲市示例区"},
        configured_default,
    )
    rejected = decide_interception(
        {**base, "latest_logistics_text": "【南京市】快件已发往南京转运中心"},
        configured_default,
    )
    assert agreed["final_action"] == "AGREE_REFUND"
    assert agreed["matched_location"] == "示例城市甲市"
    assert agreed["destination_text"] == "示例城市甲市"
    assert rejected["final_action"] == "REJECT_REFUND"
    assert rejected["matched_location"] == ""


def test_default_bracket_city_rule_does_not_require_forwarding_wording():
    config = validate_config(DEFAULT_CONFIG)
    base = {
        "wdt_warehouse": "示例自营仓",
        "wdt_logistics_company": "韵达一拼多多2",
        "carrier": "韵达快递",
        "tracking_no": "40000000000000",
    }
    agreed = decide_interception(
        {**base, "latest_logistics_text": "【示例城市甲市】已离开 江苏示例城市甲市分拨交付中心；发往 山东济南分拨交付中心"},
        config,
    )
    rejected = decide_interception(
        {**base, "latest_logistics_text": "【济南市】已离开 山东济南分拨交付中心；发往 下一站"},
        config,
    )
    assert agreed["final_action"] == "AGREE_REFUND"
    assert agreed["matched_location"] == "示例城市甲市"
    assert rejected["final_action"] == "REJECT_REFUND"


def test_bracket_location_extracts_latest_city_label():
    assert _extract_bracket_location("【天津市】上一站；【苏州市】已离开") == "苏州市"
    assert _extract_bracket_location("快件已发往广州转运中心") == ""


def test_bracket_location_uses_the_city_immediately_before_forwarding_phrase():
    text = "【无锡市】快件已发往 济南转运中心（如遇问题请致电【客服热线：95311】）"
    assert _extract_bracket_location(text, "快件已发往") == "无锡市"

    configured_default = validate_config(DEFAULT_CONFIG)
    result = decide_interception(
        {
            "wdt_warehouse": "示例自营仓",
            "wdt_logistics_company": "中通—拼多多",
            "carrier": "中通快递",
            "tracking_no": "ZT001",
            "latest_logistics_text": text,
        },
        configured_default,
    )
    assert result["matched_location"] == "无锡"
    assert result["final_action"] == "AGREE_REFUND"


def test_junfu_warehouse_allows_any_green_latest_trace_containing_wuxi():
    config = validate_config(DEFAULT_CONFIG)
    base = {
        "wdt_warehouse": "示例自营仓",
        "wdt_logistics_company": "韵达一拼多多2",
        "carrier": "韵达快递",
        "tracking_no": "40000000000000",
    }
    result = decide_interception(
        {**base, "latest_logistics_text": "快件已到达无锡分拨交付中心，派送中"},
        config,
    )

    assert result["ok"] is True
    assert result["final_action"] == "AGREE_REFUND"
    assert result["matched_location"] == "无锡"


def test_wuxi_in_customer_service_annotation_does_not_count_as_green_trace():
    trace = "快件已发往广州转运中心（客服热线：无锡）"
    assert latest_logistics_mentions_wuxi(trace) is False


def test_departed_wuxi_transfer_center_counts_as_wuxi_latest_trace():
    trace = "您的快件离开【无锡转运中心】，正在发往下一站"
    assert latest_logistics_mentions_wuxi(trace) is True


def test_non_junfu_warehouse_stops_before_carrier_routing():
    result = decide_interception(
        {
            "wdt_warehouse": "其他仓",
            "wdt_logistics_company": "韵达一拼多多2",
            "carrier": "韵达快递",
            "tracking_no": "40000000000000",
            "latest_logistics_text": "快件已到达无锡分拨交付中心",
        },
        validate_config(DEFAULT_CONFIG),
    )

    assert result["ok"] is False
    assert result["manual_review"] is True
    assert result["reason_code"] == "UNSUPPORTED_WDT_WAREHOUSE"


def test_jst_product_warehouse_routes_each_keyword_to_exact_wecom_group():
    config = validate_config(DEFAULT_CONFIG)
    cases = {
        "【F】26秋·示例品类 A·提优课课练·语文": "示例供应商 A",
        "26秋示例品类 B同步练习": "示例供应商 B",
        "示例品类 C同步训练": "示例供应商 C",
        "示例品类 D课时优化作业": "示例供应商 D",
        "示例品类 E同步练习": "【分销】示例供应商 E 沟通群",
        "2026秋示例品类 G同步练习": "【分销】示例供应商 G 对接群",
        "2026秋示例品类 H同步练习": "分销1-示例团队&示例供应商 H 对接群",
    }
    for product_name, expected_group in cases.items():
        result = decide_interception(
            {
                "wdt_warehouse": "示例供应链代发总仓",
                "wdt_logistics_company": "中通速递",
                "carrier": "中通快递",
                "tracking_no": "40000000000000",
                "wdt_product_names": [product_name],
            },
            config,
        )
        assert result["ok"] is True
        assert result["route_type"] == "WDT_PRODUCT_GROUP"
        assert result["group"]["platform"] == "wecom"
        assert result["group"]["group_name"] == expected_group
        assert result["message"] == "40000000000000+拦截"
        assert result["final_action"] == "REJECT_REFUND"
        assert result["force_reject_after_interception"] is True


def test_jst_qiwenxuan_product_routes_to_exact_wecom_group():
    result = decide_interception(
        {
            "wdt_warehouse": "示例供应链代发总仓",
            "wdt_logistics_company": "中通速递",
            "carrier": "中通快递",
            "tracking_no": "40000000000000",
            "wdt_product_names": ["2026秋示例品类 G同步练习"],
        },
        validate_config(DEFAULT_CONFIG),
    )

    assert result["ok"] is True
    assert result["route_type"] == "WDT_PRODUCT_GROUP"
    assert result["matched_product_keyword"] == "示例品类 G"
    assert result["group"]["platform"] == "wecom"
    assert result["group"]["account_alias"] == "企业微信"
    assert result["group"]["group_name"] == "【分销】示例供应商 G 对接群"
    assert result["message"] == "40000000000000+拦截"
    assert result["final_action"] == "REJECT_REFUND"


def test_jst_hanzhijian_product_routes_to_exact_wecom_group():
    result = decide_interception(
        {
            "wdt_warehouse": "示例供应链代发总仓",
            "wdt_logistics_company": "中通速递",
            "carrier": "中通快递",
            "tracking_no": "40000000000000",
            "wdt_product_names": ["2026秋示例品类 H同步练习"],
        },
        validate_config(DEFAULT_CONFIG),
    )

    assert result["ok"] is True
    assert result["route_type"] == "WDT_PRODUCT_GROUP"
    assert result["matched_product_keyword"] == "示例品类 H"
    assert result["group"]["platform"] == "wecom"
    assert result["group"]["account_alias"] == "企业微信"
    assert result["group"]["group_name"] == "分销1-示例团队&示例供应商 H 对接群"
    assert result["message"] == "40000000000000+拦截"
    assert result["final_action"] == "REJECT_REFUND"


def test_jst_tianxing_product_routes_to_exact_qq_group():
    result = decide_interception(
        {
            "wdt_warehouse": "示例供应链代发总仓",
            "wdt_logistics_company": "中通速递",
            "carrier": "中通快递",
            "tracking_no": "40000000000000",
            "wdt_product_names": ["2026秋示例品类 F教材全解"],
        },
        validate_config(DEFAULT_CONFIG),
    )

    assert result["ok"] is True
    assert result["route_type"] == "WDT_PRODUCT_GROUP"
    assert result["matched_product_keyword"] == "示例品类 F"
    assert result["group"]["platform"] == "qq"
    assert result["group"]["group_name"] == "示例供应商 F 分销群"
    assert result["message"] == "40000000000000+拦截"
    assert result["final_action"] == "REJECT_REFUND"


def test_jst_product_warehouse_unmapped_or_multi_group_fails_closed():
    config = validate_config(DEFAULT_CONFIG)
    base = {
        "wdt_warehouse": "示例供应链代发总仓",
        "tracking_no": "40000000000000",
    }
    unmapped = decide_interception(
        {**base, "wdt_product_names": ["未知教辅"]}, config
    )
    ambiguous = decide_interception(
        {**base, "wdt_product_names": ["示例品类 A", "示例品类 C同步训练"]}, config
    )
    assert unmapped["reason_code"] == "WDT_PRODUCT_NAME_UNMAPPED"
    assert ambiguous["reason_code"] == "WDT_PRODUCT_NAME_AMBIGUOUS"


def test_legacy_product_routes_gain_the_global_warehouse_without_data_loss():
    legacy = deepcopy(DEFAULT_CONFIG)
    legacy.pop("schema_version", None)
    legacy["version"] = 12
    for mapping in legacy["product_groups"]:
        mapping.pop("warehouse", None)

    migrated = validate_config(legacy)

    assert migrated["schema_version"] == 13
    assert all(
        mapping["warehouse"] == "示例供应链代发总仓"
        for mapping in migrated["product_groups"]
    )
    assert next(
        mapping for mapping in migrated["product_groups"]
        if mapping["keyword"] == "示例品类 G"
    )["group_name"] == "【分销】示例供应商 G 对接群"


def test_product_route_can_add_and_route_a_second_warehouse_automatically():
    config = deepcopy(DEFAULT_CONFIG)
    config["product_groups"].append(
        {
            "keyword": "跨仓测试",
            "warehouse": "测试 二仓",
            "platform": "wechat",
            "account_alias": "微信",
            "group_name": "测试二仓拦截群",
            "message_mode": "group_chat",
            "enabled": True,
        }
    )

    validated = validate_config(config)
    assert "测试二仓" in validated["supported_warehouses"]
    result = decide_interception(
        {
            "wdt_warehouse": "测试 二仓",
            "tracking_no": "TEST-WAYBILL-2",
            "wdt_product_names": ["2026秋跨仓测试同步练习"],
        },
        validated,
    )

    assert result["ok"] is True
    assert result["route_type"] == "WDT_PRODUCT_GROUP"
    assert result["group"]["platform"] == "wechat"
    assert result["group"]["group_name"] == "测试二仓拦截群"


def test_same_product_keyword_can_route_differently_by_warehouse():
    config = deepcopy(DEFAULT_CONFIG)
    config["product_groups"].append(
        {
            "keyword": "示例品类 G",
            "warehouse": "第二聚水潭仓",
            "platform": "wechat",
            "account_alias": "微信",
            "group_name": "第二仓示例品类 G群",
            "message_mode": "group_chat",
            "enabled": True,
        }
    )
    config = validate_config(config)

    first = decide_interception(
        {
            "wdt_warehouse": "示例供应链代发总仓",
            "tracking_no": "WAYBILL-A",
            "wdt_product_names": ["示例品类 G同步练习"],
        },
        config,
    )
    second = decide_interception(
        {
            "wdt_warehouse": "第二聚水潭仓",
            "tracking_no": "WAYBILL-B",
            "wdt_product_names": ["示例品类 G同步练习"],
        },
        config,
    )

    assert first["group"]["group_name"] == "【分销】示例供应商 G 对接群"
    assert second["group"]["group_name"] == "第二仓示例品类 G群"


def test_multi_goods_prefers_keyword_common_to_every_goods_name():
    result = decide_interception(
        {
            "order_no": "5127709933008138616",
            "wdt_warehouse": "示例供应链代发总仓",
            "tracking_no": "40000000000000",
            "wdt_product_names": [
                "【F】26秋.示例品类 D.示例品类 E.语文.6年级上【小学】",
                "【预售9.10】【F】26秋.示例品类 D.博物课堂.6年级上【小学】",
            ],
        },
        DEFAULT_CONFIG,
    )

    assert result["ok"] is True
    assert result["matched_product_keyword"] == "示例品类 D"
    assert result["group"]["group_name"] == "示例供应商 D"


def test_duplicate_keyword_in_the_same_warehouse_is_rejected():
    config = deepcopy(DEFAULT_CONFIG)
    config["product_groups"].append(deepcopy(config["product_groups"][0]))

    try:
        validate_config(config)
    except Exception as exc:
        assert "货品关键词" in str(exc) and "重复" in str(exc)
    else:
        raise AssertionError("同仓库重复货品关键词应被拒绝")


def test_jst_and_dropship_resolve_supplier_and_reject():
    jst = decide_interception(
        {"wdt_warehouse": "示例自营仓", "wdt_logistics_company": "圆通(示例供应链发货)", "carrier": "圆通", "tracking_no": "YT1", "jst_supplier": "甲供应商"},
        configured(),
    )
    dropship = decide_interception(
        {"wdt_warehouse": "示例自营仓", "wdt_logistics_company": "韵达代发", "carrier": "韵达", "tracking_no": "YD1", "wdt_log_company": "甲公司"},
        configured(),
    )
    assert jst["group"]["group_name"] == "甲供应商群"
    assert dropship["group"]["group_name"] == "甲供应商群"
    assert jst["final_action"] == dropship["final_action"] == "REJECT_REFUND"


def test_missing_mapping_goes_to_manual_review():
    result = decide_interception(
        {"wdt_warehouse": "示例自营仓", "wdt_logistics_company": "申通-天猫", "carrier": "申通", "tracking_no": "ST1", "latest_logistics_text": "运输中"},
        configured(),
    )
    assert result["manual_review"] is True
    assert "映射数量应为1" in result["reason"]


def test_verified_full_wdt_row_recovers_one_supported_warehouse():
    order_no = "260000-034724619433070"
    raw_row = (
        f"{order_no}已完成7894312d9297708**666e33c168f9b97黄**"
        "辽宁省沈阳市铁西区开发北二路*号普洛斯物流园A*库*分区"
        "款到发货示例自营仓"
    )

    result = decide_interception(
        {
            "order_no": order_no,
            "wdt_warehouse": raw_row,
            "wdt_logistics_company": "中通-拼多多",
            "carrier": "中通",
            "tracking_no": "40000000000000",
            "latest_logistics_text": "【无锡市】快件已发往盘锦转运中心",
        },
        configured(),
    )

    assert result["ok"] is True
    assert result["wdt_warehouse"] == "示例自营仓"
    assert result["wdt_warehouse_raw"] == raw_row
    assert result["wdt_warehouse_read_source"] == "verified_full_row_recovery"


def test_full_wdt_row_does_not_recover_without_current_order_identity():
    result = decide_interception(
        {
            "order_no": "260000-034724619433070",
            "wdt_warehouse": "260000-014822452126703已完成款到发货示例自营仓",
            "wdt_logistics_company": "中通-拼多多",
            "carrier": "中通",
            "tracking_no": "ZT001",
        },
        configured(),
    )

    assert result["ok"] is False
    assert result["reason_code"] == "WDT_WAREHOUSE_CELL_NOT_ISOLATED"


def test_full_wdt_row_with_two_supported_warehouses_is_ambiguous():
    config = configured()
    config["supported_warehouses"] = ["示例自营仓", "示例供应链代发总仓"]
    result = decide_interception(
        {
            "order_no": "260000-034724619433070",
            "wdt_warehouse": (
                "260000-034724619433070已完成示例自营仓示例供应链代发总仓"
            ),
            "wdt_logistics_company": "中通-拼多多",
            "carrier": "中通",
            "tracking_no": "ZT001",
        },
        validate_config(config),
    )

    assert result["ok"] is False
    assert result["reason_code"] == "WDT_WAREHOUSE_AMBIGUOUS"


def test_group_dispatch_records_only_verified_and_deduplicates():
    database = Path("runtime") / f"test-dispatch-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    service = GroupDispatchService(repo)
    group = {"platform": "wecom", "account_alias": "拦截号", "group_name": "中通拦截群"}
    assert service.prepare(group, "ZT001", "ZT001+需要拦截")["duplicate"] is False
    assert service.record_dispatched(None, group, "ZT001", "ZT001+需要拦截", {"sent": True}) is True
    assert service.prepare(group, "ZT001", "ZT001+需要拦截")["duplicate"] is True
    assert service.record_dispatched(None, group, "ZT001", "ZT001+需要拦截", {"sent": True}) is False
    database.unlink(missing_ok=True)


def test_group_dispatch_records_same_waybill_for_distinct_tasks():
    database = Path("runtime") / f"test-dispatch-task-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    service = GroupDispatchService(repo)
    group = {"platform": "wecom", "account_alias": "企业微信", "group_name": "快递群"}

    assert service.record_dispatched(101, group, "ZT001", "ZT001+拦截", {"sent": True}) is True
    assert service.record_dispatched(102, group, "ZT001", "ZT001+拦截", {"sent": True}) is True
    assert service.record_dispatched(102, group, "ZT001", "ZT001+拦截", {"sent": True}) is False
    assert service.task_action_key(101, group, "ZT001").endswith(":TASK:101")
    assert service.task_action_key(102, group, "ZT001").endswith(":TASK:102")
    database.unlink(missing_ok=True)
