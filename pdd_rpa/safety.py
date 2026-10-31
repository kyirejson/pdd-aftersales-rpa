from __future__ import annotations

from dataclasses import dataclass

from .models import ExecutionMode


MONEY_ACTIONS = {
    "AGREE_REFUND",
    "AGREE_RETURN_AFTER_REFUND",
    "AGREE_RECALL_AFTER_REFUND",
    "REJECT_REFUND",
}
EXTERNAL_WRITE_ACTIONS = MONEY_ACTIONS | {
    "SEND_INTERCEPT_REQUEST",
    "REMIND_CARRIER",
    "LOCK_ORDER",
}


class SafetyViolation(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SafetyContext:
    configured_shop_name: str
    page_shop_name: str
    task_shop_name: str
    configured_profile: str
    active_profile: str


def validate_shop_context(context: SafetyContext) -> None:
    if len({context.configured_shop_name, context.page_shop_name, context.task_shop_name}) != 1:
        raise SafetyViolation("店铺身份不一致，已阻止操作")
    if context.configured_profile != context.active_profile:
        raise SafetyViolation("Edge配置目录不一致，已阻止操作")


def assert_action_allowed(
    action: str,
    mode: ExecutionMode,
    live_actions_enabled: bool,
    approved: bool = False,
) -> None:
    if action not in EXTERNAL_WRITE_ACTIONS:
        return
    if not live_actions_enabled:
        raise SafetyViolation("真实外部操作总开关处于关闭状态")
    if mode is ExecutionMode.OBSERVE:
        raise SafetyViolation("observe模式禁止外部写操作")
    if mode is ExecutionMode.APPROVE and not approved:
        raise SafetyViolation("approve模式要求人工批准")
