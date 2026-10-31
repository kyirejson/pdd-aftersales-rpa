from pdd_rpa.aftersale_workbench import (
    AGREE_RECALL_AFTER_REFUND,
    AGREE_REFUND,
    AGREE_RETURN_AFTER_REFUND,
    EXTEND_RECEIPT,
    NEED_REVIEW,
    REJECT_REFUND,
    SKIP_REFUND_CLOSED,
    decide_aftersale,
)


def test_return_refund_extends_only_when_button_exists():
    assert decide_aftersale(
        {"refund_type": "退货退款", "has_extend_receipt": True}
    ).action == EXTEND_RECEIPT
    assert decide_aftersale(
        {"refund_type": "退货退款", "has_extend_receipt": False}
    ).action == NEED_REVIEW


def test_shipped_refund_requires_list_shipping_and_complete_logistics():
    missing_shipping = decide_aftersale(
        {
            "refund_type": "退款",
            "logistics_read_complete": True,
            "logistics_trajectory_count": 1,
        }
    )
    assert missing_shipping.action == NEED_REVIEW
    assert "不是已发货" in missing_shipping.reason

    incomplete = decide_aftersale(
        {
            "refund_type": "退款",
            "list_shipping_status": "已发货",
            "logistics_read_complete": False,
            "logistics_trajectory_count": 1,
        }
    )
    assert incomplete.action == NEED_REVIEW
    assert "尚未完整" in incomplete.reason


def test_shipped_refund_agrees_at_three_traces_or_less_without_action_inventory():
    for trace_count in (0, 1, 3):
        result = decide_aftersale(
            {
                "refund_type": "退款",
                "list_shipping_status": "已发货",
                "logistics_read_complete": True,
                "logistics_trajectory_count": trace_count,
            }
        )
        assert result.action == AGREE_REFUND


def test_shipped_refund_jushuitan_agrees_only_at_one_trace():
    base = {
        "refund_type": "退款",
        "list_shipping_status": "已发货",
        "logistics_read_complete": True,
        "wdt_warehouse": "示例供应链代发总仓",
        "action_controls_read_complete": True,
        "has_agree_return_after_refund": True,
        "has_reject_refund": True,
    }

    one = decide_aftersale({**base, "logistics_trajectory_count": 1})
    two = decide_aftersale({**base, "logistics_trajectory_count": 2})

    assert one.action == AGREE_REFUND
    assert "示例供应链仓不超过一条" in one.reason
    assert two.action == AGREE_RETURN_AFTER_REFUND


def test_shipped_refund_junfu_keeps_three_trace_limit():
    result = decide_aftersale(
        {
            "refund_type": "退款",
            "list_shipping_status": "已发货",
            "logistics_read_complete": True,
            "logistics_trajectory_count": 3,
            "wdt_warehouse": "示例自营仓",
        }
    )

    assert result.action == AGREE_REFUND
    assert "示例自营仓不超过三条" in result.reason


def test_shipped_refund_over_three_requires_complete_action_inventory():
    result = decide_aftersale(
        {
            "refund_type": "退款",
            "list_shipping_status": "已发货",
            "logistics_read_complete": True,
            "logistics_trajectory_count": 4,
        }
    )
    assert result.action == NEED_REVIEW
    assert "动作控件尚未完整读取" in result.reason

    unread_even_with_button_flags = decide_aftersale(
        {
            "refund_type": "退款",
            "list_shipping_status": "已发货",
            "logistics_read_complete": True,
            "logistics_trajectory_count": 4,
            "action_controls_read_complete": False,
            "has_agree_return_after_refund": True,
            "has_reject_refund": True,
        }
    )
    assert unread_even_with_button_flags.action == NEED_REVIEW
    assert "动作控件尚未完整读取" in unread_even_with_button_flags.reason


def test_shipped_refund_over_three_prefers_return_after_refund():
    result = decide_aftersale(
        {
            "refund_type": "退款",
            "list_shipping_status": "已发货",
            "logistics_read_complete": True,
            "logistics_trajectory_count": 4,
            "action_controls_read_complete": True,
            "has_agree_return_after_refund": True,
            "has_reject_refund": True,
        }
    )

    assert result.action == AGREE_RETURN_AFTER_REFUND
    assert "优先同意快递退回后退款" in result.reason


def test_shipped_refund_over_three_uses_reject_when_return_action_is_absent():
    result = decide_aftersale(
        {
            "refund_type": "退款",
            "list_shipping_status": "已发货",
            "logistics_read_complete": True,
            "logistics_trajectory_count": 12,
            "action_controls_read_complete": True,
            "has_agree_return_after_refund": False,
            "has_reject_refund": True,
        }
    )

    assert result.action == REJECT_REFUND
    assert "改走驳回退款" in result.reason


def test_shipped_refund_over_three_uses_recall_before_reject():
    result = decide_aftersale(
        {
            "refund_type": "退款",
            "list_shipping_status": "已发货",
            "logistics_read_complete": True,
            "logistics_trajectory_count": 12,
            "action_controls_read_complete": True,
            "has_agree_return_after_refund": False,
            "has_agree_recall_after_refund": True,
            "has_reject_refund": True,
        }
    )

    assert result.action == AGREE_RECALL_AFTER_REFUND
    assert "提供同意召回后退款" in result.reason


def test_shipped_refund_return_and_recall_are_mutually_exclusive():
    result = decide_aftersale(
        {
            "refund_type": "退款",
            "list_shipping_status": "已发货",
            "logistics_read_complete": True,
            "logistics_trajectory_count": 12,
            "action_controls_read_complete": True,
            "has_agree_return_after_refund": True,
            "has_agree_recall_after_refund": True,
        }
    )

    assert result.action == NEED_REVIEW
    assert "异常地同时出现" in result.reason


def test_shipped_refund_over_three_needs_review_when_no_safe_action_exists():
    result = decide_aftersale(
        {
            "refund_type": "退款",
            "list_shipping_status": "已发货",
            "logistics_read_complete": True,
            "logistics_trajectory_count": 5,
            "action_controls_read_complete": True,
            "has_agree_return_after_refund": False,
            "has_reject_refund": False,
        }
    )

    assert result.action == NEED_REVIEW
    assert "没有可安全执行" in result.reason


def test_refund_closed_is_always_ignored_before_other_rules():
    for status in ("退款关闭", "商家退款关闭", "退款 关闭"):
        result = decide_aftersale(
            {
                "aftersale_status": status,
                "refund_type": "退货退款",
                "has_extend_receipt": True,
            }
        )
        assert result.action == SKIP_REFUND_CLOSED
        assert "退款关闭" in result.reason

    shipped_result = decide_aftersale(
        {
            "aftersale_status": "平台退款关闭",
            "refund_type": "退款",
            "list_shipping_status": "已发货",
            "logistics_read_complete": True,
            "logistics_trajectory_count": 9,
            "action_controls_read_complete": True,
            "has_agree_return_after_refund": True,
            "has_reject_refund": True,
        }
    )
    assert shipped_result.action == SKIP_REFUND_CLOSED


def test_refund_succeeded_is_always_ignored_before_other_rules():
    for status in ("退款成功", "商家退款成功", "退款 成功"):
        result = decide_aftersale(
            {
                "aftersale_status": status,
                "refund_type": "退款",
                "list_shipping_status": "已发货",
                "logistics_read_complete": True,
                "logistics_trajectory_count": 9,
                "action_controls_read_complete": True,
                "has_agree_return_after_refund": True,
                "has_reject_refund": True,
            }
        )

        assert result.action == SKIP_REFUND_CLOSED
        assert "退款成功" in result.reason
