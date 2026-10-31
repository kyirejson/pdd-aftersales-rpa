import json

from pdd_rpa.aftersale_workbench import AFTERSALE_SHIPPED_REFUND
from pdd_rpa.db import LEGACY_AFTERSALE_GT3_MIGRATION_KEY, Repository
from pdd_rpa.models import Shop, TaskState, WorkOrderFacts


def _task(repo: Repository, suffix: str) -> int:
    return repo.upsert_task(
        WorkOrderFacts(
            shop_id="shop_01",
            shop_name="测试店铺",
            edge_profile="Profile",
            work_order_no=f"AFTERSALE:{suffix}",
            order_no=f"ORDER:{suffix}",
            work_order_title=AFTERSALE_SHIPPED_REFUND,
            refund_type="退款",
            pdd_order_status="已发货",
            source_evidence={
                "source_system": "aftersale_workbench",
                "queue_scope": "pending_soon_overdue",
            },
        )
    )


def test_initialize_reopens_only_exact_legacy_gt3_manual_rows_once(tmp_path):
    repo = Repository(tmp_path / "rpa.db")
    repo.initialize()
    repo.seed_shops((Shop("shop_01", "测试店铺", "Profile"),))

    eligible_id = _task(repo, "ELIGIBLE")
    repo.mark_task_status(
        eligible_id,
        TaskState.NEED_REVIEW,
        "已发货普通退款完整物流轨迹共 9 条，超过三条分支尚未启用",
    )

    unrelated_id = _task(repo, "UNRELATED")
    repo.mark_task_status(
        unrelated_id,
        TaskState.NEED_REVIEW,
        "当前售后动作控件未能唯一读取",
    )

    attempted_id = _task(repo, "ATTEMPTED")
    repo.mark_task_status(
        attempted_id,
        TaskState.NEED_REVIEW,
        "已发货普通退款完整物流轨迹共 12 条，超过三条分支尚未启用",
    )
    repo.log_action(
        attempted_id,
        "test:attempted-final-action",
        "AFTERSALE_FINAL_ACTION",
        "uncertain",
        {
            "action": "REJECT_REFUND",
            "business_write_performed": True,
        },
    )

    # Simulate a database produced before this versioned migration existed.
    with repo.connect() as conn:
        conn.execute(
            "DELETE FROM system_settings WHERE key=?",
            (LEGACY_AFTERSALE_GT3_MIGRATION_KEY,),
        )

    repo.initialize()

    assert repo.get_task(eligible_id)["state"] == TaskState.DISCOVERED
    assert repo.get_task(unrelated_id)["state"] == TaskState.NEED_REVIEW
    assert repo.get_task(attempted_id)["state"] == TaskState.NEED_REVIEW
    recovery_logs = [
        item
        for item in repo.list_logs(task_id=eligible_id)
        if item["details"].get("recovery_trigger")
        == LEGACY_AFTERSALE_GT3_MIGRATION_KEY
    ]
    assert len(recovery_logs) == 1
    assert recovery_logs[0]["details"]["from"] == TaskState.NEED_REVIEW
    assert recovery_logs[0]["details"]["to"] == TaskState.DISCOVERED

    with repo.connect() as conn:
        setting = conn.execute(
            "SELECT value FROM system_settings WHERE key=?",
            (LEGACY_AFTERSALE_GT3_MIGRATION_KEY,),
        ).fetchone()
    applied = json.loads(setting["value"])
    assert applied["reopened_task_ids"] == [eligible_id]
    assert applied["reopened_count"] == 1

    repo.initialize()
    repeated_logs = [
        item
        for item in repo.list_logs(task_id=eligible_id)
        if item["details"].get("recovery_trigger")
        == LEGACY_AFTERSALE_GT3_MIGRATION_KEY
    ]
    assert len(repeated_logs) == 1

