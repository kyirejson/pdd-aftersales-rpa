from dataclasses import replace

import pytest

from pdd_rpa.aftersale_workbench import (
    AFTERSALE_SHIPPED_REFUND,
    AGREE_REFUND,
    REJECT_REFUND,
)
from pdd_rpa.config import load_config
from pdd_rpa.db import Repository
from pdd_rpa.models import WorkOrderFacts
from pdd_rpa.orchestrator import Orchestrator


def _runtime(tmp_path):
    config = replace(load_config(), database_path=tmp_path / "rpa.db")
    repository = Repository(config.database_path)
    orchestrator = Orchestrator(config, repository)
    orchestrator.initialize()
    return repository, orchestrator


def _shipped_refund_task(repository: Repository, orchestrator: Orchestrator) -> int:
    shop = orchestrator.config.shops[0]
    task_id = repository.upsert_task(
        WorkOrderFacts(
            shop_id=shop.id,
            shop_name=shop.name,
            edge_profile=shop.edge_profile,
            work_order_no="AFTERSALE:SAFETY",
            order_no="ORDER:SAFETY",
            work_order_title=AFTERSALE_SHIPPED_REFUND,
            refund_type="退款",
            refund_amount=28.8,
            pdd_order_status="已发货",
            source_evidence={"source_system": "aftersale_workbench"},
        )
    )
    return task_id


def test_legacy_daily_refund_limit_is_ignored(tmp_path):
    repository, orchestrator = _runtime(tmp_path)
    task_id = _shipped_refund_task(repository, orchestrator)
    # A migrated settings file may still contain the retired field.  It must
    # no longer participate in readiness or execution safety decisions.
    orchestrator.config.raw.setdefault("safety", {})[
        "max_daily_refund_amount"
    ] = 0.01

    result = orchestrator.validate_aftersale_shipped_refund_safety(
        task_id,
        action=AGREE_REFUND,
        live=False,
    )

    assert result["refund_amount"] == 28.8
    assert "max_daily_refund_amount" not in result
    assert "daily_total_before" not in result


def test_legacy_daily_refund_limit_is_ignored_by_direct_refund_execution(
    tmp_path, monkeypatch
):
    repository, orchestrator = _runtime(tmp_path)
    shop = orchestrator.config.shops[0]
    task_id = repository.upsert_task(
        WorkOrderFacts(
            shop_id=shop.id,
            shop_name=shop.name,
            edge_profile=shop.edge_profile,
            work_order_no="DIRECT:SAFETY",
            order_no="ORDER:DIRECT:SAFETY",
            work_order_title="已发货无轨迹退款处理",
            refund_type="仅退款",
            refund_amount=28.8,
            pdd_order_status="已发货",
        )
    )
    orchestrator.config.raw.setdefault("safety", {})[
        "max_daily_refund_amount"
    ] = 0.01
    monkeypatch.setattr(
        "pdd_rpa.orchestrator.assert_action_allowed",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_isolated_pdd_refund_stage",
        lambda *args, **kwargs: {
            "result": {"ok": True, "business_write_performed": False},
            "preview": {},
            "detail_cleanup": {},
        },
    )

    result = orchestrator.execute_refund_pilot(
        task_id,
        "ORDER:DIRECT:SAFETY",
        28.8,
    )

    assert result["ok"] is True
    assert "max_daily_refund_amount" not in result["safety"]
    assert "daily_total_before" not in result["safety"]


def test_unknown_aftersale_business_write_blocks_automatic_repeat(tmp_path):
    repository, orchestrator = _runtime(tmp_path)
    task_id = _shipped_refund_task(repository, orchestrator)
    repository.log_action(
        task_id,
        "test:uncertain-aftersale-write",
        "AFTERSALE_FINAL_ACTION",
        "uncertain",
        {
            "action": AGREE_REFUND,
            "refund_amount": 28.8,
            "business_write_performed": True,
        },
    )

    with pytest.raises(RuntimeError, match="已有真实业务写入记录"):
        orchestrator.validate_aftersale_shipped_refund_safety(
            task_id,
            action=AGREE_REFUND,
            live=False,
        )


def test_reject_attempt_before_final_consumer_confirmation_can_resume(tmp_path):
    repository, orchestrator = _runtime(tmp_path)
    task_id = _shipped_refund_task(repository, orchestrator)
    repository.log_action(
        task_id,
        "test:reject-preconfirmation",
        "AFTERSALE_FINAL_ACTION",
        "uncertain",
        {
            "action": REJECT_REFUND,
            "business_write_performed": True,
            "result": {
                "reason_code": "PDD_REJECT_NEGOTIATION_OUTCOME_UNKNOWN",
                "write_outcome_unknown": True,
                "business_write_performed": True,
                "submit_attempted": True,
                "trace": [
                    {"text": "坚持驳回"},
                    {"text": "退货后退全款"},
                    {"text": "提交协商方案"},
                ],
            },
        },
    )

    result = orchestrator.validate_aftersale_shipped_refund_safety(
        task_id,
        action=REJECT_REFUND,
        live=False,
    )

    assert result["corrected_preconfirmation_attempts"] == 1


def test_verified_reject_negotiation_stage_allows_the_next_round(tmp_path):
    repository, orchestrator = _runtime(tmp_path)
    task_id = _shipped_refund_task(repository, orchestrator)
    facts = dict(repository.get_task(task_id)["facts"])
    facts["aftersale_no"] = "AS-STAGE"
    repository.update_facts(task_id, facts)
    repository.log_action(
        task_id,
        "test:verified-reject-stage",
        "AFTERSALE_FINAL_ACTION",
        "deferred",
        {
            "action": REJECT_REFUND,
            "business_write_performed": True,
            "result": {
                "reason_code": "PDD_REJECT_NEGOTIATION_COOLDOWN",
                "defer_kind": "POST_SUBMIT_VERIFIED",
                "stage_committed": True,
                "negotiation_round_submitted": True,
                "write_outcome_unknown": False,
                "business_write_performed": True,
                "submit_attempted": True,
            },
        },
    )
    repository.defer_task_retry(
        task_id,
        "2026-08-25T12:30:00",
        "PDD_REJECT_NEGOTIATION_COOLDOWN",
        "待倒计时结束后可继续处理驳回",
        {
            "business_write_performed": True,
            "submit_attempted": True,
            "defer_kind": "POST_SUBMIT_VERIFIED",
            "stage_committed": True,
            "negotiation_round_submitted": True,
            "resume_action": REJECT_REFUND,
            "reject_surface": "KEEP_REJECT",
            "shop_id": facts["shop_id"],
            "order_no": facts["order_no"],
            "aftersale_no": "AS-STAGE",
        },
    )

    result = orchestrator.validate_aftersale_shipped_refund_safety(
        task_id,
        action=REJECT_REFUND,
        live=False,
    )

    assert result["verified_negotiation_rounds"] == 1
