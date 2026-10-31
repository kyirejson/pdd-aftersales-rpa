from __future__ import annotations

from argparse import Namespace
from dataclasses import replace
from pathlib import Path
import tempfile

import pytest

from pdd_rpa.connectors.base import ConnectorResult
from pdd_rpa.config import load_config
from pdd_rpa.db import Repository
from pdd_rpa.message_worker import _run
from pdd_rpa.models import ExecutionMode, WorkOrderFacts
from pdd_rpa.orchestrator import Orchestrator


def test_message_worker_returns_connector_result_without_parent_uia(monkeypatch):
    calls = []

    class FakeConnector:
        name = "message.desktop_components"

        def __init__(self, repository, evidence_dir):
            calls.append((repository.path, evidence_dir))

        def send(self, group, message):
            calls.append((group, message))
            return ConnectorResult(
                True,
                {"sent": True, "group": group, "message": message},
                evidence=["evidence.png"],
            )

    monkeypatch.setattr("pdd_rpa.message_worker.DesktopMessageConnector", FakeConnector)
    with tempfile.TemporaryDirectory(dir="runtime") as temporary_dir:
        result = _run(
            {
                "group": {"platform": "yto", "group_name": "圆通专属客服"},
                "message": "YT123+拦截",
            },
            Namespace(
                database=f"{temporary_dir}/rpa.db",
                evidence_dir=f"{temporary_dir}/evidence",
            ),
        )

    assert result == {
        "ok": True,
        "result": {
            "ok": True,
            "data": {
                "sent": True,
                "group": {"platform": "yto", "group_name": "圆通专属客服"},
                "message": "YT123+拦截",
            },
            "evidence": ["evidence.png"],
            "error": None,
            "connector": "message.desktop_components",
        },
    }
    assert calls[-1] == (
        {"platform": "yto", "group_name": "圆通专属客服"},
        "YT123+拦截",
    )


def test_orchestrator_dispatch_uses_isolated_worker_not_parent_message_connector(monkeypatch):
    with tempfile.TemporaryDirectory(dir="runtime") as temporary_dir:
        config = replace(
            load_config(),
            database_path=Path(temporary_dir) / "rpa.db",
            evidence_dir=Path(temporary_dir) / "evidence",
            execution_mode=ExecutionMode.AUTO,
            live_actions_enabled=True,
        )
        repository = Repository(config.database_path)
        orchestrator = Orchestrator(config, repository)
        orchestrator.initialize()
        task_id = repository.upsert_task(
            WorkOrderFacts(
                shop_id="shop_01",
                shop_name="示例书店 A",
                edge_profile="Profile",
                work_order_no="WO-MESSAGE-001",
                order_no="ORDER-MESSAGE-001",
                work_order_title="订单问题：在途无理由退款处理",
                refund_type="退款",
                refund_reason="不想要了",
                pdd_order_status="已发货",
                carrier="圆通",
                tracking_no="YT123456789",
            )
        )

        def fail_if_parent_touches_uia(*_args, **_kwargs):
            raise AssertionError("主后端不应直接发送桌面消息")

        observed = {}
        isolated_calls = []

        def isolated_send(**kwargs):
            isolated_calls.append(dict(kwargs))
            observed.update(kwargs)
            return {
                "ok": True,
                "connector": "message.desktop_components",
                "data": {
                    "sent": True,
                    "sent_echo": "YT123456789+拦截",
                },
                "evidence": ["message-evidence.png"],
                "error": None,
            }

        monkeypatch.setattr(orchestrator.messages, "send", fail_if_parent_touches_uia)
        monkeypatch.setattr("pdd_rpa.orchestrator.run_isolated_message_send", isolated_send)

        result = orchestrator.dispatch_in_transit_interception(
            task_id,
            {
                "ok": True,
                "tracking_no": "YT123456789",
                "message": "YT123456789+拦截",
                "group": {"platform": "yto", "group_name": "圆通专属客服"},
            },
        )
        reused = orchestrator.dispatch_in_transit_interception(
            task_id,
            {
                "ok": True,
                "tracking_no": "YT123456789",
                "message": "YT123456789+拦截",
                "group": {"platform": "yto", "group_name": "圆通专属客服"},
            },
        )
        persisted = repository.get_task(task_id)["facts"]
        action_key = orchestrator.group_dispatch.task_action_key(
            task_id,
            {"platform": "yto", "group_name": "圆通专属客服"},
            "YT123456789",
        )
        dispatch_log = repository.get_action_log(action_key)

    assert observed["database_path"] == config.database_path
    assert observed["group"] == {"platform": "yto", "group_name": "圆通专属客服"}
    assert len(isolated_calls) == 1
    assert result["send"]["sent"] is True
    assert result["reused"] is False
    assert reused["reused"] is True
    assert reused["send"]["sent_echo"] == "YT123456789+拦截"
    assert persisted["source_evidence"]["interception_message"] == {
        "platform": "yto",
        "account_alias": "",
        "group_name": "圆通专属客服",
        "tracking_no": "YT123456789",
        "message": "YT123456789+拦截",
        "sent_echo": "YT123456789+拦截",
        "screenshots": ["message-evidence.png"],
    }
    assert dispatch_log["status"] == "success"


def test_orchestrator_dispatch_fails_closed_on_incomplete_task_audit(monkeypatch):
    with tempfile.TemporaryDirectory(dir="runtime") as temporary_dir:
        config = replace(
            load_config(),
            database_path=Path(temporary_dir) / "rpa.db",
            evidence_dir=Path(temporary_dir) / "evidence",
            execution_mode=ExecutionMode.AUTO,
            live_actions_enabled=True,
        )
        repository = Repository(config.database_path)
        orchestrator = Orchestrator(config, repository)
        orchestrator.initialize()
        task_id = repository.upsert_task(
            WorkOrderFacts(
                shop_id="shop_01",
                shop_name="示例书店 A",
                edge_profile="Profile",
                work_order_no="WO-MESSAGE-INCOMPLETE",
                order_no="ORDER-MESSAGE-INCOMPLETE",
                work_order_title="订单问题：在途无理由退款处理",
                refund_type="退款",
                refund_reason="不想要了",
                pdd_order_status="已发货",
                carrier="圆通",
                tracking_no="YT-INCOMPLETE",
            )
        )
        group = {"platform": "yto", "group_name": "圆通专属客服"}
        message = "YT-INCOMPLETE+拦截"
        assert orchestrator.group_dispatch.record_dispatched(
            task_id,
            group,
            "YT-INCOMPLETE",
            message,
            {"sent": True, "evidence": ["incomplete.png"]},
        )

        def fail_if_resend_attempted(**_kwargs):
            raise AssertionError("不完整审计存在时不得再次外发")

        monkeypatch.setattr(
            "pdd_rpa.orchestrator.run_isolated_message_send",
            fail_if_resend_attempted,
        )

        with pytest.raises(RuntimeError, match="sent_echo"):
            orchestrator.dispatch_in_transit_interception(
                task_id,
                {
                    "ok": True,
                    "tracking_no": "YT-INCOMPLETE",
                    "message": message,
                    "group": group,
                },
            )


def test_orchestrator_dispatch_fails_closed_when_retry_route_changed(monkeypatch):
    with tempfile.TemporaryDirectory(dir="runtime") as temporary_dir:
        config = replace(
            load_config(),
            database_path=Path(temporary_dir) / "rpa.db",
            evidence_dir=Path(temporary_dir) / "evidence",
            execution_mode=ExecutionMode.AUTO,
            live_actions_enabled=True,
        )
        repository = Repository(config.database_path)
        orchestrator = Orchestrator(config, repository)
        orchestrator.initialize()
        task_id = repository.upsert_task(
            WorkOrderFacts(
                shop_id="shop_01",
                shop_name="示例书店 A",
                edge_profile="Profile",
                work_order_no="WO-MESSAGE-ROUTE-CHANGED",
                order_no="ORDER-MESSAGE-ROUTE-CHANGED",
                work_order_title="订单问题：在途无理由退款处理",
                refund_type="退款",
                refund_reason="不想要了",
                pdd_order_status="已发货",
                carrier="圆通",
                tracking_no="YT-OLD",
            )
        )
        old_group = {
            "platform": "企业微信",
            "account_alias": "客服A",
            "group_name": "原拦截群",
        }
        assert orchestrator.group_dispatch.record_dispatched(
            task_id,
            old_group,
            "YT-OLD",
            "YT-OLD+拦截",
            {
                "sent": True,
                "sent_echo": "YT-OLD+拦截",
                "evidence": ["old-route.png"],
            },
        )

        def fail_if_resend_attempted(**_kwargs):
            raise AssertionError("路由变化后不得把旧发送隐藏并再次外发")

        monkeypatch.setattr(
            "pdd_rpa.orchestrator.run_isolated_message_send",
            fail_if_resend_attempted,
        )

        with pytest.raises(RuntimeError, match="task/target/tracking/message"):
            orchestrator.dispatch_in_transit_interception(
                task_id,
                {
                    "ok": True,
                    "tracking_no": "YT-NEW",
                    "message": "YT-NEW+拦截",
                    "group": {
                        "platform": "微信",
                        "account_alias": "客服B",
                        "group_name": "新拦截群",
                    },
                },
            )
