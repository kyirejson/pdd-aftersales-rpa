from pathlib import Path

import pytest

from pdd_rpa.detail_collector import PddDetailCollector, extract_detail_fields
from pdd_rpa.edge_uia import PddUiaError


def test_extract_detail_fields_from_explicit_labels():
    fields = extract_detail_fields(
        "关联订单 260000-107446580937970\n工单详情\n订单编号：260000-107446580937970\n退款金额：¥16.80\n物流公司：中通快递\n物流单号：ZT12345678901"
    )
    assert fields == {
        "order_no": "260000-107446580937970",
        "tracking_no": "ZT12345678901",
        "carrier": "中通快递",
        "refund_amount": "16.80",
    }


def test_extract_detail_fields_from_logistics_line():
    fields = extract_detail_fields(
        "订单编号：260000-107446580937970\n物流信息：圆通速递 YT23508777230"
    )
    assert fields["tracking_no"] == "YT23508777230"
    assert fields["carrier"] == "圆通速递"


def test_invalid_order_is_rejected_before_browser_access():
    collector = PddDetailCollector({}, (), Path("runtime/evidence"))
    with pytest.raises(PddUiaError):
        collector.collect("shop_01", "not-an-order")
