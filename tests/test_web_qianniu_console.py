from __future__ import annotations

from dataclasses import replace
import importlib
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

import pdd_rpa.config as config_module
from pdd_rpa.models import TaskState, WorkOrderFacts


@pytest.fixture()
def web_module(tmp_path, monkeypatch):
    config = replace(
        config_module.load_config(),
        database_path=tmp_path / "web.db",
        evidence_dir=tmp_path / "evidence",
    )
    monkeypatch.setattr(config_module, "load_config", lambda path=None: config)
    sys.modules.pop("pdd_rpa.web", None)
    module = importlib.import_module("pdd_rpa.web")
    try:
        yield module
    finally:
        sys.modules.pop("pdd_rpa.web", None)


def _add_qianniu_task(web_module, *, work_order_no: str = "405273961532043549") -> int:
    return web_module.repository.upsert_task(
        WorkOrderFacts(
            shop_id="qianniu_01",
            shop_name="示例天猫旗舰店",
            edge_profile="qianniu_edge",
            work_order_no=work_order_no,
            order_no="5127217455017018107",
            work_order_title="千牛未收货退款",
            refund_type="未收货退款",
            refund_reason="不想要了",
            refund_amount=45.94,
            pdd_order_status="已发货",
            carrier="圆通速递",
            wdt_order_status="已发货",
            wdt_warehouse="示例供应链代发总仓",
            source_evidence={
                "source_system": "qianniu_aftersale",
                "platform": "tmall",
                "queue_scope": "unreceived_refund_pending",
                "aftersale_no": work_order_no,
                "aftersale_status": "退款待处理",
                "available_actions": ["同意退款", "拦截快递", "查看详情"],
                "automation_eligible": True,
                "shop_scan_order": 11,
                "page_no": 1,
                "page_index": 3,
            },
        )
    )


def _authenticated_client(web_module) -> TestClient:
    client = TestClient(web_module.app)
    client.cookies.set(
        web_module.CONSOLE_AUTH_COOKIE,
        web_module.auth_manager.issue_session(),
    )
    return client


def test_tasks_api_tags_qianniu_kind(web_module):
    task_id = _add_qianniu_task(web_module)
    client = _authenticated_client(web_module)

    tasks = client.get("/api/tasks?view=pending").json()
    matched = [task for task in tasks if task["id"] == task_id]
    assert len(matched) == 1
    task = matched[0]
    assert task["task_kind"] == "qianniu"
    assert task["task_source_label"] == "千牛未收货退款"
    assert task["queue_scope"] == "unreceived_refund_pending"
    assert task["facts"]["shop_name"] == "示例天猫旗舰店"
    assert task["facts"]["refund_amount"] == pytest.approx(45.94)


def test_task_detail_api_tags_qianniu_kind(web_module):
    task_id = _add_qianniu_task(web_module)
    client = _authenticated_client(web_module)

    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["task_kind"] == "qianniu"
    assert detail["task_source_label"] == "千牛未收货退款"
    evidence = detail["facts"]["source_evidence"]
    assert evidence["source_system"] == "qianniu_aftersale"
    assert evidence["platform"] == "tmall"


def test_dashboard_reports_qianniu_shop_and_count(web_module):
    _add_qianniu_task(web_module)
    client = _authenticated_client(web_module)

    data = client.get("/api/dashboard").json()
    assert data["current_qianniu_count"] == 1
    assert data["current_targets"]["qianniu_unreceived_refund"] == 1
    shop = data["qianniu_shop"]
    assert shop["id"] == "qianniu_01"
    assert shop["platform"] == "tmall"
    assert shop["name"]


def test_qianniu_register_is_labeled_tmall_with_shop(web_module):
    task_id = _add_qianniu_task(web_module)
    web_module.repository.upsert_interception_register(
        task_id=task_id,
        record_date="2026-08-31",
        carrier="圆通快递",
        tracking_no="YT7968643252039",
        order_no="5127217455017018107",
        refund_reason="天猫未收货退款-已揽收五条以内",
        amount=45.94,
        warehouse="示例自营仓",
        group_name="【快递】示例团队&圆通对接群",
    )
    client = _authenticated_client(web_module)

    records = client.get("/api/interception-registers").json()

    assert len(records) == 1
    assert records[0]["task_id"] == task_id
    assert records[0]["platform"] == "tmall"
    assert records[0]["platform_label"] == "天猫"
    assert records[0]["shop_name"] == "示例天猫旗舰店"


def test_processed_history_and_export_are_not_limited_to_latest_200(web_module):
    for index in range(215):
        task_id = _add_qianniu_task(
            web_module,
            work_order_no=f"405273961532{index:06d}",
        )
        web_module.repository.mark_task_status(
            task_id,
            TaskState.COMPLETED,
            "测试完成",
        )
    client = _authenticated_client(web_module)

    first_page = client.get(
        "/api/tasks/processed-history?page=1&page_size=100"
    ).json()
    last_page = client.get(
        "/api/tasks/processed-history?page=3&page_size=100"
    ).json()

    assert first_page["total"] == 215
    assert first_page["pages"] == 3
    assert len(first_page["items"]) == 100
    assert len(last_page["items"]) == 15

    exported = client.get("/api/tasks/processed-export")
    assert exported.status_code == 200
    assert exported.content.startswith(b"\xef\xbb\xbf")
    assert len(exported.content.decode("utf-8-sig").splitlines()) == 216


def test_console_static_files_include_complete_qianniu_workspace():
    static_dir = Path(__file__).resolve().parents[1] / "pdd_rpa" / "static"
    index = (static_dir / "index.html").read_text(encoding="utf-8")
    app = (static_dir / "app.js").read_text(encoding="utf-8")
    styles = (static_dir / "styles.css").read_text(encoding="utf-8")

    assert 'data-source="qianniu"' in index
    assert 'id="task-source-qianniu-count"' in index
    assert "function renderQianniuRow(task)" in app
    assert "function renderQianniuDetail(task)" in app
    assert "? renderQianniuDetail(task)" in app
    assert "if (isQianniuTask(task))" in app
    assert "平台最终反馈" in app
    assert "/api/tasks/processed-history" in app
    assert "/api/tasks/processed-export" in app
    assert 'id="processed-date-from"' in index
    assert 'id="processed-date-to"' in index
    assert ".task-source-badge.qianniu" in styles
    assert ".detail-summary-card.qianniu" in styles
