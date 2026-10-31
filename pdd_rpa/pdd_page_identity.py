from __future__ import annotations

from urllib.parse import urlparse


LOGIN = "login"
WORK_ORDER_LIST = "work_order_list"
WORK_ORDER_DETAIL = "work_order_detail"
AFTERSALE_LIST = "aftersale_list"
AFTERSALE_DETAIL = "aftersale_detail"
AFTERSALE_SETUP = "aftersale_setup"
OTHER = "other"


def classify_pdd_surface(url: str) -> str:
    """Classify only allow-listed PDD merchant-backend page surfaces.

    The work-order module and the aftersale-workbench module intentionally use
    different surface names.  A caller must never treat one list/detail page
    as the other merely because both belong to mms.pinduoduo.com.
    """

    parsed = urlparse(str(url or ""))
    if parsed.netloc.lower() != "mms.pinduoduo.com":
        return OTHER
    path = parsed.path.rstrip("/").lower()
    return {
        "/login": LOGIN,
        "/aftersales/work_order/list": WORK_ORDER_LIST,
        "/aftersales/work_order/tododetail": WORK_ORDER_DETAIL,
        "/aftersales/aftersale_list": AFTERSALE_LIST,
        "/aftersales-ssr/detail": AFTERSALE_DETAIL,
        "/aftersales/setup": AFTERSALE_SETUP,
    }.get(path, OTHER)


__all__ = [
    "AFTERSALE_DETAIL",
    "AFTERSALE_LIST",
    "AFTERSALE_SETUP",
    "LOGIN",
    "OTHER",
    "WORK_ORDER_DETAIL",
    "WORK_ORDER_LIST",
    "classify_pdd_surface",
]
