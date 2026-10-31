from __future__ import annotations

import re
from copy import deepcopy
from enum import StrEnum
from typing import Any


SETTING_KEY = "in_transit_routing_config"


class RouteType(StrEnum):
    DIRECT_CARRIER = "DIRECT_CARRIER"
    JST_SUPPLIER = "JST_SUPPLIER"
    WDT_DROPSHIP = "WDT_DROPSHIP"
    WDT_PRODUCT_GROUP = "WDT_PRODUCT_GROUP"


class RoutingConfigError(ValueError):
    pass


class RoutingConfigConflict(RuntimeError):
    pass


DEFAULT_CONFIG: dict[str, Any] = {
    # ``schema_version`` controls startup migrations. ``version`` is the
    # operator-facing revision and increases after every successful save.
    "schema_version": 13,
    "version": 13,
    "classification_rules": [
        {"rule_id": "DIRECT-PDD", "route_type": "DIRECT_CARRIER", "match_mode": "regex", "pattern": r"[-－—一]拼多多\d*$", "priority": 300, "enabled": True},
        {"rule_id": "DIRECT-TMALL", "route_type": "DIRECT_CARRIER", "match_mode": "regex", "pattern": r"[-－—一]天猫\d*$", "priority": 290, "enabled": True},
        {"rule_id": "JST-FOUR-WAREHOUSE", "route_type": "JST_SUPPLIER", "match_mode": "contains", "pattern": "示例供应链发货", "priority": 200, "enabled": True},
        {"rule_id": "WDT-DROPSHIP", "route_type": "WDT_DROPSHIP", "match_mode": "regex", "pattern": r"代发$", "priority": 100, "enabled": True},
    ],
    "carrier_groups": [
        {
            "carrier": "圆通快递",
            "aliases": ["圆通", "圆通速递"],
            "platform": "yto",
            "account_alias": "圆通专属客服",
            # The site entry is named “专属客服”, but this is the actual chat
            # header observed after opening it.  It is the value that the
            # fixed-component OCR must verify before any message can be sent.
            "group_name": "示例客户服务群",
            "target_url": "https://kh.yto.net.cn/new/home",
            "message_mode": "service_chat",
            "enabled": True,
        },
        {
            "carrier": "中通快递",
            "aliases": ["中通", "中通速递"],
            "platform": "wecom",
            "account_alias": "企业微信",
            "group_name": "【快递】示例团队&中通对接群",
            "message_mode": "group_chat",
            "enabled": True,
        },
        {
            "carrier": "顺丰快递",
            "aliases": ["顺丰", "顺丰速运"],
            "platform": "wecom",
            "account_alias": "企业微信",
            "group_name": "示例顺丰服务群",
            "message_mode": "group_chat",
            "enabled": True,
        },
        {
            "carrier": "韵达快递",
            "aliases": ["韵达", "韵达速递"],
            "platform": "wechat",
            "account_alias": "微信",
            "group_name": "示例韵达沟通群",
            "message_mode": "group_chat",
            "enabled": True,
        },
    ],
    "supplier_groups": [],
    "jst_product_warehouse": "示例供应链代发总仓",
    "product_groups": [
        {
            "keyword": "示例品类 A",
            "warehouse": "示例供应链代发总仓",
            "platform": "wecom",
            "account_alias": "企业微信",
            "group_name": "示例供应商 A",
            "message_mode": "group_chat",
            "enabled": True,
        },
        {
            "keyword": "示例品类 B",
            "warehouse": "示例供应链代发总仓",
            "platform": "wecom",
            "account_alias": "企业微信",
            "group_name": "示例供应商 B",
            "message_mode": "group_chat",
            "enabled": True,
        },
        {
            "keyword": "示例品类 C",
            "warehouse": "示例供应链代发总仓",
            "platform": "wecom",
            "account_alias": "企业微信",
            "group_name": "示例供应商 C",
            "message_mode": "group_chat",
            "enabled": True,
        },
        {
            "keyword": "示例品类 D",
            "warehouse": "示例供应链代发总仓",
            "platform": "wecom",
            "account_alias": "企业微信",
            "group_name": "示例供应商 D",
            "message_mode": "group_chat",
            "enabled": True,
        },
        {
            "keyword": "示例品类 E",
            "warehouse": "示例供应链代发总仓",
            "platform": "wecom",
            "account_alias": "企业微信",
            "group_name": "【分销】示例供应商 E 沟通群",
            "message_mode": "group_chat",
            "enabled": True,
        },
        {
            "keyword": "示例品类 F",
            "warehouse": "示例供应链代发总仓",
            "platform": "qq",
            "account_alias": "QQ",
            "group_name": "示例供应商 F 分销群",
            "message_mode": "group_chat",
            "enabled": True,
        },
        {
            "keyword": "示例品类 G",
            "warehouse": "示例供应链代发总仓",
            "platform": "wecom",
            "account_alias": "企业微信",
            "group_name": "【分销】示例供应商 G 对接群",
            "message_mode": "group_chat",
            "enabled": True,
        },
        {
            "keyword": "示例品类 H",
            "warehouse": "示例供应链代发总仓",
            "platform": "wecom",
            "account_alias": "企业微信",
            "group_name": "分销1-示例团队&示例供应商 H 对接群",
            "message_mode": "group_chat",
            "enabled": True,
        },
    ],
    # 示例自营仓按快递公司路由；示例供应链代发总仓按货品名称路由。
    "supported_warehouses": ["示例自营仓", "示例供应链代发总仓"],
    "latest_logistics_phrase": "快件已发往",
    "location_whitelists": [
        {
            "carrier": "*",
            "aliases": ["圆通", "中通", "顺丰", "韵达", "圆通快递", "中通快递", "顺丰快递", "韵达快递"],
            "keywords": ["示例城市甲市", "示例城市乙市"],
            "enabled": True,
        }
    ],
    "fixed_reject": {
        "option_text": "消费者撤销售后申请",
        "explanation_template": "已联系拦截，当前物流状态不满足退款条件",
    },
    "message_template": "{tracking_no}+\u62e6\u622a",
    "supported_platforms": ["yto", "wecom", "wechat", "dingtalk", "qq"],
    "refund_reason_option_text": "主观原因不想要",
}


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(DEFAULT_CONFIG)
    result.update(deepcopy(dict(config or {})))
    # Migrate the earlier default wording to the explicitly configured
    # message format: tracking number + “拦截”.
    if result.get("message_template") == "{tracking_no}+\u9700\u8981\u62e6\u622a":
        result["message_template"] = DEFAULT_CONFIG["message_template"]
    route_values = {item.value for item in RouteType}
    platforms = {
        str(item or "").strip().lower()
        for item in (result.get("supported_platforms") or [])
        if str(item or "").strip()
    }
    result["supported_platforms"] = sorted(platforms)
    supported_warehouses = [
        re.sub(r"\s+", "", str(item or ""))
        for item in (result.get("supported_warehouses") or [])
    ]
    supported_warehouses = list(dict.fromkeys(item for item in supported_warehouses if item))
    legacy_product_warehouse = re.sub(
        r"\s+",
        "",
        str(result.get("jst_product_warehouse") or DEFAULT_CONFIG["jst_product_warehouse"]),
    )
    result["jst_product_warehouse"] = legacy_product_warehouse
    seen_rules: set[str] = set()
    seen_product_routes: set[tuple[str, str]] = set()

    for rule in result.get("classification_rules") or []:
        rule_id = str(rule.get("rule_id", "")).strip()
        if not rule_id or rule_id in seen_rules:
            raise RoutingConfigError("分类规则 ID 不能为空或重复")
        seen_rules.add(rule_id)
        if rule.get("route_type") not in route_values:
            raise RoutingConfigError(f"未知发货类型：{rule.get('route_type')}")
        if rule.get("match_mode") not in {"contains", "regex", "exact"}:
            raise RoutingConfigError(f"未知匹配方式：{rule.get('match_mode')}")
        pattern = str(rule.get("pattern", ""))
        if not pattern:
            raise RoutingConfigError(f"规则 {rule_id} 缺少匹配内容")
        if rule.get("match_mode") == "regex":
            try:
                re.compile(pattern)
            except re.error as exc:
                raise RoutingConfigError(f"规则 {rule_id} 正则无效：{exc}") from exc

    for collection in ("carrier_groups", "supplier_groups", "product_groups"):
        for mapping in result.get(collection) or []:
            if not isinstance(mapping, dict):
                raise RoutingConfigError(f"{collection} 中的路由必须是对象")
            platform = str(mapping.get("platform") or "").strip().lower()
            if platform not in platforms:
                raise RoutingConfigError(f"群平台必须属于 {sorted(platforms)}")
            group_name = str(mapping.get("group_name", "")).strip()
            account_alias = str(mapping.get("account_alias", "")).strip()
            if not group_name:
                raise RoutingConfigError("群名称不能为空")
            if not account_alias:
                raise RoutingConfigError("发送账号不能为空")
            mapping["platform"] = platform
            mapping["group_name"] = group_name
            mapping["account_alias"] = account_alias
            if collection == "product_groups":
                keyword = str(mapping.get("keyword", "")).strip()
                if not keyword:
                    raise RoutingConfigError("货品关键词不能为空")
                warehouse = re.sub(
                    r"\s+",
                    "",
                    str(mapping.get("warehouse") or legacy_product_warehouse),
                )
                if not warehouse:
                    raise RoutingConfigError("货品路由仓库不能为空")
                # Version 12 and earlier stored one global product warehouse.
                # Materialise it on every mapping so the form can edit routes
                # independently without invalidating existing deployments.
                mapping["keyword"] = keyword
                mapping["warehouse"] = warehouse
                route_key = (warehouse, keyword)
                if route_key in seen_product_routes:
                    raise RoutingConfigError(
                        f"仓库“{warehouse}”的货品关键词“{keyword}”重复，请保留一条"
                    )
                seen_product_routes.add(route_key)
                if warehouse not in supported_warehouses:
                    supported_warehouses.append(warehouse)

    if not supported_warehouses:
        raise RoutingConfigError("在途自动化仓库白名单不能为空")
    result["supported_warehouses"] = supported_warehouses

    reject = result.get("fixed_reject") or {}
    if not isinstance(reject, dict):
        raise RoutingConfigError("固定拒绝配置格式错误")
    result["schema_version"] = max(
        int(DEFAULT_CONFIG["schema_version"]),
        int(result.get("schema_version") or 1),
    )
    result["version"] = int(result.get("version", 1))
    return result


def _operational_latest_logistics_text(latest_trace: str) -> str:
    """Keep only the green latest-trace content, excluding service annotations."""
    return re.split(
        r"[（(](?:如遇(?:物流)?问题|客服热线)",
        str(latest_trace or ""),
        maxsplit=1,
    )[0].strip()


def latest_logistics_mentions_wuxi(latest_trace: str) -> bool:
    """A 示例自营仓 order can refund when its latest green trace contains 无锡."""
    return "无锡" in _operational_latest_logistics_text(latest_trace)


def _matches(value: str, rule: dict[str, Any]) -> bool:
    pattern = str(rule["pattern"])
    mode = rule["match_mode"]
    if mode == "exact":
        return value == pattern
    if mode == "contains":
        return pattern in value
    return re.search(pattern, value) is not None


def classify_logistics_company(value: str, config: dict[str, Any]) -> dict[str, Any]:
    normalized = str(value or "").strip()
    if not normalized:
        return {"ok": False, "manual_review": True, "reason": "旺店通物流公司为空"}
    matches = [
        rule
        for rule in config.get("classification_rules", [])
        if rule.get("enabled", True) and _matches(normalized, rule)
    ]
    if not matches:
        return {"ok": False, "manual_review": True, "reason": f"物流公司未匹配任何发货类型：{normalized}"}
    matches.sort(key=lambda item: int(item.get("priority", 0)), reverse=True)
    top_priority = int(matches[0].get("priority", 0))
    top = [item for item in matches if int(item.get("priority", 0)) == top_priority]
    if len({item["route_type"] for item in top}) != 1:
        return {"ok": False, "manual_review": True, "reason": f"同优先级规则冲突：{[item['rule_id'] for item in top]}"}
    return {
        "ok": True,
        "manual_review": False,
        "route_type": top[0]["route_type"],
        "rule_id": top[0]["rule_id"],
        "logistics_company": normalized,
    }


def _alias_match(value: str, mapping: dict[str, Any], primary_key: str) -> bool:
    names = [
        str(mapping.get(primary_key, "")).strip(),
        *[str(item).strip() for item in mapping.get("aliases", [])],
    ]
    return value in {name for name in names if name}


def _unique_mapping(items: list[dict[str, Any]], predicate, label: str) -> tuple[dict[str, Any] | None, str]:
    matches = [item for item in items if item.get("enabled", True) and predicate(item)]
    if len(matches) != 1:
        return None, f"{label}映射数量应为1，实际为{len(matches)}"
    return matches[0], ""


def _extract_forwarded_destination(latest_trace: str, required_phrase: str) -> str:
    """Extract the destination immediately after the latest forwarding phrase.

    The full PDD logistics sentence can contain an earlier city, a customer
    service phone number, or a repeated help message.  Only the text after the
    last ``快件已发往`` marker and before the next sentence delimiter is
    eligible for the refund-location rule.
    """
    text = str(latest_trace or "").strip()
    phrase = str(required_phrase or "").strip()
    if not text or not phrase:
        return ""
    occurrences = list(re.finditer(re.escape(phrase), text))
    if not occurrences:
        return ""
    destination = text[occurrences[-1].end():]
    destination = re.sub(r"^[\s:：\-—>]+", "", destination)
    destination = re.split(r"[\r\n（(，,。；;、|]", destination, maxsplit=1)[0]
    return destination.strip()


def _extract_bracket_location(latest_trace: str, required_phrase: str = "") -> str:
    """Extract the PDD city label belonging to the forwarding update.

    A logistics card can contain both ``【示例城市 A】快件已发往 ...`` and a later
    support annotation such as ``【客服热线：95311】``.  The latter is not a
    location.  When a forwarding phrase is supplied, only a bracket directly
    before that phrase is eligible for the refund decision.
    """
    text = str(latest_trace or "")
    phrase = str(required_phrase or "").strip()
    if phrase:
        pattern = rf"【\s*([^】]+?)\s*】\s*{re.escape(phrase)}"
        matches = list(re.finditer(pattern, text))
        if matches:
            return matches[-1].group(1).strip()
        # Some carrier cards say “已离开” instead of “快件已发往”.  In that
        # layout retain only city-shaped brackets; a later 【客服热线：…】 must
        # never supersede a real 【示例城市 A】/【示例城市 B】 location.
        city_labels = [
            label.strip()
            for label in re.findall(r"【\s*([^】]+?)\s*】", text)
            if label.strip().endswith("市")
        ]
        return city_labels[-1] if city_labels else ""
    # Without an explicit forwarding phrase, retain the latest city-shaped
    # label in the card. Later brackets that are a date or a support phone
    # marker are excluded because they are not city-shaped.
    city_labels = [
        label.strip()
        for label in re.findall(r"【\s*([^】]+?)\s*】", text)
        if label.strip().endswith("市")
    ]
    return city_labels[-1] if city_labels else ""


def _normalize_wdt_warehouse(
    value: Any,
    supported_warehouses: list[str],
    *,
    expected_order_no: str = "",
) -> dict[str, Any]:
    """Resolve one warehouse without treating arbitrary row text as a cell.

    WDT's custom-painted grid sometimes copies the entire selected row even
    when the warehouse coordinate was clicked.  Recovery is allowed only when
    that row contains the exact expected order number and exactly one configured
    warehouse.  This keeps the repair narrow: an unknown warehouse containing a
    familiar substring, or a row mentioning multiple warehouses, still stops.
    """

    raw = re.sub(r"\s+", "", str(value or ""))
    supported = list(
        dict.fromkeys(
            re.sub(r"\s+", "", str(item or ""))
            for item in supported_warehouses
            if re.sub(r"\s+", "", str(item or ""))
        )
    )
    if not raw:
        return {
            "ok": False,
            "reason_code": "WDT_WAREHOUSE_UNREADABLE",
            "reason": "旺店通仓库未读取到有效内容",
            "raw": "",
            "warehouse": "",
            "matches": [],
        }
    if raw in supported:
        return {
            "ok": True,
            "raw": raw,
            "warehouse": raw,
            "matches": [raw],
            "source": "exact_cell",
        }

    matches = [warehouse for warehouse in supported if warehouse in raw]
    if len(matches) > 1:
        return {
            "ok": False,
            "reason_code": "WDT_WAREHOUSE_AMBIGUOUS",
            "reason": (
                "旺店通仓库读取结果同时包含多个白名单仓库，无法唯一确定："
                f"{', '.join(matches)}"
            ),
            "raw": raw,
            "warehouse": "",
            "matches": matches,
        }
    if len(matches) == 1:
        order_no = re.sub(r"\s+", "", str(expected_order_no or ""))
        if order_no and order_no in raw:
            return {
                "ok": True,
                "raw": raw,
                "warehouse": matches[0],
                "matches": matches,
                "source": "verified_full_row_recovery",
                "verified_order_no": order_no,
            }
        return {
            "ok": False,
            "reason_code": "WDT_WAREHOUSE_CELL_NOT_ISOLATED",
            "reason": (
                "旺店通仓库坐标复制到了整行，虽包含"
                f"“{matches[0]}”，但未同时回读到当前订单号，禁止模糊采用"
            ),
            "raw": raw,
            "warehouse": "",
            "matches": matches,
        }
    return {
        "ok": False,
        "reason_code": "UNSUPPORTED_WDT_WAREHOUSE",
        "reason": (
            f"旺店通仓库“{raw}”不在在途自动化白名单："
            f"{', '.join(supported)}"
        ),
        "raw": raw,
        "warehouse": raw,
        "matches": [],
    }


def decide_interception(inputs: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    config = validate_config(config)
    supported_warehouses = list(config["supported_warehouses"])
    warehouse_read = _normalize_wdt_warehouse(
        inputs.get("wdt_warehouse", ""),
        supported_warehouses,
        expected_order_no=str(inputs.get("order_no") or ""),
    )
    if not warehouse_read.get("ok"):
        return {
            "ok": False,
            "manual_review": True,
            "reason_code": warehouse_read["reason_code"],
            "reason": warehouse_read["reason"],
            "wdt_warehouse": str(warehouse_read.get("warehouse") or ""),
            "wdt_warehouse_raw": str(warehouse_read.get("raw") or ""),
            "warehouse_matches": list(warehouse_read.get("matches") or []),
            "supported_warehouses": supported_warehouses,
        }
    warehouse = str(warehouse_read["warehouse"])
    warehouse_metadata = {
        "wdt_warehouse": warehouse,
        "wdt_warehouse_raw": str(warehouse_read.get("raw") or warehouse),
        "wdt_warehouse_read_source": str(
            warehouse_read.get("source") or "exact_cell"
        ),
    }
    tracking_no = str(inputs.get("tracking_no", "")).strip()
    carrier = str(inputs.get("carrier", "")).strip()
    if not tracking_no:
        return {"ok": False, "manual_review": True, "reason": "快递单号为空"}

    legacy_product_warehouse = re.sub(
        r"\s+", "", str(config.get("jst_product_warehouse") or "示例代发总仓")
    )
    product_groups = list(config.get("product_groups") or [])
    product_route_warehouses = {legacy_product_warehouse}
    product_route_warehouses.update(
        re.sub(
            r"\s+",
            "",
            str(mapping.get("warehouse") or legacy_product_warehouse),
        )
        for mapping in product_groups
        if isinstance(mapping, dict)
    )
    product_route_warehouses.discard("")
    if warehouse in product_route_warehouses:
        raw_names = inputs.get("wdt_product_names") or []
        if isinstance(raw_names, str):
            product_names = [item.strip() for item in raw_names.splitlines() if item.strip()]
        else:
            product_names = [str(item).strip() for item in raw_names if str(item).strip()]
        product_names = list(dict.fromkeys(product_names))
        matched = [
            mapping
            for mapping in product_groups
            if mapping.get("enabled", True)
            and re.sub(
                r"\s+",
                "",
                str(mapping.get("warehouse") or legacy_product_warehouse),
            )
            == warehouse
            and str(mapping.get("keyword") or "").strip()
            and any(str(mapping["keyword"]).strip() in name for name in product_names)
        ]
        if not matched:
            return {
                "ok": False,
                "manual_review": True,
                "reason_code": "WDT_PRODUCT_NAME_UNMAPPED",
                "reason": f"{warehouse}货品名称未匹配拦截群：{product_names or '未读取'}",
                "wdt_warehouse": warehouse,
                "wdt_product_names": product_names,
            }
        # A WDT order can contain several goods while one after-sale record
        # covers only part of them.  Prefer a supplier keyword present in
        # every returned goods name; e.g. “示例品类 D” is the common supplier
        # for an order containing both “示例品类 E” and “博物课堂”.  A keyword
        # present in only one line must not override that common route.
        common_matched = [
            mapping
            for mapping in matched
            if all(
                str(mapping.get("keyword") or "").strip() in name
                for name in product_names
            )
        ]
        candidates = common_matched or matched
        targets = {
            (
                str(item.get("platform") or ""),
                str(item.get("account_alias") or ""),
                str(item.get("group_name") or ""),
                str(item.get("message_mode") or ""),
            )
            for item in candidates
        }
        if len(targets) != 1:
            return {
                "ok": False,
                "manual_review": True,
                "reason_code": "WDT_PRODUCT_NAME_AMBIGUOUS",
                "reason": f"同一订单货品命中多个拦截群：{sorted(targets)}",
                "wdt_warehouse": warehouse,
                "wdt_product_names": product_names,
            }
        target = candidates[0]
        message = str(config.get("message_template", "{tracking_no}+\u62e6\u622a")).format(
            tracking_no=tracking_no, carrier=carrier
        )
        return {
            "ok": True,
            "manual_review": False,
            **warehouse_metadata,
            "route_type": RouteType.WDT_PRODUCT_GROUP.value,
            "rule_id": "WDT-PRODUCT-KEYWORD",
            "wdt_product_names": product_names,
            "matched_product_keyword": str(target.get("keyword") or ""),
            "tracking_no": tracking_no,
            "carrier": carrier,
            "group": target,
            "message": message,
            "evidence_source": "WDT_GOODS_LIST",
            "matched_location": "",
            "location_phrase": "",
            "destination_text": "",
            # This warehouse branch only performs supplier interception; it
            # must not reuse 示例自营仓's latest-city agree-refund decision.
            "final_action": "REJECT_REFUND",
            "force_reject_after_interception": True,
            "fixed_reject": config.get("fixed_reject", {}),
        }

    classified = classify_logistics_company(str(inputs.get("wdt_logistics_company", "")), config)
    if not classified.get("ok"):
        return classified

    route = RouteType(classified["route_type"])

    target: dict[str, Any] | None = None
    final_action = "REJECT_REFUND"
    evidence_source = ""
    matched_location = ""
    location_phrase = ""
    destination_text = ""

    if route is RouteType.DIRECT_CARRIER:
        target, error = _unique_mapping(
            config.get("carrier_groups", []),
            lambda item: _alias_match(carrier, item, "carrier"),
            f"快递公司 {carrier} 的群",
        )
        if error:
            return {**classified, "ok": False, "manual_review": True, "reason": error}
        whitelist, error = _unique_mapping(
            config.get("location_whitelists", []),
            lambda item: _alias_match(carrier, item, "carrier"),
            f"快递公司 {carrier} 的地点白名单",
        )
        if error:
            return {**classified, "ok": False, "manual_review": True, "reason": error}
        latest_trace = str(inputs.get("latest_logistics_text", "")).strip()
        # For 示例自营仓, “无锡” anywhere in the green latest logistics update can
        # agree.  Other locations retain the configured bracket-city whitelist
        # and old-layout forwarding fallback.
        location_phrase = str(whitelist.get("required_phrase") or "").strip()
        bracket_location = _extract_bracket_location(latest_trace, location_phrase)
        # 示例自营仓的无锡规则以工单详情页绿色最新走件文字为准：
        # 只要该条真正的物流描述包含“无锡”即可退款，不再要求
        # 固定的【示例城市 A】括号格式或“快件已发往”措辞。
        if latest_logistics_mentions_wuxi(latest_trace):
            destination_text = _operational_latest_logistics_text(latest_trace)
            matched_location = "无锡"
        elif bracket_location:
            # When PDD gives a bracketed location, require an exact match to
            # the configured city names.  A later occurrence such as
            # “客服热线：无锡” must never override a non-whitelisted city.
            destination_text = bracket_location
            matched_location = next(
                (
                    str(keyword).strip()
                    for keyword in whitelist.get("keywords", [])
                    if str(keyword).strip() == bracket_location
                ),
                "",
            )
        else:
            # Keep compatibility with older detail layouts that omitted the
            # bracket label altogether.
            fallback_phrase = str(
                location_phrase
                or config.get("latest_logistics_phrase")
                or "快件已发往"
            ).strip()
            location_phrase = fallback_phrase
            destination_text = _extract_forwarded_destination(latest_trace, fallback_phrase)
            matched_location = next(
                (
                    str(keyword)
                    for keyword in whitelist.get("keywords", [])
                    if str(keyword) and str(keyword) in destination_text
                ),
                "",
            )
        final_action = "AGREE_REFUND" if matched_location else "REJECT_REFUND"
        evidence_source = "PDD_LATEST_LOGISTICS"
    elif route is RouteType.JST_SUPPLIER:
        supplier = str(inputs.get("jst_supplier", "")).strip()
        if not supplier:
            return {**classified, "ok": False, "manual_review": True, "reason": "聚水潭未读取到供应商"}
        target, error = _unique_mapping(
            config.get("supplier_groups", []),
            lambda item: _alias_match(supplier, item, "supplier"),
            f"供应商 {supplier} 的群",
        )
        if error:
            return {**classified, "ok": False, "manual_review": True, "reason": error}
        evidence_source = "JST_ORDER_SUPPLIER"
    else:
        supplier = str(inputs.get("wdt_log_company", "")).strip()
        if not supplier:
            return {**classified, "ok": False, "manual_review": True, "reason": "旺店通订单日志未读取到选仓公司"}
        target, error = _unique_mapping(
            config.get("supplier_groups", []),
            lambda item: _alias_match(supplier, item, "supplier"),
            f"代发公司 {supplier} 的群",
        )
        if error:
            return {**classified, "ok": False, "manual_review": True, "reason": error}
        evidence_source = "WDT_WAREHOUSE_LOG"

    message = str(config.get("message_template", "{tracking_no}+\u62e6\u622a")).format(
        tracking_no=tracking_no, carrier=carrier
    )
    return {
        "ok": True,
        "manual_review": False,
        **warehouse_metadata,
        **classified,
        "tracking_no": tracking_no,
        "carrier": carrier,
        "group": target,
        "message": message,
        "evidence_source": evidence_source,
        "matched_location": matched_location,
        "location_phrase": location_phrase,
        "destination_text": destination_text,
        "final_action": final_action,
        "fixed_reject": config.get("fixed_reject", {}),
    }
