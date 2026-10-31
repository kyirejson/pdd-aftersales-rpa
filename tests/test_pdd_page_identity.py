import pytest

from pdd_rpa.edge_uia import PddEdgeUiaSession, PddPageIdentityError
from pdd_rpa.pdd_page_identity import (
    AFTERSALE_DETAIL,
    AFTERSALE_LIST,
    AFTERSALE_SETUP,
    LOGIN,
    OTHER,
    WORK_ORDER_DETAIL,
    WORK_ORDER_LIST,
    classify_pdd_surface,
)


SHOP = {"id": "shop_01", "name": "示例书店 A"}
ORDER = "260000-039479311591310"


class FakeWindow:
    handle = 42

    def window_text(self):
        return "示例书店 A - Microsoft Edge"


class FakeControl:
    def __init__(self, name):
        self.name = name


def fake_session(url, names=()):
    session = object.__new__(PddEdgeUiaSession)
    session._current_url = lambda _window: url
    controls = [FakeControl(name) for name in names]
    session._controls = lambda _handle, visible_only=False: controls
    session._name = lambda control: control.name
    session._has_exact_name = lambda _handle, text: text in names
    session._pending_filter = lambda _handle: object()
    return session


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://mms.pinduoduo.com/login", LOGIN),
        ("https://mms.pinduoduo.com/aftersales/work_order/list?x=1", WORK_ORDER_LIST),
        ("https://mms.pinduoduo.com/aftersales/work_order/tododetail?id=1", WORK_ORDER_DETAIL),
        ("https://mms.pinduoduo.com/aftersales/aftersale_list", AFTERSALE_LIST),
        ("https://mms.pinduoduo.com/aftersales-ssr/detail?id=1", AFTERSALE_DETAIL),
        ("https://mms.pinduoduo.com/aftersales/setup?tab=3", AFTERSALE_SETUP),
        ("https://example.com/aftersales/work_order/list", OTHER),
    ],
)
def test_classifies_all_pdd_surfaces_without_cross_mapping(url, expected):
    assert classify_pdd_surface(url) == expected


def test_aftersale_list_cannot_be_consumed_as_work_order_list():
    session = fake_session(
        "https://mms.pinduoduo.com/aftersales/aftersale_list",
        [SHOP["name"], "售后单查询", "待处理即将逾期"],
    )
    with pytest.raises(PddPageIdentityError, match="预期=work_order_list.*实际=aftersale_list"):
        session.assert_page_contract(
            FakeWindow(), SHOP, WORK_ORDER_LIST, stage="工单扫描", require_ready=True
        )


def test_work_order_list_cannot_be_consumed_as_aftersale_list():
    session = fake_session(
        "https://mms.pinduoduo.com/aftersales/work_order/list",
        [SHOP["name"], "待处理"],
    )
    with pytest.raises(PddPageIdentityError, match="预期=aftersale_list.*实际=work_order_list"):
        session.assert_page_contract(
            FakeWindow(), SHOP, AFTERSALE_LIST, stage="售后扫描", require_ready=True
        )


def test_detail_contract_rejects_wrong_order_even_on_correct_surface():
    session = fake_session(
        "https://mms.pinduoduo.com/aftersales-ssr/detail?id=1",
        [SHOP["name"], "260000-281155523185556"],
    )
    with pytest.raises(PddPageIdentityError, match="页面订单身份不符"):
        session.assert_page_contract(
            FakeWindow(), SHOP, AFTERSALE_DETAIL, order_no=ORDER, stage="售后提交前"
        )


def test_aftersale_detail_accepts_duplicate_equal_order_nodes_when_url_matches():
    session = fake_session(
        f"https://mms.pinduoduo.com/aftersales-ssr/detail?id=1&orderSn={ORDER}",
        [SHOP["name"], ORDER, ORDER],
    )

    result = session.assert_page_contract(
        FakeWindow(), SHOP, AFTERSALE_DETAIL, order_no=ORDER, stage="售后打开完成"
    )

    assert result["order_no"] == ORDER


def test_aftersale_detail_rejects_duplicate_order_nodes_without_matching_url():
    session = fake_session(
        "https://mms.pinduoduo.com/aftersales-ssr/detail?id=1",
        [SHOP["name"], ORDER, ORDER],
    )

    with pytest.raises(PddPageIdentityError, match="URL订单核验=False"):
        session.assert_page_contract(
            FakeWindow(), SHOP, AFTERSALE_DETAIL, order_no=ORDER, stage="售后打开完成"
        )


def test_aftersale_list_contract_accepts_exact_shop_and_semantic_markers():
    session = fake_session(
        "https://mms.pinduoduo.com/aftersales/aftersale_list",
        [SHOP["name"], "售后单查询", "待处理即将逾期"],
    )
    result = session.assert_page_contract(
        FakeWindow(), SHOP, AFTERSALE_LIST, stage="售后扫描", require_ready=True
    )
    assert result["surface"] == AFTERSALE_LIST
    assert result["ready"] is True
