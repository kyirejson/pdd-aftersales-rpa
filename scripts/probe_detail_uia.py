from __future__ import annotations

import json
import re
import time

from pdd_rpa.config import load_config
from pdd_rpa.connectors.pdd_edge_readonly import WindowsEdgeReadOnlySession, classify_pdd_url


ORDER = "260000-000000000000000"
PATTERN = re.compile(r"工单详情|已完结|未交给快递|同意退款|提交|订单编号|物流信息|退款金额|售后状态|圆通|中通|YT\d+|\d{12,}")


def main() -> None:
    config = load_config()
    session = WindowsEdgeReadOnlySession(config.raw["pdd_readonly"])
    windows = [
        w for w in session._windows()
        if "示例书店 A" in w.window_text() and classify_pdd_url(session._current_url(w)) == "list"
    ]
    if len(windows) != 1:
        raise RuntimeError(f"list window count: {len(windows)}")
    window = windows[0]
    ui_window = session.Desktop(backend="uia").window(handle=int(window.handle))
    order_controls = [c for c in ui_window.descendants(control_type="Text") if str(c.element_info.name or "").strip() == ORDER]
    if len(order_controls) != 1:
        raise RuntimeError(f"order control count: {len(order_controls)}")
    y = order_controls[0].rectangle().top
    actions = [
        c for c in ui_window.descendants(control_type="Text")
        if str(c.element_info.name or "").strip() == "立即处理" and abs(c.rectangle().top - (y + 57)) < 25
    ]
    if len(actions) != 1:
        raise RuntimeError(f"action count: {len(actions)}")
    session._focus(window)
    actions[0].click_input()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        time.sleep(0.7)
        if classify_pdd_url(session._current_url(window)) == "detail":
            break
    else:
        raise RuntimeError("detail did not open")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        time.sleep(0.7)
        names = [str(c.element_info.name or "").strip() for c in session.Desktop(backend="uia").window(handle=int(window.handle)).descendants()]
        if ORDER in names:
            break
    controls = []
    for control in session.Desktop(backend="uia").window(handle=int(window.handle)).descendants():
        try:
            name = str(control.element_info.name or "").strip()
            if not name or not PATTERN.search(name):
                continue
            rect = control.rectangle()
            controls.append({"name": name[:300], "type": str(control.element_info.control_type or ""), "class": str(control.element_info.class_name or ""), "rect": [rect.left, rect.top, rect.right, rect.bottom], "visible": bool(control.is_visible())})
        except Exception:
            continue
    print(json.dumps({"url": session._current_url(window), "controls": controls}, ensure_ascii=False, indent=2))
    session._focus(window)
    session.keyboard.send_keys("^w")


if __name__ == "__main__":
    main()
