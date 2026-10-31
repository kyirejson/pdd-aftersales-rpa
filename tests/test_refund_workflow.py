from __future__ import annotations

import pytest

from pdd_rpa.refund_workflow import RefundComponentWorkflow


class FakeRepository:
    def get_workflow(self, workflow_id):
        return {
            "workflow_id": workflow_id,
            "nodes": [
                {"component_id": "CMP-PDD-SELECT-UNHANDED"},
                {"component_id": "CMP-PDD-SUBMIT-UNHANDED"},
            ],
        }

    def get_component(self, component_id):
        return {"component_id": component_id, "name": component_id, "config": {}}


class FakeDetailCollector:
    def __init__(self):
        self.calls = []

    def collect(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("保留详情页模式不应再次打开工单")


class FakeBrowser:
    def __init__(self):
        self.target_calls = 0

    def _detail_target(self, facts):
        self.target_calls += 1
        return object(), 123

    def inspect_page_state(self, facts):
        return {
            "work_order_id_matches": True,
            "order_matches": True,
            "completed": False,
            "refund_success": False,
        }

    def execute_click(self, component, facts, live_actions_enabled):
        return {"ready": True, "executed": False}


def facts():
    return {
        "shop_id": "shop_03",
        "order_no": "260000-052956848194880",
        "source_evidence": {
            "detail_url": "https://mms.pinduoduo.com/aftersales/work_order/tododetail?id=1"
        },
    }


def test_preview_reuses_existing_detail_without_reopening():
    collector = FakeDetailCollector()
    workflow = RefundComponentWorkflow(FakeRepository(), collector, {})
    workflow.browser = FakeBrowser()

    result = workflow.preview_first_stage(facts(), retain_detail=True)

    assert result["detail_retained"] is True
    assert collector.calls == []
    assert workflow._prepared_detail["order_no"] == "260000-052956848194880"


def test_execution_requires_the_prepared_detail_marker():
    workflow = RefundComponentWorkflow(FakeRepository(), FakeDetailCollector(), {})
    workflow.browser = FakeBrowser()

    with pytest.raises(RuntimeError, match="未复用同一工单详情页"):
        workflow._require_open_detail(facts())


def test_shipped_after_interception_uses_handed_then_agree_sequence():
    calls = []

    class Browser(FakeBrowser):
        def execute_click(self, component, facts, live_actions_enabled):
            calls.append((component["locator"]["text"], live_actions_enabled))
            return {"executed": live_actions_enabled}

        def wait_for_text(self, facts, text, timeout_seconds):
            calls.append(("wait", text))
            return {"ready": True}

        def wait_for_refund_success(self, facts, timeout_seconds):
            calls.append(("success", timeout_seconds))
            return {"refund_processing": True}

    workflow = RefundComponentWorkflow(FakeRepository(), FakeDetailCollector(), {})
    workflow.browser = Browser()

    result = workflow.execute_shipped_after_interception(facts(), live=True)

    assert calls == [
        ("已交给快递", True),
        ("提交", True),
        ("wait", "同意退款"),
        ("同意退款", True),
        ("提交", True),
        ("success", 25),
    ]
    assert result["business_write_performed"] is True


def test_shipped_after_interception_resumes_from_agree_stage_without_replaying_handed():
    calls = []

    class Browser(FakeBrowser):
        def inspect_page_state(self, facts):
            return {
                "work_order_id_matches": True,
                "order_matches": True,
                "completed": False,
                "refund_success": False,
                "has_agree_refund": True,
            }

        def execute_click(self, component, facts, live_actions_enabled):
            calls.append((component["locator"]["text"], live_actions_enabled))
            return {"executed": live_actions_enabled}

        def wait_for_text(self, facts, text, timeout_seconds):
            calls.append(("wait", text))
            return {"ready": True, "marker": text}

        def wait_for_refund_success(self, facts, timeout_seconds):
            calls.append(("success", timeout_seconds))
            return {"refund_processing": True}

    workflow = RefundComponentWorkflow(FakeRepository(), FakeDetailCollector(), {})
    workflow.browser = Browser()

    result = workflow.execute_shipped_after_interception(facts(), live=True)

    assert calls == [
        ("wait", "同意退款"),
        ("同意退款", True),
        ("提交", True),
        ("success", 25),
    ]
    assert result["business_write_performed"] is True
    assert result["trace"][0]["component_id"] == "RESUME-AGREE-AFTER-INTERCEPT"
