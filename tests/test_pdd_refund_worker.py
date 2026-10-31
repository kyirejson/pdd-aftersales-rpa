from types import SimpleNamespace

from pdd_rpa import pdd_refund_worker


def test_in_transit_finish_closes_retained_detail_in_same_worker(monkeypatch):
    calls = []

    class FakeRepository:
        def __init__(self, database):
            self.database = database

        def initialize(self):
            calls.append("repository")

    class FakeRefundWorkflow:
        def __init__(self, repository, collector, config):
            self.browser = object()

        def activate_detail_after_wdt(self, facts):
            calls.append("activate")
            return {"activated": True}

        def close_retained_detail(self, facts):
            calls.append("close")
            return {"closed": 1, "list_ready": True, "shop_id": facts["shop_id"]}

    class FakeInTransitWorkflow:
        def __init__(self, repository, config, browser):
            assert browser is not None

        def finish(self, facts, action, *, live):
            calls.append(("finish", action, live))
            return {
                "ok": True,
                "action": action,
                "trace": [],
                "business_write_performed": bool(live),
            }

    monkeypatch.setattr(pdd_refund_worker, "Repository", FakeRepository)
    monkeypatch.setattr(pdd_refund_worker, "RefundComponentWorkflow", FakeRefundWorkflow)
    monkeypatch.setattr(pdd_refund_worker, "InTransitComponentWorkflow", FakeInTransitWorkflow)

    result = pdd_refund_worker._run(
        {
            "operation": "in_transit_finish",
            "facts": {"shop_id": "shop_01", "order_no": "260000-169765022796445"},
            "action": "AGREE_REFUND",
            "live": True,
            "close_detail_after_execute": True,
        },
        SimpleNamespace(
            database="unused.sqlite",
            evidence_dir=".",
        ),
    )

    assert calls == ["repository", "activate", ("finish", "AGREE_REFUND", True), "close"]
    assert result["result"]["ok"] is True
    assert result["business_write_performed"] is True
    assert result["detail_cleanup"] == {"closed": 1, "list_ready": True, "shop_id": "shop_01"}


def test_in_transit_complete_reuses_one_detail_session_for_all_post_interception_steps(monkeypatch):
    calls = []

    class FakeRepository:
        def __init__(self, database):
            self.database = database

        def initialize(self):
            calls.append("repository")

    class FakeBrowser:
        def refresh_detail(self, facts):
            calls.append("refresh")
            return {
                "tracking_no": facts["tracking_no"],
                "carrier": "中通快递",
                "latest_logistics_text": "【无锡市】快件已发往中转中心",
            }

    class FakeRefundWorkflow:
        def __init__(self, repository, collector, config):
            self.browser = FakeBrowser()

        def activate_detail_after_wdt(self, facts):
            calls.append("activate")
            return {"activated": True}

        def close_retained_detail(self, facts):
            calls.append("close")
            return {"closed": 1, "list_ready": True}

    class FakeInTransitWorkflow:
        def __init__(self, repository, config, browser):
            assert isinstance(browser, FakeBrowser)

        def prepare_reason(self, facts, reason_text, *, live):
            calls.append(("reason", reason_text, live))
            return {
                "ok": True,
                "detail_form": "reason",
                "refund_reason": reason_text,
                "trace": [],
            }

        def finish(self, facts, action, *, live):
            calls.append(("finish", action, live))
            return {"ok": True, "action": action, "trace": []}

        def wait_for_final_form(self, facts, action):
            calls.append(("wait_final_form", action))
            return {"ready": True}

    monkeypatch.setattr(pdd_refund_worker, "Repository", FakeRepository)
    monkeypatch.setattr(pdd_refund_worker, "RefundComponentWorkflow", FakeRefundWorkflow)
    monkeypatch.setattr(pdd_refund_worker, "InTransitComponentWorkflow", FakeInTransitWorkflow)
    monkeypatch.setattr(
        pdd_refund_worker,
        "decide_interception",
        lambda facts, config: {"ok": True, "final_action": "AGREE_REFUND"},
    )

    result = pdd_refund_worker._run(
        {
            "operation": "in_transit_complete",
            "facts": {
                "shop_id": "shop_01",
                "order_no": "260000-169765022796445",
                "tracking_no": "ZT001",
            },
            "routing_config": {"supported_warehouses": ["示例自营仓"]},
            "reason_text": "主观原因不想要",
            "live": True,
            "close_detail_after_execute": True,
        },
        SimpleNamespace(database="unused.sqlite", evidence_dir="."),
    )

    assert calls == [
        "repository",
        "activate",
        "refresh",
        ("reason", "主观原因不想要", True),
        ("wait_final_form", "AGREE_REFUND"),
        ("finish", "AGREE_REFUND", True),
        "close",
    ]
    assert result["effective_action"] == "AGREE_REFUND"
    assert result["detail_cleanup"] == {"closed": 1, "list_ready": True}
