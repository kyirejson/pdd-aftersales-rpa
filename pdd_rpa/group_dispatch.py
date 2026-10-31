from __future__ import annotations

from hashlib import sha256
from typing import Any

from .db import Repository


class GroupDispatchService:
    """Safety and idempotency boundary around real desktop group-message connectors."""

    def __init__(self, repository: Repository):
        self.repository = repository

    @staticmethod
    def action_key(group: dict[str, Any], tracking_no: str) -> str:
        identity = "|".join(
            [
                str(group.get("platform", "")).strip().lower(),
                str(group.get("account_alias", "")).strip(),
                str(group.get("group_name", "")).strip(),
                str(tracking_no).strip(),
            ]
        )
        return f"INTERCEPT_DISPATCHED:{sha256(identity.encode('utf-8')).hexdigest()[:24]}"

    @classmethod
    def task_action_key(
        cls,
        task_id: int | None,
        group: dict[str, Any],
        tracking_no: str,
    ) -> str:
        """Return a per-ticket audit key while retaining route identity.

        One WDT shipment can legitimately back multiple PDD after-sale
        tickets. Those tickets require separate messages and screenshots;
        using only group + waybill made the second successful dispatch lose
        its action-log row. A task suffix keeps retries for the same ticket
        idempotent without collapsing distinct tickets.
        """
        base = cls.action_key(group, tracking_no)
        return f"{base}:TASK:{int(task_id)}" if task_id is not None else base

    def prepare(
        self,
        group: dict[str, Any],
        tracking_no: str,
        message: str,
        *,
        check_duplicate: bool = True,
    ) -> dict[str, Any]:
        """Validate a target before dispatching.

        Historical duplicate lookup is optional.  The production in-transit
        flow deliberately sets ``check_duplicate=False`` because the operator
        requested a fresh interception message for every verified ticket.
        The audit key is still retained so a later reconciliation can identify
        the exact group/account/tracking combination.
        """
        key = self.action_key(group, tracking_no)
        previous = self.repository.get_action_log(key) if check_duplicate else None
        return {
            "ok": True,
            "duplicate": previous is not None,
            "duplicate_check_performed": bool(check_duplicate),
            "action_key": key,
            "group": group,
            "tracking_no": tracking_no,
            "message": message,
            "previous": previous,
        }

    @staticmethod
    def _target_identity(group: dict[str, Any]) -> tuple[str, str, str]:
        """Return the exact desktop target fields used for safe replay checks."""

        return (
            str(group.get("platform") or "").strip(),
            str(group.get("account_alias") or "").strip(),
            str(group.get("group_name") or "").strip(),
        )

    def verified_task_dispatch(
        self,
        task_id: int,
        group: dict[str, Any],
        tracking_no: str,
        message: str,
    ) -> dict[str, Any] | None:
        """Return a complete, exact prior dispatch for this task, if one exists.

        A prior action-log row is the durable hand-off after the isolated desktop
        worker has proved its send.  Reusing it is safe only when every target
        field and the message payload still match and both the sent-message echo
        and screenshot evidence are present.  Any partial or conflicting row is
        treated as an unsafe state, never as permission to send again.
        """

        action_key = self.task_action_key(task_id, group, tracking_no)
        # Do not limit the lookup to the key derived from the *current* route.
        # A retry may have refreshed WDT/routing facts; looking up only the new
        # key would hide an earlier successful send and permit a second message.
        successful_dispatches = [
            item
            for item in self.repository.list_logs(task_id=task_id, limit=100_000)
            if item.get("action_type") == "INTERCEPT_MESSAGE_DISPATCHED"
            and item.get("status") == "success"
        ]
        if len(successful_dispatches) > 1:
            raise RuntimeError(
                "同一工单存在多条成功的拦截发送审计，无法唯一确认已发送目标，禁止继续"
            )
        if successful_dispatches:
            previous = successful_dispatches[0]
        else:
            # A malformed/non-success row under the exact deterministic key is
            # also an unsafe partial outbox state; validate it and fail closed
            # instead of attempting an external resend.
            previous = self.repository.get_action_log(action_key)
            if previous is None:
                return None

        details = dict(previous.get("details") or {})
        recorded_group = dict(details.get("group") or {})
        recorded_evidence = dict(details.get("evidence") or {})
        evidence_paths = [
            str(item).strip()
            for item in recorded_evidence.get("evidence") or []
            if str(item).strip()
        ]
        expected = {
            "task_id": int(task_id),
            "target": self._target_identity(group),
            "tracking_no": str(tracking_no or "").strip(),
            "message": str(message or ""),
        }
        actual = {
            "task_id": int(previous.get("task_id") or 0),
            "target": self._target_identity(recorded_group),
            "tracking_no": str(details.get("tracking_no") or "").strip(),
            "message": str(details.get("message") or ""),
        }
        errors: list[str] = []
        if previous.get("action_type") != "INTERCEPT_MESSAGE_DISPATCHED":
            errors.append("action_type")
        if previous.get("status") != "success":
            errors.append("status")
        if actual != expected:
            errors.append("task/target/tracking/message")
        if not str(recorded_evidence.get("sent_echo") or "").strip():
            errors.append("sent_echo")
        if not evidence_paths:
            errors.append("screenshots")
        if errors:
            raise RuntimeError(
                "同一工单已有拦截发送审计，但记录不完整或与当前路由冲突："
                + ", ".join(errors)
            )
        recorded_evidence["evidence"] = evidence_paths
        return {
            "action_key": str(previous.get("action_key") or action_key),
            "log": previous,
            "evidence": recorded_evidence,
        }

    def record_dispatched(
        self,
        task_id: int | None,
        group: dict[str, Any],
        tracking_no: str,
        message: str,
        evidence: dict[str, Any],
    ) -> bool:
        """Record a verified dispatch once per task/group/waybill."""
        return self.repository.log_action(
            task_id,
            self.task_action_key(task_id, group, tracking_no),
            "INTERCEPT_MESSAGE_DISPATCHED",
            "success",
            {
                "route_action_key": self.action_key(group, tracking_no),
                "group": group,
                "tracking_no": tracking_no,
                "message": message,
                "evidence": evidence,
            },
        )
