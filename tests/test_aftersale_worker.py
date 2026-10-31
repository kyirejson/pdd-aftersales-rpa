from types import SimpleNamespace
from pathlib import Path
import json

from pdd_rpa import aftersale_worker, aftersale_worker_client
from pdd_rpa.aftersale_workbench import (
    AFTERSALE_SHIPPED_REFUND,
    AGREE_REFUND,
    AGREE_RETURN_AFTER_REFUND,
    EXTEND_RECEIPT,
    NEED_REVIEW,
    REJECT_REFUND,
    SKIP_REFUND_CLOSED,
)


class FakeCandidateSession:
    candidates = [False, True]
    last_instance = None

    def __init__(self, config):
        self.config = config
        self.last_search_candidate_count = len(self.candidates)
        self.closed = []
        self.open_calls = []
        type(self).last_instance = self

    def open_detail(
        self,
        shop,
        order_no,
        *,
        candidate_index=0,
        search_value=None,
        expected_aftersale_no="",
        require_shipped_refund_result=False,
    ):
        self.last_search_candidate_count = len(self.candidates)
        self.open_calls.append(
            {
                "order_no": order_no,
                "candidate_index": candidate_index,
                "search_value": search_value,
                "expected_aftersale_no": expected_aftersale_no,
                "require_shipped_refund_result": require_shipped_refund_result,
            }
        )
        return SimpleNamespace(handle=100 + candidate_index), f"detail:{candidate_index}"

    def read_detail(self, window, order_no, detail_url, shop):
        index = int(detail_url.rsplit(":", 1)[1])
        return {
            "order_no": order_no,
            "refund_type": "退货退款",
            "has_extend_receipt": self.candidates[index],
        }

    def screenshot(self, handle, path):
        return None

    def close_detail(self, window, shop):
        self.closed.append(window.handle)
        return {"closed": 1, "list_ready": True}


def test_collect_multiple_results_inspects_only_first_and_returns_manual_review(monkeypatch):
    monkeypatch.setattr(aftersale_worker, "PddAftersaleUiaSession", FakeCandidateSession)

    result = aftersale_worker._run(
        {
            "operation": "collect",
            "order_no": "ORDER-1",
            "shop": {"id": "shop-1"},
            "keep_detail_open": True,
        },
        Path("runtime/evidence"),
    )

    assert result["decision"]["action"] == NEED_REVIEW
    assert [item["has_extend_receipt"] for item in result["facts"]["aftersale_candidates_inspected"]] == [False]
    assert result["facts"]["aftersale_search_candidate_count"] == 2
    assert result["facts"]["aftersale_candidate_policy"] == "FIRST_RESULT_ONLY"
    assert len(result["evidence"]) == 1
    assert result["detail_retained"] is False
    assert result["detail_cleanup"] == {"closed": 1, "list_ready": True}
    assert FakeCandidateSession.last_instance.closed == [100]


def test_collect_multiple_results_retains_first_when_it_can_extend(monkeypatch):
    class FirstExtendSession(FakeCandidateSession):
        candidates = [True, False]

    monkeypatch.setattr(aftersale_worker, "PddAftersaleUiaSession", FirstExtendSession)

    result = aftersale_worker._run(
        {
            "operation": "collect",
            "order_no": "ORDER-2",
            "shop": {"id": "shop-1"},
            "keep_detail_open": True,
        },
        Path("runtime/evidence"),
    )

    assert result["decision"]["action"] == EXTEND_RECEIPT
    assert len(result["facts"]["aftersale_candidates_inspected"]) == 1
    assert result["facts"]["aftersale_search_candidate_count"] == 2
    assert result["detail_retained"] is True
    assert result["detail_cleanup"] is None
    assert FirstExtendSession.last_instance.closed == []


def test_collect_shipped_refund_reads_all_logistics_and_retains_at_three(monkeypatch):
    class ShippedRefundSession(FakeCandidateSession):
        candidates = [False]

        def __init__(self, config):
            super().__init__(config)
            self.events = []

        def read_detail(self, window, order_no, detail_url, shop):
            self.events.append("read_detail")
            return {
                "order_no": order_no,
                "aftersale_status": "待商家处理",
                "refund_type": "退款",
                "has_extend_receipt": False,
            }

        def read_logistics_detail(self, window, order_no, detail_url, shop):
            self.events.append("read_logistics_detail")
            return {
                "logistics_read_complete": True,
                "logistics_trajectory_count": 3,
                "logistics_expanded_segments": 1,
                "logistics_trace_rows": [
                    {"timestamp": "2026-08-25 02:05:26", "event": "运输中"}
                ],
                "latest_logistics_text": "运输中",
                "logistics_latest_signed": False,
            }

        def screenshot(self, handle, path):
            self.events.append(f"screenshot:{Path(path).name}")

    monkeypatch.setattr(
        aftersale_worker, "PddAftersaleUiaSession", ShippedRefundSession
    )

    result = aftersale_worker._run(
        {
            "operation": "collect",
            "order_no": "ORDER-SHIPPED-3",
            "facts": {
                "work_order_title": AFTERSALE_SHIPPED_REFUND,
                "pdd_order_status": "已发货",
            },
            "shop": {"id": "shop-1"},
            "keep_detail_open": True,
        },
        Path("runtime/evidence"),
    )

    assert result["decision"]["action"] == AGREE_REFUND
    assert result["facts"]["logistics_trajectory_count"] == 3
    assert result["facts"]["aftersale_candidate_policy"] == (
        "SHIPPED_REFUND_NON_CLOSED_SEARCH_RESULT"
    )
    assert ShippedRefundSession.last_instance.open_calls[0]["search_value"] == (
        "ORDER-SHIPPED-3"
    )
    assert ShippedRefundSession.last_instance.open_calls[0][
        "require_shipped_refund_result"
    ] is True
    assert result["detail_retained"] is True
    assert len(result["evidence"]) == 2
    assert result["evidence"][0].endswith("_candidate_1_actions_top.png")
    assert result["evidence"][1].endswith("_candidate_1_logistics_all.png")
    assert ShippedRefundSession.last_instance.events == [
        "read_detail",
        "screenshot:aftersale_shop-1_ORDER-SHIPPED-3_candidate_1_actions_top.png",
        "read_logistics_detail",
        "screenshot:aftersale_shop-1_ORDER-SHIPPED-3_candidate_1_logistics_all.png",
    ]
    assert ShippedRefundSession.last_instance.closed == []


def test_collect_shipped_refund_retains_return_after_refund_action_facts(monkeypatch):
    class ReturnAfterRefundSession(FakeCandidateSession):
        candidates = [False]

        def read_detail(self, window, order_no, detail_url, shop):
            return {
                "order_no": order_no,
                "aftersale_status": "待商家处理",
                "refund_type": "退款",
                "action_controls_read_complete": True,
                "available_aftersale_actions": [
                    "同意退款",
                    "同意快递退回后退款",
                ],
                "has_agree_refund": True,
                "has_agree_return_after_refund": True,
                "has_reject_refund": False,
            }

        def read_logistics_detail(self, window, order_no, detail_url, shop):
            return {
                "logistics_read_complete": True,
                "logistics_trajectory_count": 9,
                "logistics_trace_rows": [],
                "latest_logistics_text": "运输中",
                "logistics_latest_signed": False,
            }

    monkeypatch.setattr(
        aftersale_worker,
        "PddAftersaleUiaSession",
        ReturnAfterRefundSession,
    )

    result = aftersale_worker._run(
        {
            "operation": "collect",
            "order_no": "ORDER-SHIPPED-RETURN",
            "facts": {
                "work_order_title": AFTERSALE_SHIPPED_REFUND,
                "pdd_order_status": "已发货",
            },
            "shop": {"id": "shop-1"},
            "keep_detail_open": True,
        },
        Path("runtime/evidence"),
    )

    assert result["decision"]["action"] == AGREE_RETURN_AFTER_REFUND
    assert result["detail_retained"] is True
    inspected = result["facts"]["aftersale_candidates_inspected"][0]
    assert inspected["action_controls_read_complete"] is True
    assert inspected["available_aftersale_actions"] == [
        "同意退款",
        "同意快递退回后退款",
    ]
    assert inspected["has_agree_return_after_refund"] is True
    assert ReturnAfterRefundSession.last_instance.closed == []


def test_collect_shipped_refund_retains_reject_action_detail(monkeypatch):
    class RejectRefundSession(FakeCandidateSession):
        candidates = [False]

        def read_detail(self, window, order_no, detail_url, shop):
            return {
                "order_no": order_no,
                "aftersale_status": "待商家处理",
                "refund_type": "退款",
                "action_controls_read_complete": True,
                "available_aftersale_actions": ["同意退款", "驳回退款"],
                "has_agree_refund": True,
                "has_agree_return_after_refund": False,
                "has_reject_refund": True,
            }

        def read_logistics_detail(self, window, order_no, detail_url, shop):
            return {
                "logistics_read_complete": True,
                "logistics_trajectory_count": 12,
                "logistics_trace_rows": [],
                "latest_logistics_text": "已签收",
                "logistics_latest_signed": True,
            }

    monkeypatch.setattr(
        aftersale_worker,
        "PddAftersaleUiaSession",
        RejectRefundSession,
    )

    result = aftersale_worker._run(
        {
            "operation": "collect",
            "order_no": "ORDER-SHIPPED-REJECT",
            "facts": {
                "work_order_title": AFTERSALE_SHIPPED_REFUND,
                "pdd_order_status": "已发货",
            },
            "shop": {"id": "shop-1"},
            "keep_detail_open": True,
        },
        Path("runtime/evidence"),
    )

    assert result["decision"]["action"] == REJECT_REFUND
    assert result["detail_retained"] is True
    assert result["facts"]["aftersale_candidates_inspected"][0][
        "has_reject_refund"
    ] is True
    assert RejectRefundSession.last_instance.closed == []


def test_execute_passes_new_actions_to_uia_session(monkeypatch):
    class ExecuteSession:
        calls = []

        def __init__(self, config):
            self.config = config

        def activate_detail(self, shop, detail_url, order_no):
            return SimpleNamespace(handle=301)

        def read_detail(self, window, order_no, detail_url, shop):
            return {"order_no": order_no}

        def screenshot(self, handle, path):
            return None

        def execute_action(self, window, action, **kwargs):
            type(self).calls.append((action, kwargs))
            return {
                "ok": True,
                "action": action,
                "business_write_performed": True,
            }

        def close_detail(self, window, shop):
            return {"handled": True, "closed": 1, "list_ready": True}

    monkeypatch.setattr(aftersale_worker, "PddAftersaleUiaSession", ExecuteSession)
    ExecuteSession.calls = []

    for action in (AGREE_RETURN_AFTER_REFUND, REJECT_REFUND):
        result = aftersale_worker._run(
            {
                "operation": "execute",
                "shop": {"id": "shop-1"},
                "facts": {
                    "order_no": "ORDER-EXECUTE",
                    "aftersale_no": "AFTERSALE-EXECUTE",
                    "aftersale_detail_url": "https://example.invalid/detail/1",
                },
                "action": action,
                "live": True,
            },
            Path("runtime/evidence"),
        )
        assert result["action"] == action
        assert result["business_write_performed"] is True

    assert [item[0] for item in ExecuteSession.calls] == [
        AGREE_RETURN_AFTER_REFUND,
        REJECT_REFUND,
    ]
    assert all(
        item[1]["max_agree_refund_trace_count"] == 3
        for item in ExecuteSession.calls
    )

    aftersale_worker._run(
        {
            "operation": "execute",
            "shop": {"id": "shop-1"},
            "facts": {
                "order_no": "ORDER-JST",
                "aftersale_no": "AFTERSALE-JST",
                "aftersale_detail_url": "https://example.invalid/detail/jst",
                "wdt_warehouse": "示例供应链代发总仓",
            },
            "action": REJECT_REFUND,
            "live": True,
        },
        Path("runtime/evidence"),
    )
    assert ExecuteSession.calls[-1][1]["max_agree_refund_trace_count"] == 1


def test_execute_preserves_verified_write_when_post_action_cleanup_is_delayed(
    monkeypatch,
):
    class DelayedCleanupSession:
        def __init__(self, config):
            self.config = config

        def activate_detail(self, shop, detail_url, order_no):
            return SimpleNamespace(handle=302)

        def read_detail(self, window, order_no, detail_url, shop):
            return {"order_no": order_no}

        def screenshot(self, handle, path):
            return None

        def execute_action(self, window, action, **kwargs):
            return {
                "ok": True,
                "action": action,
                "deferred": True,
                "reason_code": "PDD_REJECT_NEGOTIATION_COOLDOWN",
                "business_write_performed": True,
                "submit_attempted": True,
            }

        def wait_for_post_action_render(self, window, **kwargs):
            return False

        def close_detail(self, window, shop):
            raise RuntimeError("详情页仍在重绘")

    monkeypatch.setattr(
        aftersale_worker, "PddAftersaleUiaSession", DelayedCleanupSession
    )

    result = aftersale_worker._run(
        {
            "operation": "execute",
            "shop": {"id": "shop-1"},
            "facts": {
                "order_no": "ORDER-REPAINT",
                "aftersale_no": "AFTERSALE-REPAINT",
                "aftersale_detail_url": "https://example.invalid/detail/repaint",
            },
            "action": REJECT_REFUND,
            "live": True,
        },
        Path("runtime/evidence"),
    )

    assert result["result"]["business_write_performed"] is True
    assert result["result"]["submit_attempted"] is True
    assert result["post_action_render_ready"] is False
    assert result["detail_cleanup"]["handled"] is False
    assert (
        result["detail_cleanup"]["disposition"]
        == "post_action_cleanup_deferred"
    )


def test_collect_fails_when_first_non_actionable_detail_did_not_close(monkeypatch):
    class FailedCleanupSession(FakeCandidateSession):
        opened = []

        def open_detail(self, shop, order_no, **kwargs):
            candidate_index = int(kwargs.get("candidate_index", 0))
            self.opened.append(candidate_index)
            return super().open_detail(shop, order_no, **kwargs)

        def close_detail(self, window, shop):
            self.closed.append(window.handle)
            return {"handled": False, "closed": 0, "list_ready": False}

    monkeypatch.setattr(aftersale_worker, "PddAftersaleUiaSession", FailedCleanupSession)

    import pytest

    with pytest.raises(RuntimeError, match="第一条详情页未能安全关闭"):
        aftersale_worker._run(
            {
                "operation": "collect",
                "order_no": "ORDER-3",
                "shop": {"id": "shop-1"},
                "keep_detail_open": True,
            },
            Path("runtime/evidence"),
        )

    assert FailedCleanupSession.last_instance.opened == [0]


def test_collect_targets_exact_aftersale_number(monkeypatch):
    class ExactSession(FakeCandidateSession):
        candidates = [True]

        def open_detail(self, shop, order_no, **kwargs):
            super().open_detail(shop, order_no, **kwargs)
            return (
                SimpleNamespace(handle=100),
                "https://mms.pinduoduo.com/aftersales-ssr/detail?id=AS123456789",
            )

        def read_detail(self, window, order_no, detail_url, shop):
            return {
                "order_no": order_no,
                "aftersale_no": "AS123456789",
                "refund_type": "退货退款",
                "has_extend_receipt": True,
            }

    monkeypatch.setattr(aftersale_worker, "PddAftersaleUiaSession", ExactSession)

    result = aftersale_worker._run(
        {
            "operation": "collect",
            "order_no": "ORDER-4",
            "aftersale_no": "AS123456789",
            "shop": {"id": "shop-1"},
            "keep_detail_open": True,
        },
        Path("runtime/evidence"),
    )

    assert ExactSession.last_instance.open_calls == [
        {
            "order_no": "ORDER-4",
            "candidate_index": 0,
            "search_value": "AS123456789",
            "expected_aftersale_no": "AS123456789",
            "require_shipped_refund_result": False,
        }
    ]
    assert result["facts"]["aftersale_candidate_policy"] == "EXACT_AFTERSALE_NO"


def test_collect_fails_closed_when_exact_aftersale_number_changes(monkeypatch):
    class MismatchSession(FakeCandidateSession):
        candidates = [True]

        def open_detail(self, shop, order_no, **kwargs):
            super().open_detail(shop, order_no, **kwargs)
            return (
                SimpleNamespace(handle=100),
                "https://mms.pinduoduo.com/aftersales-ssr/detail?id=AS999999999",
            )

        def read_detail(self, window, order_no, detail_url, shop):
            return {
                "order_no": order_no,
                "aftersale_no": "AS999999999",
                "refund_type": "退货退款",
                "has_extend_receipt": True,
            }

    monkeypatch.setattr(aftersale_worker, "PddAftersaleUiaSession", MismatchSession)

    import pytest

    with pytest.raises(RuntimeError, match="售后详情编号校验失败"):
        aftersale_worker._run(
            {
                "operation": "collect",
                "order_no": "ORDER-5",
                "aftersale_no": "AS123456789",
                "shop": {"id": "shop-1"},
            },
            Path("runtime/evidence"),
        )


def test_collect_refund_closed_closes_detail_without_action(monkeypatch):
    class ClosedSession(FakeCandidateSession):
        candidates = [True]

        def read_detail(self, window, order_no, detail_url, shop):
            return {
                "order_no": order_no,
                "aftersale_status": "商家退款关闭",
                "refund_type": "退货退款",
                "has_extend_receipt": True,
            }

    monkeypatch.setattr(aftersale_worker, "PddAftersaleUiaSession", ClosedSession)

    result = aftersale_worker._run(
        {
            "operation": "collect",
            "order_no": "ORDER-CLOSED",
            "shop": {"id": "shop-1"},
            "keep_detail_open": True,
        },
        Path("runtime/evidence"),
    )

    assert result["decision"]["action"] == SKIP_REFUND_CLOSED
    assert result["detail_retained"] is False
    assert result["business_write_performed"] is False
    assert ClosedSession.last_instance.closed == [100]


def test_collect_all_closed_search_results_skips_without_opening_detail(monkeypatch):
    class AllClosedSearchSession(FakeCandidateSession):
        def open_detail(self, shop, order_no, **kwargs):
            self.last_search_candidate_count = 2
            self.last_search_candidate_rows = [
                {
                    "candidate_index": 0,
                    "shipping_status": "已发货",
                    "refund_type": "退款",
                    "aftersale_status": "退款关闭",
                    "refund_closed": True,
                    "eligible": False,
                },
                {
                    "candidate_index": 1,
                    "shipping_status": "已发货",
                    "refund_type": "退款",
                    "aftersale_status": "商家退款关闭",
                    "refund_closed": True,
                    "eligible": False,
                },
            ]
            raise aftersale_worker.PddAftersaleSearchResultsClosed(
                f"售后订单 {order_no} 搜索结果中的已发货退款均为退款关闭，已忽略"
            )

    monkeypatch.setattr(
        aftersale_worker, "PddAftersaleUiaSession", AllClosedSearchSession
    )

    result = aftersale_worker._run(
        {
            "operation": "collect",
            "order_no": "ORDER-ALL-CLOSED",
            "facts": {
                "work_order_title": AFTERSALE_SHIPPED_REFUND,
                "pdd_order_status": "已发货",
            },
            "shop": {"id": "shop-1"},
            "keep_detail_open": True,
        },
        Path("runtime/evidence"),
    )

    assert result["decision"]["action"] == SKIP_REFUND_CLOSED
    assert result["detail_url"] == ""
    assert result["evidence"] == []
    assert result["detail_cleanup"]["handled"] is True
    assert result["detail_cleanup"]["closed"] == 0
    assert result["business_write_performed"] is False
    assert len(result["facts"]["aftersale_search_candidate_rows"]) == 2


def test_worker_client_serializes_exact_aftersale_number(monkeypatch):
    captured = {}

    class Process:
        returncode = 0

        def poll(self):
            return self.returncode

    process = Process()
    monkeypatch.setattr(
        aftersale_worker_client.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        aftersale_worker_client.ACTIVE_WORKERS, "register", lambda _process: None
    )
    monkeypatch.setattr(
        aftersale_worker_client.ACTIVE_WORKERS, "unregister", lambda _process: None
    )

    def communicate(_process, request_text, *_args, **_kwargs):
        captured.update(json.loads(request_text))
        return json.dumps({"ok": True}), ""

    monkeypatch.setattr(
        aftersale_worker_client, "communicate_registered_process", communicate
    )

    result = aftersale_worker_client.run_isolated_aftersale(
        project_root=Path("."),
        evidence_dir=Path("runtime/evidence"),
        config={},
        shop={"id": "shop-1"},
        operation="collect",
        order_no="ORDER-6",
        aftersale_no="AS123456789",
    )

    assert result == {"ok": True}
    assert captured["order_no"] == "ORDER-6"
    assert captured["aftersale_no"] == "AS123456789"
