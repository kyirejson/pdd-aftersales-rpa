from __future__ import annotations

from datetime import datetime

from .base import ConnectorResult, JstConnector, LogisticsConnector, MessageConnector


class MockJstConnector(JstConnector):
    name = "jst.mock"

    def health(self) -> ConnectorResult:
        return ConnectorResult(True, {"mode": "mock"})

    def query_order(self, order_no: str, tracking_no: str = "") -> ConnectorResult:
        no_trace = order_no.endswith("002")
        return ConnectorResult(
            True,
            {
                "jst_outbound_status": "已出库",
                "jst_outbound_time": datetime.now().isoformat(timespec="seconds"),
                "jst_tracking_no": tracking_no,
                "jst_has_replaced_tracking": False,
                "jst_note": "模拟聚水潭查询",
                "jst_first_scan_expected": no_trace,
            },
        )


class MockLogisticsConnector(LogisticsConnector):
    name = "logistics.mock"

    def health(self) -> ConnectorResult:
        return ConnectorResult(True, {"mode": "mock"})

    def query(self, carrier: str, tracking_no: str) -> ConnectorResult:
        status = "NO_INFO" if "002" in tracking_no else "IN_TRANSIT"
        return ConnectorResult(
            True,
            {
                "carrier_tracking_status": status,
                "carrier_tracking_events": [] if status == "NO_INFO" else ["已揽收", "运输中"],
            },
        )


class MockMessageConnector(MessageConnector):
    name = "message.mock"

    def health(self) -> ConnectorResult:
        return ConnectorResult(True, {"mode": "preview_only", "write_enabled": False})

    def preview_send(self, group_name: str, message: str) -> ConnectorResult:
        return ConnectorResult(
            True,
            {"group_name": group_name, "message": message, "sent": False},
            evidence=["仅生成预览，未向真实群发送"],
        )
