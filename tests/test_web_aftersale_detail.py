from __future__ import annotations

from dataclasses import replace
import importlib
import sys
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

import pdd_rpa.config as config_module
from pdd_rpa.models import WorkOrderFacts


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


def _add_task(
    web_module,
    *,
    aftersale: bool,
    queue_scope: str = "pending_soon_overdue",
) -> int:
    shop = web_module.config.shops[0]
    scope_suffix = queue_scope.replace("_", "-").upper()
    return web_module.repository.upsert_task(
        WorkOrderFacts(
            shop_id=shop.id,
            shop_name=shop.name,
            edge_profile=shop.edge_profile,
            work_order_no=(
                f"AFTERSALE:{scope_suffix}"
                if aftersale
                else "WORK-ORDER-001"
            ),
            order_no=(
                f"AFTERSALE-ORDER-{scope_suffix}"
                if aftersale
                else "WORK-ORDER-ORDER-001"
            ),
            work_order_title=(
                "售后工作台：退货退款" if aftersale else "已发货无轨迹退款处理"
            ),
            refund_type="退货退款" if aftersale else "仅退款",
            source_evidence={
                "collection_mode": "edge_uia",
                **(
                    {
                        "source_system": "aftersale_workbench",
                        "queue_scope": queue_scope,
                    }
                    if aftersale
                    else {}
                ),
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


def test_aftersale_detail_readonly_endpoint_requires_console_login(web_module) -> None:
    task_id = _add_task(web_module, aftersale=True)

    response = TestClient(web_module.app).post(
        f"/api/tasks/{task_id}/collect-aftersale-detail-readonly"
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "控制台登录已失效，请重新登录"


def test_aftersale_detail_readonly_endpoint_authorizes_source_and_closes_detail(
    web_module,
    monkeypatch,
) -> None:
    aftersale_task_id = _add_task(web_module, aftersale=True)
    work_order_task_id = _add_task(web_module, aftersale=False)
    other_aftersale_task_id = _add_task(
        web_module,
        aftersale=True,
        queue_scope="other_aftersale_queue",
    )
    collect = Mock(
        return_value={
            "ok": True,
            "facts": {"order_no": "AFTERSALE-ORDER-001"},
            "decision": {"action": "NEED_REVIEW"},
            "task": {"id": aftersale_task_id},
            "detail_cleanup": {"handled": True, "list_ready": True},
            "detail_retained": False,
            "business_write_performed": False,
        }
    )
    monkeypatch.setattr(web_module.orchestrator, "collect_aftersale_detail", collect)
    client = _authenticated_client(web_module)

    rejected = client.post(
        f"/api/tasks/{work_order_task_id}/collect-aftersale-detail-readonly"
    )
    rejected_scope = client.post(
        f"/api/tasks/{other_aftersale_task_id}/collect-aftersale-detail-readonly"
    )
    missing = client.post("/api/tasks/999999/collect-aftersale-detail-readonly")
    accepted = client.post(
        f"/api/tasks/{aftersale_task_id}/collect-aftersale-detail-readonly"
    )
    detail = client.get(f"/api/tasks/{aftersale_task_id}")

    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "当前任务不是售后工作台任务"
    assert rejected_scope.status_code == 409
    assert rejected_scope.json()["detail"] == "仅支持售后工作台“待处理即将逾期”任务的详情读取"
    assert missing.status_code == 404
    assert missing.json()["detail"] == "任务不存在"
    assert accepted.status_code == 200
    assert accepted.json()["business_write_performed"] is False
    assert accepted.json()["detail_retained"] is False
    assert detail.status_code == 200
    assert detail.json()["task_kind"] == "aftersale"
    assert detail.json()["task_source_label"] == "售后工作台"
    assert detail.json()["queue_scope"] == "pending_soon_overdue"
    collect.assert_called_once_with(aftersale_task_id, keep_detail_open=False)


def test_aftersale_detail_readonly_endpoint_maps_worker_failure_to_conflict(
    web_module,
    monkeypatch,
) -> None:
    task_id = _add_task(web_module, aftersale=True)
    monkeypatch.setattr(
        web_module.orchestrator,
        "collect_aftersale_detail",
        Mock(side_effect=RuntimeError("售后详情页未能安全返回待处理即将逾期列表")),
    )

    response = _authenticated_client(web_module).post(
        f"/api/tasks/{task_id}/collect-aftersale-detail-readonly"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "售后详情页未能安全返回待处理即将逾期列表"
