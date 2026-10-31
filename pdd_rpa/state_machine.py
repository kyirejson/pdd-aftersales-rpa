from __future__ import annotations

from .models import TaskState


TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.DISCOVERED: {TaskState.COLLECTING_DATA, TaskState.PAUSED, TaskState.FAILED},
    TaskState.COLLECTING_DATA: {
        TaskState.WAIT_RULE_MATCH,
        TaskState.DATA_CONFLICT,
        TaskState.NEED_REVIEW,
        TaskState.FAILED,
    },
    TaskState.WAIT_RULE_MATCH: {TaskState.READY, TaskState.NEED_REVIEW, TaskState.FAILED},
    TaskState.READY: {TaskState.WAIT_APPROVAL, TaskState.EXECUTING, TaskState.PAUSED},
    TaskState.NEED_REVIEW: {TaskState.WAIT_APPROVAL, TaskState.REJECTED, TaskState.PAUSED},
    TaskState.DATA_CONFLICT: {TaskState.NEED_REVIEW, TaskState.PAUSED},
    TaskState.WAIT_APPROVAL: {TaskState.EXECUTING, TaskState.REJECTED, TaskState.PAUSED},
    TaskState.EXECUTING: {TaskState.VERIFYING, TaskState.RETRY_PENDING, TaskState.FAILED},
    TaskState.VERIFYING: {TaskState.COMPLETED, TaskState.RETRY_PENDING, TaskState.NEED_REVIEW},
    TaskState.RETRY_PENDING: {TaskState.EXECUTING, TaskState.NEED_REVIEW, TaskState.FAILED},
    TaskState.PAUSED: {
        TaskState.COLLECTING_DATA,
        TaskState.WAIT_RULE_MATCH,
        TaskState.WAIT_APPROVAL,
        TaskState.NEED_REVIEW,
    },
    TaskState.COMPLETED: set(),
    TaskState.REJECTED: set(),
    TaskState.FAILED: {TaskState.RETRY_PENDING, TaskState.NEED_REVIEW},
}


class InvalidTransition(ValueError):
    pass


def assert_transition(current: str | TaskState, target: str | TaskState) -> None:
    current_state = TaskState(current)
    target_state = TaskState(target)
    if target_state not in TRANSITIONS[current_state]:
        raise InvalidTransition(f"非法状态变更：{current_state} -> {target_state}")

