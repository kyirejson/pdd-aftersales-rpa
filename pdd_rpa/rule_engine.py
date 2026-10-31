from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .models import RuleDecision


class RuleError(ValueError):
    pass


def _resolve(facts: dict[str, Any], field: str) -> Any:
    value: Any = facts
    for part in field.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "eq":
        return actual == expected
    if operator == "neq":
        return actual != expected
    if operator == "in":
        return actual in expected
    if operator == "not_in":
        return actual not in expected
    if operator == "contains":
        return str(expected) in str(actual or "")
    if operator == "not_contains":
        return str(expected) not in str(actual or "")
    if operator == "is_empty":
        return actual in (None, "", [], {})
    if operator == "not_empty":
        return actual not in (None, "", [], {})
    if operator in {"lt", "lte", "gt", "gte"}:
        try:
            left, right = float(actual), float(expected)
        except (TypeError, ValueError) as exc:
            raise RuleError(f"字段无法进行数值比较：{actual!r}") from exc
        return {
            "lt": left < right,
            "lte": left <= right,
            "gt": left > right,
            "gte": left >= right,
        }[operator]
    raise RuleError(f"不支持的规则操作符：{operator}")


def condition_matches(facts: dict[str, Any], condition: dict[str, Any]) -> bool:
    return _compare(
        _resolve(facts, condition["field"]),
        condition.get("operator", "eq"),
        condition.get("value"),
    )


def rule_matches(facts: dict[str, Any], rule: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for condition in rule.get("all_conditions", []):
        if not condition_matches(facts, condition):
            reasons.append(f"未满足：{condition['field']} {condition.get('operator', 'eq')}")
            return False, reasons

    any_conditions = rule.get("any_conditions", [])
    if any_conditions and not any(condition_matches(facts, item) for item in any_conditions):
        return False, ["未满足任一候选条件"]

    for condition in rule.get("exclude_conditions", []):
        if condition_matches(facts, condition):
            return False, [f"命中排除条件：{condition['field']}"]
    return True, ["全部条件满足"]


def evaluate_rules(facts: dict[str, Any], rules: Iterable[dict[str, Any]]) -> RuleDecision:
    candidates = sorted(
        (rule for rule in rules if rule.get("enabled", False)),
        key=lambda item: int(item.get("priority", 0)),
        reverse=True,
    )
    checked: list[str] = []
    for rule in candidates:
        shops = rule.get("shops", ["*"])
        if "*" not in shops and facts.get("shop_id") not in shops:
            continue
        matched, reasons = rule_matches(facts, rule)
        if matched:
            return RuleDecision(
                matched=True,
                rule_id=rule["rule_id"],
                rule_name=rule.get("name", rule["rule_id"]),
                actions=list(rule.get("actions", [])),
                approval_required=bool(rule.get("approval_required", True)),
                reasons=reasons,
            )
        checked.extend(f"{rule.get('rule_id')}: {reason}" for reason in reasons)
    return RuleDecision(matched=False, reasons=checked or ["没有已启用的规则"])

