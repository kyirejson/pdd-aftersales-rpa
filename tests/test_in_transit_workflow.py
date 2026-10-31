import pytest

from pdd_rpa.in_transit_workflow import (
    InTransitComponentWorkflow,
    InTransitWorkflowError,
    decide_outcome_form_action,
)
from pdd_rpa.browser_executor import BrowserExecutionError


class _FakeBrowser:
    def __init__(self, initial_state=None, first_terminal=None, target_counts=None):
        self.clicks = []
        self.waits = []
        self.initial_state = initial_state or {
            "withdraw_requested": False,
            "withdraw_followup_required": False,
            "visible_forms": 1,
        }
        self.first_terminal = first_terminal or {
            "withdraw_requested": True,
            "withdraw_followup_required": True,
            "visible_forms": 1,
        }
        self.target_counts = dict(target_counts or {})

    def describe_click_targets(self, facts, texts):
        return {
            text: {
                "count": self.target_counts.get(text, 1),
                "selector_unique": self.target_counts.get(text, 1) == 1,
            }
            for text in texts
        }

    def inspect_page_state(self, facts):
        return dict(self.initial_state)

    def execute_click(self, component, facts, live):
        self.clicks.append(component["component_id"])
        return {"ready": True, "executed": True}

    def wait_for_terminal_state(self, facts, **kwargs):
        self.waits.append(dict(kwargs))
        if kwargs.get("require_withdraw_completion"):
            return {"completed": True, "withdraw_followup_required": False}
        return dict(self.first_terminal)


def test_reject_flow_submits_followup_withdrawal_when_pdd_requests_it():
    workflow = object.__new__(InTransitComponentWorkflow)
    browser = _FakeBrowser()
    workflow.browser = browser
    workflow._capture_or_reuse = lambda facts, component_id, text, **kwargs: {
        "component_id": component_id,
        "name": component_id,
    }

    result = workflow.finish({}, "REJECT_REFUND", live=True)

    assert result["ok"] is True
    assert browser.clicks == [
        workflow.WITHDRAW_COMPONENT,
        workflow.WITHDRAW_SUBMIT_COMPONENT,
        workflow.WITHDRAW_COMPONENT,
        workflow.WITHDRAW_SUBMIT_COMPONENT,
    ]
    assert browser.waits == [
        {"expected_action": "REJECT_REFUND", "require_withdraw_followup": True},
        {"expected_action": "REJECT_REFUND", "require_withdraw_completion": True},
    ]


def test_reject_flow_resumes_only_the_second_submission_after_phase_one():
    workflow = object.__new__(InTransitComponentWorkflow)
    browser = _FakeBrowser(
        initial_state={
            "withdraw_requested": True,
            "withdraw_followup_required": True,
            "visible_forms": 1,
        }
    )
    workflow.browser = browser
    workflow._capture_or_reuse = lambda facts, component_id, text, **kwargs: {
        "component_id": component_id,
        "name": component_id,
    }

    result = workflow.finish({}, "REJECT_REFUND", live=True)

    assert result["ok"] is True
    assert browser.clicks == [workflow.WITHDRAW_COMPONENT, workflow.WITHDRAW_SUBMIT_COMPONENT]
    assert browser.waits == [
        {"expected_action": "REJECT_REFUND", "require_withdraw_completion": True}
    ]


def test_reject_flow_refuses_to_mark_complete_when_second_form_does_not_appear():
    workflow = object.__new__(InTransitComponentWorkflow)
    browser = _FakeBrowser(
        first_terminal={
            "withdraw_requested": True,
            "withdraw_followup_required": False,
            "visible_forms": 0,
        }
    )
    workflow.browser = browser
    workflow._capture_or_reuse = lambda facts, component_id, text, **kwargs: {
        "component_id": component_id,
        "name": component_id,
    }

    with pytest.raises(InTransitWorkflowError, match="第二次提交页面"):
        workflow.finish({}, "REJECT_REFUND", live=True)


@pytest.mark.parametrize("carrier", ["中通快递", "韵达快递", "顺丰快递", "圆通快递"])
def test_reject_flow_is_carrier_neutral_after_interception(carrier):
    """Only the chat-delivery route varies by carrier, never the PDD finish."""
    workflow = object.__new__(InTransitComponentWorkflow)
    browser = _FakeBrowser()
    workflow.browser = browser
    workflow._capture_or_reuse = lambda facts, component_id, text, **kwargs: {
        "component_id": component_id,
        "name": component_id,
    }

    workflow.finish({"carrier": carrier}, "REJECT_REFUND", live=True)

    assert browser.clicks == [
        workflow.WITHDRAW_COMPONENT,
        workflow.WITHDRAW_SUBMIT_COMPONENT,
        workflow.WITHDRAW_COMPONENT,
        workflow.WITHDRAW_SUBMIT_COMPONENT,
    ]


@pytest.mark.parametrize(
    ("trace", "action", "blocked"),
    [
        ("【无锡市】已签收，无锡派送中", "AGREE_REFUND", ["已签收", "派送中"]),
        ("【无锡市】派送中", "AGREE_REFUND", ["派送中"]),
        ("快件已到达无锡分拨交付中心，已签收", "AGREE_REFUND", ["已签收"]),
        ("【示例城市乙市】已离开示例城市乙市分拨中心", "AGREE_REFUND", []),
        ("【示例城市乙市】已签收", "AGREED_CONSENSUS", ["已签收"]),
        ("【示例城市乙市】派送中", "AGREED_CONSENSUS", ["派送中"]),
        ("【淮安市】快件已发往连云港接驳站", "AGREED_CONSENSUS", []),
        (
            "【示例城市乙市】已离开示例城市乙市分拨中心（如遇物流问题请联系平台，已签收以官网为准）",
            "AGREE_REFUND",
            [],
        ),
    ],
)
def test_outcome_form_city_and_status_rule(trace, action, blocked):
    decision = decide_outcome_form_action(trace)
    assert decision["action"] == action
    assert decision["blocked_statuses"] == blocked


def test_prepare_uses_outcome_form_when_subjective_reason_is_absent():
    workflow = object.__new__(InTransitComponentWorkflow)
    browser = _FakeBrowser(target_counts={"主观原因不想要": 0})
    workflow.browser = browser
    captured = []
    workflow._capture_or_reuse = lambda facts, component_id, text, **kwargs: captured.append(
        (component_id, text)
    ) or {"component_id": component_id, "name": component_id}

    result = workflow.prepare_reason(
        {"latest_logistics_text": "【无锡市】已签收"},
        "主观原因不想要",
        live=True,
    )

    assert result["detail_form"] == workflow.OUTCOME_FORM
    assert result["final_action_override"] == "AGREE_REFUND"
    assert result["refund_reason"] == "同意退款"
    assert result["business_write_performed"] is False
    assert captured == [
        (workflow.OUTCOME_AGREE_COMPONENT, "同意退款"),
        (workflow.OUTCOME_CONSENSUS_COMPONENT, "已协商一致"),
        (workflow.OUTCOME_SUBMIT_COMPONENT, "提交"),
    ]


def test_outcome_form_agreed_consensus_clicks_once_then_submits():
    workflow = object.__new__(InTransitComponentWorkflow)
    browser = _FakeBrowser()
    workflow.browser = browser
    workflow._capture_or_reuse = lambda facts, component_id, text, **kwargs: {
        "component_id": component_id,
        "name": component_id,
    }

    result = workflow.finish(
        {"in_transit_detail_form": workflow.OUTCOME_FORM},
        "AGREED_CONSENSUS",
        live=True,
    )

    assert result["action"] == "AGREED_CONSENSUS"
    assert browser.clicks == [
        workflow.OUTCOME_CONSENSUS_COMPONENT,
        workflow.OUTCOME_SUBMIT_COMPONENT,
    ]
    assert browser.waits == [{"expected_action": "AGREED_CONSENSUS", "timeout_seconds": 5}]


def test_outcome_form_skips_when_consensus_submit_does_not_change_page():
    class IneffectiveOutcomeBrowser(_FakeBrowser):
        def wait_for_terminal_state(self, facts, **kwargs):
            self.waits.append(dict(kwargs))
            raise BrowserExecutionError("提交后处理后未回读到终态")

        def inspect_page_state(self, facts):
            return {
                "has_agreed_consensus": True,
                "visible_forms": 1,
            }

    workflow = object.__new__(InTransitComponentWorkflow)
    browser = IneffectiveOutcomeBrowser()
    workflow.browser = browser
    workflow._capture_or_reuse = lambda facts, component_id, text, **kwargs: {
        "component_id": component_id,
        "name": component_id,
    }

    result = workflow.finish(
        {"in_transit_detail_form": workflow.OUTCOME_FORM},
        "AGREED_CONSENSUS",
        live=True,
    )

    assert result["ok"] is True
    assert result["submission_ineffective"] is True
    assert result["skip_reason"] == "已协商一致并提交后表单未变化"
    assert browser.clicks == [
        workflow.OUTCOME_CONSENSUS_COMPONENT,
        workflow.OUTCOME_SUBMIT_COMPONENT,
    ]
    assert browser.waits == [{"expected_action": "AGREED_CONSENSUS", "timeout_seconds": 5}]
