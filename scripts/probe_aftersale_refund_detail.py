from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import time
from typing import Any

from pdd_rpa.aftersale_uia import PddAftersaleUiaSession
from pdd_rpa.config import load_config
from pdd_rpa.db import Repository


KEYWORD_RE = re.compile(
    r"物流|快递|签收|揽收|运输|派送|到达|发出|退回|退款|驳回|协商|查看全部|轨迹"
)

TIMESTAMP_RE = re.compile(
    r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?"
)

LOGISTICS_NON_EVENT_TEXTS = {
    "发货物流公司:",
    "发货物流公司：",
    "发货快递单号:",
    "发货快递单号：",
    "查看全部",
    "去修改物流信息",
}


def _control_record(session: PddAftersaleUiaSession, control: Any) -> dict[str, Any]:
    rect = session._rect(control)
    return {
        "name": session._name(control),
        "control_type": str(control.element_info.control_type or ""),
        "class_name": str(control.element_info.class_name or ""),
        "rect": list(rect),
    }


def _interesting_controls(
    session: PddAftersaleUiaSession, handle: int
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[int, int, int, int]]] = set()
    for control in session._controls(handle):
        try:
            record = _control_record(session, control)
        except Exception:
            continue
        name = str(record["name"] or "").strip()
        if not name or not KEYWORD_RE.search(name):
            continue
        key = (
            name,
            str(record["control_type"]),
            tuple(int(item) for item in record["rect"]),
        )
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
    records.sort(key=lambda item: (item["rect"][1], item["rect"][0], item["name"]))
    return records


def _action_control_diagnostics(
    session: PddAftersaleUiaSession, handle: int
) -> list[dict[str, Any]]:
    """Describe exact action labels and their UIA ancestor/pattern chain.

    This remains read-only: interface objects are queried but never invoked.
    Chromium currently exposes some underlined business actions as Text, so
    the control type alone is not enough to diagnose whether a safe UIA
    operation is available.
    """

    expected = {
        session._normalized_action_text(value)
        for value in ("同意退款", "同意快递退回后退款", "驳回退款")
    }
    result: list[dict[str, Any]] = []
    for label in session._controls(handle):
        try:
            if session._normalized_action_text(session._name(label)) not in expected:
                continue
        except Exception:
            continue
        chain: list[dict[str, Any]] = []
        current = label
        seen: set[tuple[Any, ...]] = set()
        for depth in range(6):
            if current is None:
                break
            try:
                signature = session._control_runtime_signature(current)
                if signature in seen:
                    break
                seen.add(signature)
                record = _control_record(session, current)
                record["depth"] = depth
                record["enabled"] = bool(current.is_enabled())
                record["visible"] = bool(current.is_visible())
                try:
                    current.iface_invoke
                    record["supports_invoke"] = True
                except Exception:
                    record["supports_invoke"] = False
                try:
                    current.iface_legacy_iaccessible
                    record["supports_legacy_iaccessible"] = True
                except Exception:
                    record["supports_legacy_iaccessible"] = False
                chain.append(record)
                current = current.parent()
            except Exception as exc:
                chain.append({"depth": depth, "error": str(exc)})
                break
        result.append({"label": session._name(label), "chain": chain})
    return result


def _status_control_diagnostics(
    session: PddAftersaleUiaSession, handle: int
) -> list[dict[str, Any]]:
    """Describe visible pending/status headings and their ancestor chain."""

    result: list[dict[str, Any]] = []
    for label in session._controls(handle):
        try:
            name = session._name(label).strip()
            if not name or not any(
                marker in name
                for marker in ("待商家处理", "待卖家处理", "售后状态", "处理状态")
            ):
                continue
        except Exception:
            continue
        chain: list[dict[str, Any]] = []
        current = label
        seen: set[tuple[Any, ...]] = set()
        for depth in range(6):
            if current is None:
                break
            try:
                signature = session._control_runtime_signature(current)
                if signature in seen:
                    break
                seen.add(signature)
                record = _control_record(session, current)
                record["depth"] = depth
                record["enabled"] = bool(current.is_enabled())
                record["visible"] = bool(current.is_visible())
                chain.append(record)
                current = current.parent()
            except Exception as exc:
                chain.append({"depth": depth, "error": str(exc)})
                break
        result.append({"label": session._name(label), "chain": chain})
    return result


def _view_all_targets(session: PddAftersaleUiaSession, handle: int) -> list[Any]:
    logistics_headers: list[Any] = []
    for control in session._controls(handle):
        try:
            if session._name(control).strip() == "物流轨迹":
                logistics_headers.append(control)
        except Exception:
            continue
    if len(logistics_headers) != 1:
        return []
    header_rect = session._rect(logistics_headers[0])
    matches: list[Any] = []
    for control in session._controls(handle):
        try:
            name = session._name(control).strip()
            rect = session._rect(control)
        except Exception:
            continue
        if (
            (name == "查看全部" or name.startswith("查看全部物流"))
            and rect[1] > header_rect[1]
            and header_rect[0] - 80 <= rect[0] <= header_rect[0] + 520
        ):
            matches.append(control)
    unique: dict[tuple[int, int, int, int], Any] = {}
    for control in matches:
        try:
            unique[session._rect(control)] = control
        except Exception:
            continue
    return [unique[key] for key in sorted(unique, key=lambda rect: (rect[1], rect[0]))]


def _logistics_section(
    session: PddAftersaleUiaSession, handle: int
) -> dict[str, Any]:
    controls = session._controls(handle)
    headers = []
    for control in controls:
        try:
            if session._name(control).strip() == "物流轨迹":
                headers.append(control)
        except Exception:
            continue
    if len(headers) != 1:
        return {
            "ready": False,
            "error": f"物流轨迹标题不唯一：{len(headers)}",
            "trace_count": 0,
            "events": [],
        }
    header_rect = session._rect(headers[0])
    next_section_tops: list[int] = []
    for control in controls:
        try:
            name = session._name(control).strip().rstrip("：:")
            rect = session._rect(control)
        except Exception:
            continue
        if (
            name in {"备注", "订单备注"}
            and rect[1] > header_rect[1]
            and 300 <= rect[0] <= 850
        ):
            next_section_tops.append(rect[1])
    section_bottom = min(next_section_tops) if next_section_tops else 10**9
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[int, int, int, int]]] = set()
    for control in controls:
        try:
            record = _control_record(session, control)
        except Exception:
            continue
        name = str(record["name"] or "").strip()
        rect = tuple(int(item) for item in record["rect"])
        if (
            not name
            or rect[1] <= header_rect[1]
            or rect[1] >= section_bottom
            or rect[0] < 320
            or rect[0] > 850
        ):
            continue
        key = (name, rect)
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
    records.sort(key=lambda item: (item["rect"][1], item["rect"][0], item["name"]))
    timestamps: list[str] = []
    for record in records:
        for match in TIMESTAMP_RE.finditer(str(record["name"])):
            timestamps.append(match.group(0))
    timestamps = list(dict.fromkeys(timestamps))
    event_texts = [
        str(record["name"])
        for record in records
        if not TIMESTAMP_RE.search(str(record["name"]))
        and str(record["name"]) not in LOGISTICS_NON_EVENT_TEXTS
    ]
    event_texts = list(dict.fromkeys(event_texts))

    # PDD renders each tracking event immediately above its timestamp.  Pair
    # by geometry and then choose the greatest timestamp, rather than assuming
    # the first text in the logistics section is the latest event.  The latter
    # is wrong for transferred orders because carrier/segment headings precede
    # the actual tracking rows.
    event_records = [
        record
        for record in records
        if not TIMESTAMP_RE.search(str(record["name"]))
        and str(record["name"]) not in LOGISTICS_NON_EVENT_TEXTS
        and not re.fullmatch(r"[\d\s-]{8,}", str(record["name"]))
        and "订单分" not in str(record["name"])
        and not str(record["name"]).endswith("：")
        and not str(record["name"]).endswith(":")
    ]
    trace_rows: list[dict[str, str]] = []
    for timestamp_record in records:
        timestamp_match = TIMESTAMP_RE.search(str(timestamp_record["name"]))
        if not timestamp_match:
            continue
        timestamp_top = int(timestamp_record["rect"][1])
        timestamp_left = int(timestamp_record["rect"][0])
        candidates = [
            record
            for record in event_records
            if 0 < timestamp_top - int(record["rect"][1]) <= 120
            and abs(int(record["rect"][0]) - timestamp_left) <= 180
        ]
        if not candidates:
            continue
        event_record = max(candidates, key=lambda item: int(item["rect"][1]))
        trace_rows.append(
            {
                "timestamp": timestamp_match.group(0),
                "event": str(event_record["name"]),
            }
        )
    unique_trace_rows = {
        (row["timestamp"], row["event"]): row for row in trace_rows
    }
    trace_rows = list(unique_trace_rows.values())
    trace_rows.sort(key=lambda row: row["timestamp"], reverse=True)
    latest_event = trace_rows[0]["event"] if trace_rows else ""
    signed = bool(
        re.search(
            r"(?<!未)(?:已签收|签收成功|本人签收|他人代收|驿站代收)",
            latest_event,
        )
    )
    return {
        "ready": bool(timestamps),
        "header_rect": list(header_rect),
        "section_bottom": None if section_bottom == 10**9 else section_bottom,
        "trace_count": len(timestamps),
        "timestamps": timestamps,
        "events": event_texts,
        "trace_rows": trace_rows,
        "latest_event": latest_event,
        "signed": signed,
        "controls": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="只读探测拼多多普通退款详情及物流轨迹控件")
    parser.add_argument("--task-id", type=int)
    parser.add_argument("--shop-id")
    parser.add_argument("--order-no")
    parser.add_argument("--aftersale-no", default="")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    config = load_config()
    repository = Repository(config.database_path)
    if args.task_id is not None:
        task = repository.get_task(args.task_id)
        if not task:
            raise SystemExit(f"任务不存在：{args.task_id}")
        facts = dict(task.get("facts") or {})
        source_evidence = dict(facts.get("source_evidence") or {})
        if source_evidence.get("source_system") != "aftersale_workbench":
            raise SystemExit("当前任务不是售后工作台任务")
        if str(source_evidence.get("queue_scope") or "") != "pending_soon_overdue":
            raise SystemExit("当前任务不属于待处理即将逾期")
        if str(facts.get("refund_type") or "") != "退款":
            raise SystemExit("当前任务不是普通退款")
        task_id = args.task_id
        shop_id = str(facts.get("shop_id") or "")
        order_no = str(facts.get("order_no") or "").strip()
        card = dict(source_evidence.get("aftersale_card") or {})
        expected_aftersale_no = str(
            facts.get("aftersale_no") or card.get("aftersale_no") or ""
        ).strip()
    else:
        if not args.shop_id or not args.order_no:
            raise SystemExit("未提供 --task-id 时必须同时提供 --shop-id 和 --order-no")
        task_id = None
        shop_id = str(args.shop_id).strip()
        order_no = str(args.order_no).strip()
        expected_aftersale_no = str(args.aftersale_no or "").strip()

    shop = next(
        (
            item
            for item in config.shops
            if item.enabled and item.id == shop_id
        ),
        None,
    )
    if shop is None:
        raise SystemExit("任务店铺不存在或已停用")
    shop_data = {
        "id": shop.id,
        "name": shop.name,
        "edge_profile": shop.edge_profile,
    }
    session = PddAftersaleUiaSession(dict(config.raw.get("pdd_readonly") or {}))
    window = None
    output: dict[str, Any] = {
        "task_id": task_id,
        "shop_id": shop.id,
        "order_no": order_no,
        "expected_aftersale_no": expected_aftersale_no,
        "business_write_performed": False,
        "expanded": False,
        "screenshots": [],
    }
    try:
        window, detail_url = session.open_detail(
            shop_data,
            order_no,
            candidate_index=0,
            search_value=expected_aftersale_no or order_no,
            expected_aftersale_no=expected_aftersale_no,
        )
        detail = session.read_detail(window, order_no, detail_url, shop_data)
        output["detail"] = detail
        output["detail_url"] = detail_url
        output["actual_aftersale_no"] = str(detail.get("aftersale_no") or "")
        handle = int(window.handle)
        output["top_action_control_diagnostics"] = _action_control_diagnostics(
            session, handle
        )
        output["top_status_control_diagnostics"] = _status_control_diagnostics(
            session, handle
        )
        session._focus(window)
        session.keyboard.send_keys("^{END}")
        time.sleep(0.8)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = config.evidence_dir / f"aftersale_refund_probe_{shop.id}_{order_no}_{stamp}"
        bottom_path = Path(f"{base}_bottom.png")
        session.screenshot(handle, bottom_path)
        output["screenshots"].append(str(bottom_path))
        output["before_expand_controls"] = _interesting_controls(session, handle)

        targets = _view_all_targets(session, handle)
        output["view_all_candidates"] = [
            _control_record(session, control) for control in targets
        ]
        if targets:
            expanded_segments = 0
            # A transferred order can contain multiple logistics segments,
            # each with its own 查看全部. Expand the first remaining target and
            # re-resolve the UIA tree after every DOM change so stale wrappers
            # are never reused.
            for _ in range(min(len(targets), 8)):
                remaining = _view_all_targets(session, handle)
                if not remaining:
                    break
                target = remaining[0]
                try:
                    target.iface_scroll_item.ScrollIntoView()
                    time.sleep(0.4)
                except Exception:
                    pass
                if not session._invoke_with_uia(target):
                    raise RuntimeError("物流轨迹“查看全部”不支持安全 UIA Invoke")
                expanded_segments += 1
                time.sleep(0.8)
            output["expanded"] = expanded_segments > 0
            output["expanded_segments"] = expanded_segments
            expanded_path = Path(f"{base}_expanded.png")
            session.screenshot(handle, expanded_path)
            output["screenshots"].append(str(expanded_path))
            output["after_expand_controls"] = _interesting_controls(session, handle)
            output["logistics"] = _logistics_section(session, handle)
        else:
            output["probe_error"] = "没有找到物流轨迹查看全部控件"
    finally:
        if window is not None:
            try:
                output["detail_cleanup"] = session.close_detail(window, shop_data)
            except Exception as exc:
                output["detail_cleanup_error"] = str(exc)
        if args.compact:
            output.pop("before_expand_controls", None)
            output.pop("after_expand_controls", None)
            logistics = output.get("logistics")
            if isinstance(logistics, dict):
                logistics.pop("controls", None)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
