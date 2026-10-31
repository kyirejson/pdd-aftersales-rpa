import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from pdd_rpa.connectors.pdd_edge_readonly import (
    PddEdgeReadOnlyConnector,
    PddReadOnlyError,
    WindowSnapshot,
    card_identity,
    classify_pdd_url,
    parse_card,
    select_shop_snapshot,
    validate_complete_snapshot,
)


SHOP = {"id": "shop_01", "name": "示例书店 A", "edge_profile": "Profile"}
LIST_URL = "https://mms.pinduoduo.com/aftersales/work_order/list?msfrom=mms_sidenav"


def card(order="260000-224539649176559", work_id="work-1"):
    return {
        "text": (
            f"订单号：{order}\n订单问题：已发货无物流轨迹\n"
            "商品名称\n￥88.00\n剩余2小时30分钟\n待处理\n立即处理"
        ),
        "detail_url": (
            "https://mms.pinduoduo.com/aftersales/work_order/"
            f"tododetail?id={work_id}"
        ),
        "dom_key": "",
    }


class FakeSession:
    refresh_calls = 0

    def __init__(self, config):
        self.snapshot = WindowSnapshot(
            10,
            "工单管理 - Microsoft Edge",
            LIST_URL,
            "示例书店 A\n工单管理\n共查询到 1 个工单",
        )

    def health(self):
        return {"available": True, "visible_edge_windows": 1}

    def diagnostics(self):
        return [{"page_kind": "list", "login_required": False}]

    def snapshots(self):
        return [self.snapshot]

    def refresh_and_wait(self, handle, expected_kind, expected_shop, required_text=""):
        type(self).refresh_calls += 1
        return self.snapshot

    def cards(self, handle):
        return [card()]

    def collect_cards(self, handle, total):
        return self.cards(handle)

    def screenshot(self, handle, path):
        return None


class PddReadOnlyParserTests(unittest.TestCase):
    def test_only_expected_pdd_paths_are_classified(self):
        self.assertEqual(classify_pdd_url(LIST_URL), "list")
        self.assertEqual(classify_pdd_url("https://example.com/aftersales/work_order/list"), "other")

    def test_snapshot_total_is_only_a_moving_hint(self):
        self.assertEqual(validate_complete_snapshot("共查询到 2 个工单", [card()]), 1)

    def test_exact_shop_identity_is_required(self):
        snapshot = WindowSnapshot(1, "Edge", LIST_URL, "其他店铺\n工单管理")
        with self.assertRaises(PddReadOnlyError):
            select_shop_snapshot([snapshot], SHOP)

    def test_card_parser_does_not_treat_display_price_as_refund_amount(self):
        item = parse_card(card(), SHOP)
        self.assertEqual(item["work_order_no"], "work-1")
        self.assertEqual(item["refund_amount"], 0.0)
        self.assertEqual(item["source_evidence"]["card_display_amount"], "88.00")
        deadline = datetime.fromisoformat(item["work_order_deadline"])
        self.assertGreater(deadline, datetime.now())

    def test_creation_time_is_stable_fallback_identity(self):
        item = card()
        item["detail_url"] = ""
        item["dom_key"] = ""
        item["text"] += "\n创建时间：2026-08-05 08:17:32"
        self.assertEqual(
            card_identity(item),
            "created:260000-224539649176559:2026-08-05 08:17:32",
        )

    def test_connector_collects_without_write_capability(self):
        FakeSession.refresh_calls = 0
        with tempfile.TemporaryDirectory() as temporary:
            connector = PddEdgeReadOnlyConnector({}, Path(temporary), session_factory=FakeSession)
            result = connector.discover(SHOP)
        self.assertTrue(result.ok)
        self.assertFalse(connector.supports_live_writes)
        self.assertEqual(result.data["total"], 1)
        self.assertEqual(len(result.data["items"]), 1)
        self.assertEqual(FakeSession.refresh_calls, 1)


if __name__ == "__main__":
    unittest.main()
