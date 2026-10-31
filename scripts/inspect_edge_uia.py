from __future__ import annotations

import json
import re

from pdd_rpa.config import load_config
from pdd_rpa.connectors.pdd_edge_readonly import WindowsEdgeReadOnlySession, classify_pdd_url


PATTERN = re.compile(r".")


def main() -> None:
    config = load_config()
    session = WindowsEdgeReadOnlySession(config.raw["pdd_readonly"])
    results = []
    for window in session._windows():
        url = session._current_url(window)
        if classify_pdd_url(url) not in {"list", "detail"}:
            continue
        title = window.window_text()
        if not any(shop.name in title for shop in config.shops):
            continue
        ui_window = session.Desktop(backend="uia").window(handle=int(window.handle))
        controls = []
        for control in ui_window.descendants():
            try:
                name = str(control.element_info.name or "").strip()
                rect = control.rectangle()
                if not name or not PATTERN.search(name) or not (200 <= rect.left <= 1250 and 420 <= rect.top <= 650):
                    continue
                controls.append(
                    {
                        "name": name[:240],
                        "control_type": str(control.element_info.control_type or ""),
                        "automation_id": str(control.element_info.automation_id or ""),
                        "class_name": str(control.element_info.class_name or ""),
                        "rect": [rect.left, rect.top, rect.right, rect.bottom],
                        "enabled": bool(control.is_enabled()),
                        "visible": bool(control.is_visible()),
                    }
                )
            except Exception:
                continue
        results.append({"title": title, "url": url, "controls": controls})
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
