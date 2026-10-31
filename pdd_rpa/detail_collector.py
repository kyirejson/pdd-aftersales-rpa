from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .edge_uia import (
    PddAlreadyProcessedError,
    PddEdgeUiaSession,
    PddUiaError,
)


ORDER_RE = re.compile(r"\b\d{6}-\d{15,}\b")
ORDER_LABEL_RE = re.compile(r"(?:订单编号|订单号)[：:]\s*(\d{6}-\d{15,})")
TRACKING_LABEL_RE = re.compile(r"(?:物流单号|快递单号|运单号)[：:]\s*([A-Za-z0-9-]{8,})", re.I)
CARRIER_RE = re.compile(r"(?:物流公司|快递公司|承运商)[：:]\s*([^\r\n]{2,20})")
LOGISTICS_LINE_RE = re.compile(r"物流信息[：:]\s*([^\s\r\n]{2,20})\s+([A-Za-z0-9]{10,})", re.I)
REFUND_AMOUNT_RE = re.compile(r"退款金额[：:]\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)")


def extract_detail_fields(page_text: str) -> dict[str, str]:
    """Legacy text parser retained only for fixture/import compatibility, not live capture."""
    text = str(page_text or "")
    order_match = ORDER_LABEL_RE.search(text)
    tracking_match = TRACKING_LABEL_RE.search(text)
    logistics_match = LOGISTICS_LINE_RE.search(text)
    carrier_match = CARRIER_RE.search(text)
    refund_match = REFUND_AMOUNT_RE.search(text)
    return {
        "order_no": order_match.group(1) if order_match else "",
        "tracking_no": (
            tracking_match.group(1).strip()
            if tracking_match
            else logistics_match.group(2).strip() if logistics_match else ""
        ),
        "carrier": (
            carrier_match.group(1).strip()
            if carrier_match
            else logistics_match.group(1).strip() if logistics_match else ""
        ),
        "refund_amount": refund_match.group(1) if refund_match else "",
    }


@dataclass(slots=True)
class DetailCollectionResult:
    shop_id: str
    order_no: str
    detail_url: str
    tracking_no: str
    carrier: str
    latest_logistics_text: str
    refund_amount: float
    evidence_path: str
    list_page_number: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "shop_id": self.shop_id,
            "order_no": self.order_no,
            "detail_url": self.detail_url,
            "tracking_no": self.tracking_no,
            "carrier": self.carrier,
            "latest_logistics_text": self.latest_logistics_text,
            "refund_amount": self.refund_amount,
            "evidence_path": self.evidence_path,
            "list_page_number": self.list_page_number,
            "business_write_performed": False,
            "engine": "edge_uia",
        }


class PddDetailCollector:
    """Open and read a uniquely bound work-order detail through Edge UIA only."""

    def __init__(
        self,
        pdd_config: dict[str, Any],
        shops: tuple[Any, ...],
        evidence_dir: Path,
        session_factory: Callable[[dict[str, Any]], PddEdgeUiaSession] = PddEdgeUiaSession,
    ):
        self.pdd_config = pdd_config
        self.shops = shops
        self.evidence_dir = Path(evidence_dir)
        self.session_factory = session_factory

    def _shop(self, shop_id: str) -> dict[str, str]:
        for shop in self.shops:
            if shop.id == shop_id and shop.enabled:
                return {"id": shop.id, "name": shop.name, "edge_profile": shop.edge_profile}
        raise KeyError(f"店铺不存在或已停用：{shop_id}")

    @staticmethod
    def _tab_items(session: Any, handle: int) -> list[Any]:
        try:
            return [
                item for item in session.Desktop(backend="uia").window(handle=handle).descendants(control_type="TabItem")
                if str(item.element_info.class_name or "") == "EdgeTab"
            ]
        except Exception:
            return []

    def collect(
        self,
        shop_id: str,
        order_no: str,
        keep_detail_open: bool = False,
        *,
        require_logistics: bool = True,
    ) -> DetailCollectionResult:
        if not ORDER_RE.fullmatch(str(order_no or "")):
            raise PddUiaError("订单号格式无效，拒绝定位工单")
        shop = self._shop(shop_id)
        session = self.session_factory(self.pdd_config)
        window, detail_url = session.open_detail(shop, order_no)
        completed = False
        try:
            work_order_id = parse_qs(urlparse(detail_url).query).get("id", [""])[0]
            page_state = session.detail_state(int(window.handle), order_no, work_order_id)
            if page_state.get("completed") or page_state.get("refund_success") or page_state.get("refund_processing"):
                raise PddAlreadyProcessedError(
                    f"订单 {order_no} 详情显示已处理或退款进行中，直接跳过：{page_state}"
                )
            fields = (
                session.read_detail(int(window.handle), order_no)
                if require_logistics
                else {
                    "tracking_no": "",
                    "carrier": "",
                    "latest_logistics_text": "",
                    "refund_amount": 0,
                }
            )
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            evidence = self.evidence_dir / f"detail_{shop_id}_{order_no}_{stamp}_uia.png"
            session.screenshot(int(window.handle), evidence)
            result = DetailCollectionResult(
                shop_id=shop_id,
                order_no=order_no,
                detail_url=detail_url,
                tracking_no=str(fields["tracking_no"]),
                carrier=str(fields["carrier"]),
                latest_logistics_text=str(fields.get("latest_logistics_text") or ""),
                refund_amount=float(fields["refund_amount"] or 0),
                evidence_path=str(evidence),
                list_page_number=1,
            )
            completed = True
            return result
        finally:
            if not (keep_detail_open and completed):
                session.close_detail(window, shop)

    def close_active_detail(self, shop_id: str) -> dict[str, Any]:
        """Close finished PDD detail tabs and confirm the shop is back on list."""
        shop = self._shop(shop_id)
        session = self.session_factory(self.pdd_config)
        result = session.close_detail_tabs(shop)
        return {**result, "shop_id": shop_id}
