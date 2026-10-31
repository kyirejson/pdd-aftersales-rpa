from __future__ import annotations

from types import SimpleNamespace

from pdd_rpa.aftersale_uia import AftersaleCard
from pdd_rpa.connectors import pdd_edge_uia
from pdd_rpa.connectors.pdd_edge_uia import PddEdgeUiaConnector


SHOP = {"id": "shop-1", "name": "测试店铺", "edge_profile": "Default"}


class MainSession:
    last_login_recovery = None
    last_pagination_trace = []

    def __init__(self, _config):
        self.restore_calls = []

    def refresh_pending_list(self, _shop):
        return SimpleNamespace(
            handle=101,
            title="测试店铺 - 工单管理",
            url="https://mms.pinduoduo.com/aftersales/work_order/list",
            cards=[],
            total=0,
        )

    def screenshot(self, _handle, _path):
        return None

    def restore_work_order_list_after_aftersale(self, shop, handle):
        self.restore_calls.append((shop, handle))
        return {"handled": True, "list_ready": True}


def _discover(monkeypatch, tmp_path, cards, summary):
    class AftersaleSession:
        def __init__(self, _config):
            self.last_collection_summary = dict(summary)

        def scan_pending(self, _shop):
            return SimpleNamespace(
                handle=202,
                title="测试店铺 - 售后工作台",
                url=(
                    "https://mms.pinduoduo.com/aftersales/aftersale_list"
                    "?searchType=4"
                ),
                cards=list(cards),
                total=len(cards),
            )

        def screenshot(self, _handle, _path):
            return None

    monkeypatch.setattr(pdd_edge_uia, "PddAftersaleUiaSession", AftersaleSession)
    connector = PddEdgeUiaConnector(
        {"aftersale_enabled": True},
        tmp_path,
        session_factory=MainSession,
        isolate=False,
    )
    return connector.discover(SHOP)


def test_pending_soon_overdue_keeps_refund_and_return_refund_with_safe_routing(
    monkeypatch, tmp_path
):
    cards = [
        AftersaleCard(
            order_no="ORDER-1",
            aftersale_no="ASREFUND001",
            refund_type="退款",
            shipping_status="未发货",
            refund_reason="不想要了",
            refund_amount=10.0,
            apply_time="2026-08-24 10:00:00",
        ),
        AftersaleCard(
            order_no="ORDER-1",
            aftersale_no="ASRETURN001",
            refund_type="退货退款",
            shipping_status="已发货",
            refund_reason="破损",
            refund_amount=20.0,
            apply_time="2026-08-24 10:05:00",
        ),
        AftersaleCard(
            order_no="ORDER-2",
            aftersale_no="ASRETURN002",
            refund_type="退货退款",
            shipping_status="已发货",
            refund_reason="少件",
            refund_amount=30.0,
            apply_time="2026-08-24 10:10:00",
        ),
    ]
    result = _discover(
        monkeypatch,
        tmp_path,
        cards,
        {
            "filter": "待处理即将逾期",
            "discovered_cards": 3,
            "pager_total": 3,
            "capture_complete": True,
            "pending_badge_count": 3,
            "pending_badge_exact": True,
        },
    )

    assert result.ok is True
    assert result.data["aftersale_total"] == 3
    items = result.data["items"]
    ordinary, exact_sibling, legacy_primary = items

    assert ordinary["work_order_title"] == "售后工作台：退款"
    assert ordinary["work_order_no"] == "AFTERSALE:REFUND:ASREFUND001"
    assert ordinary["work_order_no"] != "AFTERSALE:ORDER-1"
    assert ordinary["source_evidence"]["automation_eligible"] is False
    assert ordinary["source_evidence"]["display_only"] is True
    assert (
        ordinary["source_evidence"]["automation_block_reason"]
        == "ordinary_refund_not_shipped"
    )

    assert exact_sibling["work_order_no"] == "AFTERSALE:ASRETURN001"
    assert exact_sibling["source_evidence"]["automation_eligible"] is True
    assert exact_sibling["source_evidence"]["display_only"] is False

    assert legacy_primary["work_order_no"] == "AFTERSALE:ORDER-2"
    assert (
        legacy_primary["source_evidence"]["persistence_identity"]
        == "legacy_primary_order"
    )
    for item in items:
        evidence = item["source_evidence"]
        assert evidence["task_source"] == "aftersale_workbench"
        assert evidence["queue_scope"] == "pending_soon_overdue"
        assert evidence["filter_label"] == "待处理即将逾期"


def test_pending_shipped_ordinary_refund_enters_automation_queue(monkeypatch, tmp_path):
    card = AftersaleCard(
        order_no="ORDER-SHIPPED",
        aftersale_no="AS-SHIPPED-001",
        refund_type="退款",
        shipping_status="已发货",
        refund_amount=18.8,
    )
    result = _discover(
        monkeypatch,
        tmp_path,
        [card],
        {
            "filter": "待处理即将逾期",
            "discovered_cards": 1,
            "pager_total": 1,
            "capture_complete": True,
        },
    )

    item = result.data["items"][0]
    assert item["work_order_title"] == "售后工作台：已发货-退款"
    assert item["source_evidence"]["automation_eligible"] is True
    assert item["source_evidence"]["display_only"] is False
    assert item["source_evidence"]["queue_scope"] == "pending_soon_overdue"


def test_pending_soon_overdue_count_mismatch_keeps_rows_without_blocking_upload(
    monkeypatch, tmp_path
):
    card = AftersaleCard(
        order_no="ORDER-3",
        aftersale_no="ASREFUND003",
        refund_type="退款",
        refund_amount=5.0,
    )
    summary = {
        "filter": "待处理即将逾期",
        "discovered_cards": 1,
        "pager_total": 3,
        "capture_complete": False,
        "capture_shortfall": 2,
        "pending_badge_count": 99,
        "pending_badge_exact": False,
        "badge_vs_discovered_difference": None,
        "badge_vs_pager_difference": None,
    }

    result = _discover(monkeypatch, tmp_path, [card], summary)

    assert result.ok is True
    assert len(result.data["items"]) == 1
    assert result.data["aftersale_error"] == ""
    assert result.data["aftersale_collection_summary"] == summary


def test_work_order_failure_does_not_skip_fresh_aftersale_rows(monkeypatch, tmp_path):
    class FailingMainSession(MainSession):
        def refresh_pending_list(self, _shop):
            raise RuntimeError("工单列表恢复失败")

    class AftersaleSession:
        last_collection_summary = {
            "filter": "待处理即将逾期",
            "pager_total": 1,
            "capture_complete": True,
        }

        def __init__(self, _config):
            pass

        def scan_pending(self, _shop):
            return SimpleNamespace(
                handle=202,
                title="测试店铺 - 售后工作台",
                url="https://mms.pinduoduo.com/aftersales/aftersale_list",
                cards=[
                    AftersaleCard(
                        order_no="ORDER-FRESH-AFTERSALE",
                        aftersale_no="AS-FRESH-001",
                        refund_type="退款",
                        shipping_status="已发货",
                    )
                ],
                total=1,
            )

        def screenshot(self, _handle, _path):
            return None

    monkeypatch.setattr(pdd_edge_uia, "PddAftersaleUiaSession", AftersaleSession)
    connector = PddEdgeUiaConnector(
        {"aftersale_enabled": True},
        tmp_path,
        session_factory=FailingMainSession,
        isolate=False,
    )

    result = connector.discover(SHOP)

    assert result.ok is True
    assert result.error is None
    assert result.data["work_order_error"] == "工单列表恢复失败"
    assert result.data["work_order_total"] == 0
    assert result.data["aftersale_total"] == 1
    assert len(result.data["items"]) == 1
    assert result.data["items"][0]["order_no"] == "ORDER-FRESH-AFTERSALE"
    assert (
        result.data["items"][0]["source_evidence"]["source_system"]
        == "aftersale_workbench"
    )


def test_both_independent_sources_must_fail_before_shop_fails(monkeypatch, tmp_path):
    class FailingMainSession(MainSession):
        def refresh_pending_list(self, _shop):
            raise RuntimeError("工单列表恢复失败")

    class FailingAftersaleSession:
        def __init__(self, _config):
            self.last_collection_summary = {}

        def scan_pending(self, _shop):
            raise RuntimeError("售后列表恢复失败")

    monkeypatch.setattr(
        pdd_edge_uia, "PddAftersaleUiaSession", FailingAftersaleSession
    )
    connector = PddEdgeUiaConnector(
        {"aftersale_enabled": True},
        tmp_path,
        session_factory=FailingMainSession,
        isolate=False,
    )

    result = connector.discover(SHOP)

    assert result.ok is False
    assert result.data["items"] == []
    assert result.data["work_order_error"] == "工单列表恢复失败"
    assert result.data["aftersale_error"] == "售后列表恢复失败"
    assert result.error == "工单管理：工单列表恢复失败；售后工作台：售后列表恢复失败"
