from pdd_rpa.qianniu_aftersale import (
    AGREE_REFUND,
    INTERCEPT_EXPRESS,
    MANUAL_REVIEW,
    decide_qianniu_unreceived_refund,
)


def test_pre_shipment_waiting_pickup_agrees_without_group_or_registration():
    for status in ("待客审", "已审核", "已取消", "未审核"):
        result = decide_qianniu_unreceived_refund(
            wdt_order_status=status,
            wdt_warehouse="",
            logistics_text="您的包裹正在等待揽收，请耐心等待",
        )
        assert result.action == AGREE_REFUND
        assert result.eligible_for_refund is True
        assert result.requires_group_interception is False
        assert result.requires_dingtalk_registration is False


def test_pre_shipment_without_waiting_pickup_does_not_guess_refund():
    result = decide_qianniu_unreceived_refund(
        wdt_order_status="已审核", wdt_warehouse="示例自营仓", logistics_text="运输中"
    )
    assert result.action == MANUAL_REVIEW


def test_shipped_five_or_fewer_agrees_after_group_and_registers():
    for status in ("已发货", "已完成"):
        for trace_count in (1, 5):
            result = decide_qianniu_unreceived_refund(
                wdt_order_status=status,
                wdt_warehouse="示例自营仓",
                logistics_text="运输中",
                logistics_trajectory_count=trace_count,
            )
            assert result.action == AGREE_REFUND
            assert result.requires_group_interception is True
            assert result.requires_dingtalk_registration is True
            assert result.eligible_for_refund is True
            assert result.logistics_trajectory_count == trace_count


def test_shipped_more_than_five_intercepts_without_registration():
    result = decide_qianniu_unreceived_refund(
        wdt_order_status="已完成",
        wdt_warehouse="示例自营仓",
        logistics_text="运输中",
        logistics_trajectory_count=6,
    )
    assert result.action == INTERCEPT_EXPRESS
    assert result.requires_group_interception is True
    assert result.requires_dingtalk_registration is False
    assert result.eligible_for_refund is False


def test_jushuitan_shipped_never_auto_refunds_even_with_one_trace():
    result = decide_qianniu_unreceived_refund(
        wdt_order_status="已发货",
        wdt_warehouse="示例供应链代发总仓",
        logistics_text="运输中",
        logistics_trajectory_count=1,
    )
    assert result.action == INTERCEPT_EXPRESS
    assert result.requires_group_interception is True
    assert result.requires_dingtalk_registration is False
    assert result.needs_product_routing is True
    assert result.eligible_for_refund is False


def test_shipped_unreadable_trajectory_is_manual_after_group_stage():
    result = decide_qianniu_unreceived_refund(
        wdt_order_status="已发货",
        wdt_warehouse="示例自营仓",
        logistics_text="运输中",
        logistics_trajectory_count=0,
    )
    assert result.action == MANUAL_REVIEW
    assert result.requires_group_interception is True
    assert result.requires_dingtalk_registration is False


def test_unknown_wdt_status_routes_to_manual_review_without_group():
    result = decide_qianniu_unreceived_refund(
        wdt_order_status="冻结",
        wdt_warehouse="示例自营仓",
        logistics_text="包裹正在等待揽收",
    )
    assert result.action == MANUAL_REVIEW
    assert result.requires_group_interception is False
