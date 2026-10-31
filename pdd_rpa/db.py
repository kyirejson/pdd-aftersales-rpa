from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .dingtalk_interception_register import (
    DINGTALK_TARGET_JUNFU,
    DINGTALK_TARGET_SIKU_DISTRIBUTION,
    dingtalk_target_key_for_warehouse,
)
from .aftersale_workbench import AFTERSALE_RETURN_REFUND
from .models import RuleDecision, Shop, TaskState, WorkOrderFacts
from .state_machine import assert_transition


REDISCOVERY_RECOVERABLE_REJECTION_REASONS = frozenset(
    {
        "订单号在当前待处理列表中不存在，跳过",
    }
)

LEGACY_AFTERSALE_GT3_MIGRATION_KEY = "aftersale_gt3_branch_reopen_v1"
LEGACY_AFTERSALE_GT3_TITLE = "售后工作台：已发货-退款"
LEGACY_AFTERSALE_GT3_REASON_RE = re.compile(
    r"^已发货普通退款完整物流轨迹共 \d+ 条，超过三条分支尚未启用$"
)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS shops (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    edge_profile TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_key TEXT NOT NULL UNIQUE,
    shop_id TEXT NOT NULL REFERENCES shops(id),
    order_no TEXT NOT NULL,
    work_order_no TEXT NOT NULL,
    state TEXT NOT NULL,
    risk_level TEXT NOT NULL DEFAULT 'unknown',
    matched_rule_id TEXT,
    decision_json TEXT,
    facts_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_shop ON tasks(shop_id);

CREATE TABLE IF NOT EXISTS task_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    evidence_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, evidence_type, file_path)
);

CREATE INDEX IF NOT EXISTS idx_task_evidence_task
    ON task_evidence(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS rules (
    rule_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    action_key TEXT UNIQUE,
    action_type TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    action_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS components (
    component_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    component_type TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
    task_id INTEGER REFERENCES tasks(id),
    status TEXT NOT NULL,
    current_node_id TEXT,
    context_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS interception_registers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER UNIQUE REFERENCES tasks(id),
    record_date TEXT NOT NULL,
    carrier TEXT NOT NULL,
    tracking_no TEXT NOT NULL DEFAULT '',
    order_no TEXT NOT NULL,
    refund_reason TEXT NOT NULL,
    amount REAL NOT NULL DEFAULT 0,
    warehouse TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT '',
    dingtalk_target_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interception_register_date
    ON interception_registers(record_date);

CREATE TABLE IF NOT EXISTS dingtalk_interception_register_syncs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    register_id INTEGER NOT NULL UNIQUE REFERENCES interception_registers(id),
    status TEXT NOT NULL DEFAULT 'pending',
    remote_row INTEGER,
    duplicate_tracking INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dingtalk_interception_sync_status
    ON dingtalk_interception_register_syncs(status, updated_at);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _is_task_scan_snapshot(file_path: str) -> bool:
    """Return True for shop-level list snapshots, not task detail evidence."""
    name = Path(str(file_path or "")).name.lower()
    return bool(
        re.fullmatch(
            r"pdd_.+_\d{8}_\d{6}_(?:uia|readonly|aftersale_uia)\.png",
            name,
        )
    )


class Repository:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            task_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "completed_at" not in task_columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT")
            conn.execute(
                "UPDATE tasks SET completed_at=updated_at "
                "WHERE state=? AND COALESCE(completed_at, '')=''",
                (TaskState.COMPLETED,),
            )
            # Older builds attached the same shop-level list screenshot to
            # every task discovered in that scan.  Keep those files and the
            # connector-level sync audit, but remove the misleading per-task
            # references so "处理证据" only contains real detail/message proof.
            obsolete_evidence_ids = [
                int(row["id"])
                for row in conn.execute(
                    "SELECT id, file_path FROM task_evidence"
                ).fetchall()
                if _is_task_scan_snapshot(str(row["file_path"] or ""))
            ]
            if obsolete_evidence_ids:
                placeholders = ",".join("?" for _ in obsolete_evidence_ids)
                conn.execute(
                    f"DELETE FROM task_evidence WHERE id IN ({placeholders})",
                    obsolete_evidence_ids,
                )

            # Backfill the evidence index from durable facts written by older
            # builds.  Scan snapshots are intentionally excluded; actual PDD
            # detail screenshots and confirmed message screenshots remain.
            for row in conn.execute("SELECT id, facts_json FROM tasks").fetchall():
                try:
                    facts = json.loads(str(row["facts_json"] or "{}"))
                except (json.JSONDecodeError, TypeError):
                    continue
                screenshots = [
                    str(item).strip()
                    for item in (facts.get("screenshots") or [])
                    if str(item).strip()
                ]
                filtered_screenshots = [
                    path for path in screenshots if not _is_task_scan_snapshot(path)
                ]
                if filtered_screenshots != screenshots:
                    facts["screenshots"] = filtered_screenshots
                    conn.execute(
                        "UPDATE tasks SET facts_json=? WHERE id=?",
                        (json.dumps(facts, ensure_ascii=False), int(row["id"])),
                    )
                source = dict(facts.get("source_evidence") or {})
                qianniu_source = (
                    str(source.get("source_system") or "").strip()
                    == "qianniu_aftersale"
                )
                detail_evidence_type = (
                    "qianniu_aftersale_detail"
                    if qianniu_source
                    else "pdd_work_order_detail"
                )
                if qianniu_source:
                    conn.execute(
                        "DELETE FROM task_evidence "
                        "WHERE task_id=? AND evidence_type='pdd_work_order_detail'",
                        (int(row["id"]),),
                    )
                message = dict(source.get("interception_message") or {})
                message_paths = {
                    str(path).strip()
                    for path in (message.get("screenshots") or [])
                    if str(path).strip()
                }
                for path in message_paths:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_evidence"
                        "(task_id, evidence_type, file_path, created_at) VALUES (?, ?, ?, ?)",
                        (int(row["id"]), "interception_group", path, _now()),
                    )
                for path in {
                    str(item).strip()
                    for item in filtered_screenshots
                    if str(item).strip()
                } - message_paths:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_evidence"
                        "(task_id, evidence_type, file_path, created_at) VALUES (?, ?, ?, ?)",
                        (int(row["id"]), detail_evidence_type, path, _now()),
                    )
            # Before task_evidence existed, confirmed interception screenshots
            # were retained only inside the immutable dispatch audit log.
            # Recover those paths as well so historic processed tasks expose
            # the same evidence links as newly processed tasks.
            legacy_dispatches = conn.execute(
                """
                SELECT task_id, details_json, created_at
                FROM action_logs
                WHERE task_id IS NOT NULL
                  AND action_type='INTERCEPT_MESSAGE_DISPATCHED'
                  AND status='success'
                """
            ).fetchall()
            for row in legacy_dispatches:
                try:
                    details = json.loads(str(row["details_json"] or "{}"))
                except (json.JSONDecodeError, TypeError):
                    continue
                dispatch_evidence = dict(details.get("evidence") or {})
                for path in {
                    str(item).strip()
                    for item in (dispatch_evidence.get("evidence") or [])
                    if str(item).strip()
                }:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_evidence"
                        "(task_id, evidence_type, file_path, created_at) VALUES (?, ?, ?, ?)",
                        (
                            int(row["task_id"]),
                            "interception_group",
                            path,
                            str(row["created_at"] or _now()),
                        ),
                    )
            register_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(interception_registers)").fetchall()
            }
            if "tracking_no" not in register_columns:
                conn.execute(
                    "ALTER TABLE interception_registers "
                    "ADD COLUMN tracking_no TEXT NOT NULL DEFAULT ''"
                )
            if "warehouse" not in register_columns:
                conn.execute(
                    "ALTER TABLE interception_registers "
                    "ADD COLUMN warehouse TEXT NOT NULL DEFAULT ''"
                )
            if "group_name" not in register_columns:
                conn.execute(
                    "ALTER TABLE interception_registers "
                    "ADD COLUMN group_name TEXT NOT NULL DEFAULT ''"
                )
            if "dingtalk_target_key" not in register_columns:
                conn.execute(
                    "ALTER TABLE interception_registers "
                    "ADD COLUMN dingtalk_target_key TEXT NOT NULL DEFAULT ''"
                )
            # Existing ledger rows predate the tracking-number column.  The
            # task facts are the retained source for the PDD detail value.
            legacy_rows = conn.execute(
                """
                SELECT r.id, r.tracking_no, t.facts_json
                FROM interception_registers r
                LEFT JOIN tasks t ON t.id = r.task_id
                WHERE COALESCE(r.tracking_no, '') = ''
                """
            ).fetchall()
            for row in legacy_rows:
                try:
                    facts = json.loads(str(row["facts_json"] or "{}"))
                except json.JSONDecodeError:
                    facts = {}
                tracking_no = str(facts.get("tracking_no") or "").strip()
                if tracking_no:
                    conn.execute(
                        "UPDATE interception_registers "
                        "SET tracking_no=?, updated_at=? WHERE id=?",
                        (tracking_no, _now(), int(row["id"])),
                    )
            # Versioned once-only correction for historic register rows.  New
            # rows arrive pre-adjusted by the orchestrator.
            adjustment_key = "interception_register_amount_plus_5_v1"
            adjusted = conn.execute(
                "SELECT 1 FROM system_settings WHERE key=?", (adjustment_key,)
            ).fetchone()
            if not adjusted:
                conn.execute(
                    "UPDATE interception_registers SET amount=ROUND(amount + 5, 2)"
                )
                conn.execute(
                    "INSERT INTO system_settings(key, value, updated_at) VALUES (?, ?, ?)",
                    (adjustment_key, "applied", _now()),
                )
            # The original migration package had one online document and did
            # not retain the warehouse/group/target needed for a safe retry.
            # Backfill those facts once.  Rows already confirmed in DingTalk
            # remain bound to the original 示例自营仓 document; only unsynchronised
            # 示例团队 rows are redirected, and their amount is restored to the raw
            # refund amount because that target must not add five yuan.
            routing_key = "interception_register_warehouse_routes_v1"
            routed = conn.execute(
                "SELECT 1 FROM system_settings WHERE key=?", (routing_key,)
            ).fetchone()
            if not routed:
                route_rows = conn.execute(
                    """
                    SELECT r.*, t.facts_json, s.status AS sync_status
                    FROM interception_registers r
                    LEFT JOIN tasks t ON t.id=r.task_id
                    LEFT JOIN dingtalk_interception_register_syncs s
                        ON s.register_id=r.id
                    """
                ).fetchall()
                for row in route_rows:
                    try:
                        facts = json.loads(str(row["facts_json"] or "{}"))
                    except (json.JSONDecodeError, TypeError):
                        facts = {}
                    source = dict(facts.get("source_evidence") or {})
                    message = dict(source.get("interception_message") or {})
                    warehouse = str(
                        row["warehouse"] or facts.get("wdt_warehouse") or ""
                    ).strip()
                    group_name = str(
                        row["group_name"] or message.get("group_name") or ""
                    ).strip()
                    succeeded = str(row["sync_status"] or "") == "succeeded"
                    target_key = (
                        DINGTALK_TARGET_JUNFU
                        if succeeded
                        else dingtalk_target_key_for_warehouse(None, warehouse)
                    )
                    amount = round(float(row["amount"] or 0), 2)
                    if target_key == DINGTALK_TARGET_SIKU_DISTRIBUTION and not succeeded:
                        try:
                            amount = round(float(facts.get("refund_amount") or 0), 2)
                        except (TypeError, ValueError):
                            pass
                    conn.execute(
                        """
                        UPDATE interception_registers
                        SET warehouse=?, group_name=?, dingtalk_target_key=?,
                            amount=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            warehouse,
                            group_name,
                            target_key,
                            amount,
                            _now(),
                            int(row["id"]),
                        ),
                    )
                conn.execute(
                    "INSERT INTO system_settings(key, value, updated_at) VALUES (?, ?, ?)",
                    (routing_key, "applied", _now()),
                )
            conn.execute(
                "INSERT OR IGNORE INTO system_settings(key, value, updated_at) VALUES (?, ?, ?)",
                ("global_paused", "false", _now()),
            )
            # The first implementation intentionally sent every shipped
            # refund with more than three complete logistics rows to manual
            # review while the branch was unfinished.  Once the branch became
            # available, those exact legacy rows would otherwise remain in a
            # terminal manual state forever.  Reopen only rows whose latest
            # verified status update is that retired reason, whose task has not
            # changed since that update, and which have never attempted a final
            # aftersale write.  The versioned marker makes this a once-only,
            # auditable data migration and prevents unrelated NEED_REVIEW rows
            # from ever being generalized back into automation.
            migration_row = conn.execute(
                "SELECT 1 FROM system_settings WHERE key=?",
                (LEGACY_AFTERSALE_GT3_MIGRATION_KEY,),
            ).fetchone()
            if not migration_row:
                reopened_task_ids: list[int] = []
                candidates = conn.execute(
                    "SELECT id, facts_json, version, updated_at FROM tasks WHERE state=?",
                    (TaskState.NEED_REVIEW,),
                ).fetchall()
                for row in candidates:
                    task_id = int(row["id"])
                    try:
                        facts = json.loads(str(row["facts_json"] or "{}"))
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if str(facts.get("work_order_title") or "").strip() != (
                        LEGACY_AFTERSALE_GT3_TITLE
                    ):
                        continue
                    last_status = conn.execute(
                        """
                        SELECT details_json, created_at
                        FROM action_logs
                        WHERE task_id=?
                          AND action_type='STATE_STATUS_UPDATE'
                          AND status='success'
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (task_id,),
                    ).fetchone()
                    if not last_status or str(last_status["created_at"] or "") != str(
                        row["updated_at"] or ""
                    ):
                        continue
                    try:
                        status_details = json.loads(
                            str(last_status["details_json"] or "{}")
                        )
                    except (TypeError, json.JSONDecodeError):
                        continue
                    previous_reason = str(status_details.get("reason") or "").strip()
                    if not LEGACY_AFTERSALE_GT3_REASON_RE.fullmatch(previous_reason):
                        continue
                    final_attempt = conn.execute(
                        "SELECT 1 FROM action_logs "
                        "WHERE task_id=? AND action_type='AFTERSALE_FINAL_ACTION' LIMIT 1",
                        (task_id,),
                    ).fetchone()
                    if final_attempt:
                        continue
                    now = _now()
                    updated = conn.execute(
                        """
                        UPDATE tasks
                        SET state=?, completed_at=NULL, version=version+1, updated_at=?
                        WHERE id=? AND state=? AND version=?
                        """,
                        (
                            TaskState.DISCOVERED,
                            now,
                            task_id,
                            TaskState.NEED_REVIEW,
                            int(row["version"]),
                        ),
                    )
                    if updated.rowcount != 1:
                        continue
                    reopened_task_ids.append(task_id)
                    self._log_with_conn(
                        conn,
                        task_id,
                        f"migration:{LEGACY_AFTERSALE_GT3_MIGRATION_KEY}:{task_id}",
                        "STATE_STATUS_UPDATE",
                        "success",
                        {
                            "from": TaskState.NEED_REVIEW,
                            "to": TaskState.DISCOVERED,
                            "reason": "超过三条售后退款分支已上线，恢复重新采集",
                            "previous_reason": previous_reason,
                            "verified_outcome": True,
                            "recovery_trigger": LEGACY_AFTERSALE_GT3_MIGRATION_KEY,
                        },
                    )
                conn.execute(
                    "INSERT INTO system_settings(key, value, updated_at) VALUES (?, ?, ?)",
                    (
                        LEGACY_AFTERSALE_GT3_MIGRATION_KEY,
                        json.dumps(
                            {
                                "applied": True,
                                "reopened_task_ids": reopened_task_ids,
                                "reopened_count": len(reopened_task_ids),
                            },
                            ensure_ascii=False,
                        ),
                        _now(),
                    ),
                )

    def purge_demo_tasks(self) -> int:
        """Remove legacy DEMO-* tasks and their dependent audit rows."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE order_no LIKE 'DEMO-%' OR work_order_no LIKE 'DEMO-%'"
            ).fetchall()
            task_ids = [int(row["id"]) for row in rows]
            if not task_ids:
                return 0
            placeholders = ",".join("?" for _ in task_ids)
            conn.execute(f"DELETE FROM workflow_runs WHERE task_id IN ({placeholders})", task_ids)
            conn.execute(f"DELETE FROM approvals WHERE task_id IN ({placeholders})", task_ids)
            conn.execute(f"DELETE FROM action_logs WHERE task_id IN ({placeholders})", task_ids)
            conn.execute(f"DELETE FROM task_evidence WHERE task_id IN ({placeholders})", task_ids)
            conn.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", task_ids)
            return len(task_ids)

    def seed_shops(self, shops: tuple[Shop, ...]) -> None:
        now = _now()
        with self.connect() as conn:
            # The settings file is the authoritative operational shop list.
            # A shop removed from configuration must not remain enabled in the
            # database and silently reappear in the console or launch scope.
            conn.execute("UPDATE shops SET enabled=0, updated_at=?", (now,))
            for shop in shops:
                conn.execute(
                    """
                    INSERT INTO shops(id, name, edge_profile, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        edge_profile=excluded.edge_profile,
                        enabled=excluded.enabled,
                        updated_at=excluded.updated_at
                    """,
                    (shop.id, shop.name, shop.edge_profile, int(shop.enabled), now, now),
                )

    def upsert_task(self, facts: WorkOrderFacts) -> int:
        now = _now()
        with self.connect() as conn:
            incoming = facts.to_dict()
            existing_row = conn.execute(
                "SELECT id, state, facts_json FROM tasks WHERE business_key=?",
                (facts.business_key,),
            ).fetchone()
            rediscovery_rejection_reason = None
            reactivate_completed_return_refund = False
            reactivate_failed_qianniu = False
            reactivate_completed_qianniu = False
            if existing_row:
                existing = json.loads(existing_row["facts_json"])
                enriched_fields = {
                    "refund_amount", "pdd_order_status", "pdd_logistics_status",
                    "carrier", "tracking_no", "jst_outbound_status", "jst_outbound_time",
                    "jst_tracking_no", "wdt_result_count", "wdt_order_id", "wdt_platform",
                    "wdt_order_status", "wdt_warehouse", "wdt_logistics_company", "wdt_product_names", "wdt_eligible", "carrier_tracking_status",
                    "latest_logistics_text", "buyer_description",
                }
                for key in enriched_fields:
                    new_value = incoming.get(key)
                    old_value = existing.get(key)
                    is_empty = new_value in (None, "", 0, False, "UNKNOWN") or new_value == []
                    old_is_empty = old_value in (None, "", 0, False, "UNKNOWN") or old_value == []
                    if is_empty and not old_is_empty:
                        incoming[key] = old_value
                incoming["screenshots"] = list(dict.fromkeys([
                    *list(existing.get("screenshots") or []),
                    *list(incoming.get("screenshots") or []),
                ]))
                existing_source = dict(existing.get("source_evidence") or {})
                incoming_source = dict(incoming.get("source_evidence") or {})
                merged_source = {**existing_source, **incoming_source}
                # Runtime retry metadata is durable operational state, not a
                # field owned by the next read-only list scan.  Preserve it
                # across rediscovery and merge partial updates field by field
                # so an incoming ``aftersale_retry={}`` cannot erase the
                # cooldown gate before its explicit clear operation.
                existing_retry = existing_source.get("aftersale_retry")
                incoming_retry = incoming_source.get("aftersale_retry")
                if isinstance(existing_retry, dict):
                    merged_source["aftersale_retry"] = {
                        **existing_retry,
                        **(incoming_retry if isinstance(incoming_retry, dict) else {}),
                    }
                elif isinstance(incoming_retry, dict):
                    merged_source["aftersale_retry"] = dict(incoming_retry)
                incoming["source_evidence"] = merged_source
                reactivate_completed_return_refund = bool(
                    existing_row["state"] == TaskState.COMPLETED
                    and str(incoming.get("work_order_title") or "").strip()
                    == AFTERSALE_RETURN_REFUND
                    and str(incoming_source.get("source_system") or "").strip()
                    == "aftersale_workbench"
                    and str(incoming_source.get("queue_scope") or "").strip()
                    == "pending_soon_overdue"
                    and str(incoming_source.get("filter_label") or "").strip()
                    == "待处理即将逾期"
                )
                reactivate_failed_qianniu = bool(
                    existing_row["state"] == TaskState.FAILED
                    and str(incoming_source.get("source_system") or "").strip()
                    == "qianniu_aftersale"
                    and bool(incoming_source.get("automation_eligible"))
                )
                # 千牛“已完成”不是永久终态：实时列表只回传平台侧仍是
                # “退款待处理且可拦截”的售后单。同一售后单再次出现在实时
                # 扫描里，说明此前的完成判定（如详情页状态误读）与平台
                # 实际状态不符，必须重新进入队列处理。
                reactivate_completed_qianniu = bool(
                    existing_row["state"] == TaskState.COMPLETED
                    and str(incoming_source.get("source_system") or "").strip()
                    == "qianniu_aftersale"
                    and bool(incoming_source.get("automation_eligible"))
                )
                if existing_row["state"] == TaskState.REJECTED:
                    last_status = conn.execute(
                        """
                        SELECT details_json
                        FROM action_logs
                        WHERE task_id=?
                          AND action_type='STATE_STATUS_UPDATE'
                          AND status='success'
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (int(existing_row["id"]),),
                    ).fetchone()
                    if last_status:
                        try:
                            status_details = json.loads(last_status["details_json"])
                        except (TypeError, json.JSONDecodeError):
                            status_details = {}
                        previous_reason = str(status_details.get("reason") or "").strip()
                        if previous_reason in REDISCOVERY_RECOVERABLE_REJECTION_REASONS:
                            rediscovery_rejection_reason = previous_reason
            payload = json.dumps(incoming, ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO tasks(
                    business_key, shop_id, order_no, work_order_no, state,
                    facts_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(business_key) DO UPDATE SET
                    facts_json=excluded.facts_json,
                    updated_at=excluded.updated_at,
                    version=tasks.version + 1
                """,
                (
                    facts.business_key,
                    facts.shop_id,
                    facts.order_no,
                    facts.work_order_no,
                    TaskState.DISCOVERED,
                    payload,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id, state, version FROM tasks WHERE business_key=?",
                (facts.business_key,),
            ).fetchone()
            task_id = int(row["id"])
            if rediscovery_rejection_reason and row["state"] == TaskState.REJECTED:
                conn.execute(
                    "UPDATE tasks SET state=?, updated_at=? WHERE id=? AND state=?",
                    (
                        TaskState.DISCOVERED,
                        now,
                        task_id,
                        TaskState.REJECTED,
                    ),
                )
                self._log_with_conn(
                    conn,
                    task_id,
                    f"rediscovered:{task_id}:{int(row['version'])}",
                    "STATE_STATUS_UPDATE",
                    "success",
                    {
                        "from": TaskState.REJECTED,
                        "to": TaskState.DISCOVERED,
                        "reason": "工单重新出现在当前待处理同步中，自动恢复",
                        "previous_rejection_reason": rediscovery_rejection_reason,
                        "verified_outcome": True,
                        "recovery_trigger": "rediscovered_current_pending",
                    },
                )
            elif reactivate_failed_qianniu and row["state"] == TaskState.FAILED:
                conn.execute(
                    "UPDATE tasks SET state=?, completed_at=NULL, updated_at=? "
                    "WHERE id=? AND state=?",
                    (
                        TaskState.DISCOVERED,
                        now,
                        task_id,
                        TaskState.FAILED,
                    ),
                )
                self._log_with_conn(
                    conn,
                    task_id,
                    f"rediscovered_qianniu_retry:{task_id}:{int(row['version'])}",
                    "STATE_STATUS_UPDATE",
                    "success",
                    {
                        "from": TaskState.FAILED,
                        "to": TaskState.DISCOVERED,
                        "reason": "千牛售后再次出现在实时列表，下一轮重新读取",
                        "verified_outcome": True,
                        "recovery_trigger": "rediscovered_qianniu_pending",
                    },
                )
            elif reactivate_completed_qianniu and row["state"] == TaskState.COMPLETED:
                conn.execute(
                    "UPDATE tasks SET state=?, completed_at=NULL, updated_at=? "
                    "WHERE id=? AND state=?",
                    (
                        TaskState.DISCOVERED,
                        now,
                        task_id,
                        TaskState.COMPLETED,
                    ),
                )
                self._log_with_conn(
                    conn,
                    task_id,
                    f"rediscovered_qianniu_completed:{task_id}:{int(row['version'])}",
                    "STATE_STATUS_UPDATE",
                    "success",
                    {
                        "from": TaskState.COMPLETED,
                        "to": TaskState.DISCOVERED,
                        "reason": "千牛售后再次出现在实时待处理列表，重新进入处理",
                        "verified_outcome": True,
                        "recovery_trigger": "rediscovered_qianniu_pending",
                    },
                )
            elif reactivate_completed_return_refund and row["state"] == TaskState.COMPLETED:
                # 延长收货不是永久终态。同一退货退款售后单后续可能再次
                # 进入“待处理即将逾期”并重新提供延长收货操作；只要当前
                # 实时列表再次发现它，就必须重新进入队列读取当下详情。
                conn.execute(
                    "UPDATE tasks SET state=?, completed_at=NULL, updated_at=? "
                    "WHERE id=? AND state=?",
                    (
                        TaskState.DISCOVERED,
                        now,
                        task_id,
                        TaskState.COMPLETED,
                    ),
                )
                self._log_with_conn(
                    conn,
                    task_id,
                    f"rediscovered_return_refund:{task_id}:{int(row['version'])}",
                    "STATE_STATUS_UPDATE",
                    "success",
                    {
                        "from": TaskState.COMPLETED,
                        "to": TaskState.DISCOVERED,
                        "reason": "退货退款再次出现在待处理即将逾期，重新进入延长收货处理",
                        "verified_outcome": True,
                        "recovery_trigger": "rediscovered_return_refund_pending",
                    },
                )
            return task_id

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        item = self._task_row(row)
        item["evidence"] = self.list_task_evidence(task_id)
        return item

    def list_tasks(self, state: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        query = "SELECT * FROM tasks"
        params: list[Any] = []
        if state:
            query += " WHERE state=?"
            params.append(state)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        items = [self._task_row(row) for row in rows]
        evidence_by_task = self.list_task_evidence_many([int(item["id"]) for item in items])
        for item in items:
            item["evidence"] = evidence_by_task.get(int(item["id"]), [])
        return items

    def list_processed_task_history(
        self,
        *,
        start_at: str = "",
        end_before: str = "",
        shop_id: str = "",
        source: str = "",
        task_type: str = "",
        search: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """Query processed history before pagination, never after a 200-row cap."""

        where = ["state IN (?, ?)"]
        params: list[Any] = [TaskState.COMPLETED, TaskState.REJECTED]
        processed_time = "COALESCE(NULLIF(completed_at, ''), updated_at)"
        if start_at:
            where.append(f"{processed_time}>=?")
            params.append(str(start_at))
        if end_before:
            where.append(f"{processed_time}<?")
            params.append(str(end_before))
        if shop_id:
            where.append("shop_id=?")
            params.append(str(shop_id))
        source_system = "json_extract(facts_json, '$.source_evidence.source_system')"
        if source == "qianniu":
            where.append(f"{source_system}='qianniu_aftersale'")
        elif source == "aftersale":
            where.append(f"{source_system}='aftersale_workbench'")
        elif source == "work_order":
            where.append(
                f"COALESCE({source_system}, '') NOT IN ('qianniu_aftersale','aftersale_workbench')"
            )
        if task_type:
            where.append("json_extract(facts_json, '$.work_order_title')=?")
            params.append(str(task_type))
        if search:
            pattern = f"%{str(search).strip()}%"
            where.append(
                "(order_no LIKE ? OR work_order_no LIKE ? OR "
                "json_extract(facts_json, '$.tracking_no') LIKE ?)"
            )
            params.extend([pattern, pattern, pattern])
        clause = " AND ".join(where)
        safe_page = max(1, int(page))
        safe_size = max(20, min(int(page_size), 500))
        offset = (safe_page - 1) * safe_size
        with self.connect() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) AS count FROM tasks WHERE {clause}", params
                ).fetchone()["count"]
            )
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE {clause} "
                f"ORDER BY {processed_time} DESC, id DESC LIMIT ? OFFSET ?",
                [*params, safe_size, offset],
            ).fetchall()
        items = [self._task_row(row) for row in rows]
        evidence_by_task = self.list_task_evidence_many(
            [int(item["id"]) for item in items]
        )
        for item in items:
            item["evidence"] = evidence_by_task.get(int(item["id"]), [])
        return {
            "items": items,
            "total": total,
            "page": safe_page,
            "page_size": safe_size,
            "pages": max(1, (total + safe_size - 1) // safe_size),
        }

    def list_tasks_by_ids(self, task_ids: list[int]) -> list[dict[str, Any]]:
        """Return every requested task without the dashboard's 200-row history limit."""
        unique_ids = list(dict.fromkeys(int(item) for item in task_ids))
        if not unique_ids:
            return []
        rows: list[sqlite3.Row] = []
        with self.connect() as conn:
            for offset in range(0, len(unique_ids), 500):
                chunk = unique_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    conn.execute(
                        f"SELECT * FROM tasks WHERE id IN ({placeholders})",
                        chunk,
                    ).fetchall()
                )
        items = [self._task_row(row) for row in rows]
        evidence_by_task = self.list_task_evidence_many([int(item["id"]) for item in items])
        for item in items:
            item["evidence"] = evidence_by_task.get(int(item["id"]), [])
        return items

    def recover_latest_live_tasks_by_shop(self) -> dict[str, Any]:
        """Rebuild a display baseline from the newest durable scan per shop."""
        with self.connect() as conn:
            rows = conn.execute("SELECT id, facts_json FROM tasks").fetchall()
        latest_at: dict[str, str] = {}
        parsed: list[tuple[int, str, str, dict[str, Any]]] = []
        for row in rows:
            try:
                facts = json.loads(row["facts_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            source = facts.get("source_evidence") or {}
            if source.get("collection_mode") not in {"edge_readonly", "edge_uia"}:
                continue
            shop_id = str(facts.get("shop_id") or "").strip()
            sync_started_at = str(source.get("sync_started_at") or "").strip()
            if not shop_id or not sync_started_at:
                continue
            parsed.append((int(row["id"]), shop_id, sync_started_at, facts))
            if sync_started_at > latest_at.get(shop_id, ""):
                latest_at[shop_id] = sync_started_at
        shops: list[dict[str, Any]] = []
        all_ids: list[int] = []
        for shop_id in sorted(latest_at):
            matching = [
                (task_id, facts)
                for task_id, item_shop_id, started_at, facts in parsed
                if item_shop_id == shop_id and started_at == latest_at[shop_id]
            ]
            task_ids = [task_id for task_id, _facts in matching]
            sample = matching[0][1]
            shops.append(
                {
                    "shop_id": shop_id,
                    "shop_name": str(sample.get("shop_name") or shop_id),
                    "task_ids": task_ids,
                    "total": len(task_ids),
                    "source": "recovered",
                    "sync_started_at": latest_at[shop_id],
                }
            )
            all_ids.extend(task_ids)
        return {"task_ids": list(dict.fromkeys(all_ids)), "shops": shops}

    def add_task_evidence(self, task_id: int, evidence_type: str, file_path: str) -> int:
        value = str(file_path or "").strip()
        kind = str(evidence_type or "").strip()
        if not value or not kind:
            raise ValueError("任务证据类型和文件路径不能为空")
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO task_evidence(task_id, evidence_type, file_path, created_at) "
                "VALUES (?, ?, ?, ?)",
                (int(task_id), kind, value, _now()),
            )
            row = conn.execute(
                "SELECT id FROM task_evidence WHERE task_id=? AND evidence_type=? AND file_path=?",
                (int(task_id), kind, value),
            ).fetchone()
        return int(row["id"])

    def list_task_evidence(self, task_id: int) -> list[dict[str, Any]]:
        return self.list_task_evidence_many([int(task_id)]).get(int(task_id), [])

    def list_task_evidence_many(self, task_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        if not task_ids:
            return {}
        unique_ids = list(dict.fromkeys(int(item) for item in task_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM task_evidence WHERE task_id IN ({placeholders}) "
                "ORDER BY created_at DESC, id DESC",
                unique_ids,
            ).fetchall()
        result: dict[int, list[dict[str, Any]]] = {item: [] for item in unique_ids}
        for row in rows:
            result[int(row["task_id"])].append(dict(row))
        return result

    def update_facts(self, task_id: int, facts: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE tasks SET facts_json=?, version=version+1, updated_at=? WHERE id=?",
                (json.dumps(facts, ensure_ascii=False), _now(), task_id),
            )

    def upsert_interception_register(
        self,
        *,
        task_id: int,
        record_date: str,
        carrier: str,
        tracking_no: str,
        order_no: str,
        refund_reason: str,
        amount: float,
        warehouse: str = "",
        group_name: str = "",
        dingtalk_target_key: str = DINGTALK_TARGET_JUNFU,
    ) -> dict[str, Any]:
        """Record one carrier interception that reached an agree-refund stage.

        A task can be retried by the runtime, so ``task_id`` is the idempotency
        key.  Re-running the verified agree step updates the row rather than
        creating duplicate ledger entries.
        """
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO interception_registers(
                    task_id, record_date, carrier, tracking_no, order_no,
                    refund_reason, amount, warehouse, group_name,
                    dingtalk_target_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    record_date=excluded.record_date,
                    carrier=excluded.carrier,
                    tracking_no=excluded.tracking_no,
                    order_no=excluded.order_no,
                    refund_reason=excluded.refund_reason,
                    amount=excluded.amount,
                    warehouse=excluded.warehouse,
                    group_name=excluded.group_name,
                    dingtalk_target_key=excluded.dingtalk_target_key,
                    updated_at=excluded.updated_at
                """,
                (
                    int(task_id),
                    str(record_date),
                    str(carrier),
                    str(tracking_no),
                    str(order_no),
                    str(refund_reason),
                    round(float(amount or 0), 2),
                    str(warehouse or "").strip(),
                    str(group_name or "").strip(),
                    str(dingtalk_target_key or "").strip(),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM interception_registers WHERE task_id=?", (int(task_id),)
            ).fetchone()
        return dict(row) if row else {}

    def list_interception_registers(
        self, *, record_date: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                r.*,
                t.shop_id AS shop_id,
                t.facts_json AS task_facts_json,
                s.status AS dingtalk_sync_status,
                s.remote_row AS dingtalk_remote_row,
                s.duplicate_tracking AS dingtalk_duplicate_tracking,
                s.last_error AS dingtalk_last_error,
                s.synced_at AS dingtalk_synced_at
            FROM interception_registers r
            LEFT JOIN tasks t ON t.id = r.task_id
            LEFT JOIN dingtalk_interception_register_syncs s
                ON s.register_id = r.id
        """
        params: list[Any] = []
        if record_date:
            query += " WHERE r.record_date=?"
            params.append(str(record_date))
        query += " ORDER BY r.record_date DESC, r.id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 2000)))
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_facts = item.pop("task_facts_json", "")
            try:
                facts = json.loads(raw_facts) if raw_facts else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                facts = {}
            source = facts.get("source_evidence") or {}
            is_qianniu = str(source.get("source_system") or "") == "qianniu_aftersale"
            item["shop_name"] = str(facts.get("shop_name") or "")
            item["platform"] = "tmall" if is_qianniu else "pdd"
            item["platform_label"] = "天猫" if is_qianniu else "拼多多"
            result.append(item)
        return result

    def begin_dingtalk_interception_sync(self, register_id: int) -> dict[str, Any]:
        """Acquire one online-table sync attempt for a retained register row.

        The online sheet is edited by people as well as the RPA, so its next
        physical row is deliberately *not* reserved in SQLite.  This durable
        ledger only records whether this exact local register has already
        reached the online table; the desktop worker discovers the live last
        row occupied in the configured reference columns immediately before it
        writes.
        """
        now = _now()
        with self.connect() as conn:
            record = conn.execute(
                "SELECT * FROM interception_registers WHERE id=?", (int(register_id),)
            ).fetchone()
            if not record:
                raise KeyError(f"快递拦截登记不存在：{register_id}")

            sync = conn.execute(
                "SELECT * FROM dingtalk_interception_register_syncs WHERE register_id=?",
                (int(register_id),),
            ).fetchone()
            if sync and str(sync["status"]) == "succeeded":
                return {
                    "record": dict(record),
                    "sync": dict(sync),
                    "already_synced": True,
                    "local_duplicate_tracking": self._has_other_tracking_with_conn(
                        conn,
                        int(register_id),
                        str(record["tracking_no"] or ""),
                        str(record["dingtalk_target_key"] or ""),
                    ),
                }

            if sync is None:
                conn.execute(
                    """
                    INSERT INTO dingtalk_interception_register_syncs(
                        register_id, status, attempts, created_at, updated_at
                    ) VALUES (?, 'running', 1, ?, ?)
                    """,
                    (int(register_id), now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE dingtalk_interception_register_syncs
                    SET status='running', attempts=attempts+1,
                        last_error=NULL, updated_at=?
                    WHERE register_id=?
                    """,
                    (now, int(register_id)),
                )
            sync = conn.execute(
                "SELECT * FROM dingtalk_interception_register_syncs WHERE register_id=?",
                (int(register_id),),
            ).fetchone()
            return {
                "record": dict(record),
                "sync": dict(sync) if sync else {},
                "already_synced": False,
                "local_duplicate_tracking": self._has_other_tracking_with_conn(
                    conn,
                    int(register_id),
                    str(record["tracking_no"] or ""),
                    str(record["dingtalk_target_key"] or ""),
                ),
            }

    @staticmethod
    def _has_other_tracking_with_conn(
        conn: sqlite3.Connection,
        register_id: int,
        tracking_no: str,
        dingtalk_target_key: str,
    ) -> bool:
        tracking_no = str(tracking_no or "").strip()
        if not tracking_no:
            return False
        row = conn.execute(
            """
            SELECT 1 FROM interception_registers
            WHERE tracking_no=? AND id<>? AND dingtalk_target_key=?
            LIMIT 1
            """,
            (tracking_no, int(register_id), str(dingtalk_target_key or "")),
        ).fetchone()
        return bool(row)

    def mark_dingtalk_interception_sync_success(
        self,
        register_id: int,
        *,
        remote_row: int,
        duplicate_tracking: bool,
    ) -> dict[str, Any]:
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE dingtalk_interception_register_syncs
                SET status='succeeded', remote_row=?, duplicate_tracking=?,
                    last_error=NULL, synced_at=?, updated_at=?
                WHERE register_id=?
                """,
                (
                    int(remote_row),
                    int(bool(duplicate_tracking)),
                    now,
                    now,
                    int(register_id),
                ),
            )
            row = conn.execute(
                "SELECT * FROM dingtalk_interception_register_syncs WHERE register_id=?",
                (int(register_id),),
            ).fetchone()
        return dict(row) if row else {}

    def mark_dingtalk_interception_sync_failure(
        self, register_id: int, error: str
    ) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE dingtalk_interception_register_syncs
                SET status='pending', last_error=?, updated_at=?
                WHERE register_id=?
                """,
                (str(error)[-2000:], _now(), int(register_id)),
            )
            row = conn.execute(
                "SELECT * FROM dingtalk_interception_register_syncs WHERE register_id=?",
                (int(register_id),),
            ).fetchone()
        return dict(row) if row else {}

    def list_pending_dingtalk_interception_registers(
        self, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List local ledger rows that have not yet reached DingTalk."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*
                FROM interception_registers r
                LEFT JOIN dingtalk_interception_register_syncs s
                    ON s.register_id=r.id
                WHERE s.id IS NULL OR s.status<>'succeeded'
                ORDER BY r.record_date ASC, r.id ASC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_dingtalk_interception_sync(self, register_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM dingtalk_interception_register_syncs WHERE register_id=?",
                (int(register_id),),
            ).fetchone()
        return dict(row) if row else None

    def transition(self, task_id: int, target: TaskState, reason: str) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(f"任务不存在：{task_id}")
            current = TaskState(row["state"])
            assert_transition(current, target)
            now = _now()
            conn.execute(
                "UPDATE tasks SET state=?, completed_at=?, version=version+1, updated_at=? WHERE id=?",
                (target, now if target is TaskState.COMPLETED else None, now, task_id),
            )
            self._log_with_conn(
                conn,
                task_id,
                f"transition:{task_id}:{current}:{target}:{_now()}",
                "STATE_TRANSITION",
                "success",
                {"from": current, "to": target, "reason": reason},
            )

    def mark_task_status(self, task_id: int, target: TaskState, reason: str) -> None:
        """Record an externally verified outcome even when the legacy state path was skipped."""
        with self.connect() as conn:
            row = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(f"任务不存在：{task_id}")
            current = TaskState(row["state"])
            if current is target:
                return
            now = _now()
            conn.execute(
                "UPDATE tasks SET state=?, completed_at=?, version=version+1, updated_at=? WHERE id=?",
                (target, now if target is TaskState.COMPLETED else None, now, task_id),
            )
            self._log_with_conn(
                conn,
                task_id,
                f"status:{task_id}:{current}:{target}:{_now()}",
                "STATE_STATUS_UPDATE",
                "success",
                {"from": current, "to": target, "reason": reason, "verified_outcome": True},
            )

    def defer_task_retry(
        self,
        task_id: int,
        retry_not_before: str,
        reason_code: str,
        prompt_text: str,
        retry_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically defer an aftersale task until a verified platform cooldown ends.

        The retry gate deliberately lives inside ``facts.source_evidence`` so
        migrated databases need no schema change.  State, metadata, and the
        audit row are committed together; a process restart therefore cannot
        leave a retryable task without its not-before timestamp.
        """

        retry_at = str(retry_not_before or "").strip()
        code = str(reason_code or "").strip()
        prompt = str(prompt_text or "").strip()
        if not retry_at:
            raise ValueError("售后重试时间不能为空")
        try:
            datetime.fromisoformat(retry_at)
        except ValueError as exc:
            raise ValueError(f"售后重试时间格式无效：{retry_at}") from exc
        if not code:
            raise ValueError("售后重试原因代码不能为空")
        if not prompt:
            raise ValueError("售后重试平台提示不能为空")

        now = _now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state, facts_json, version FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"任务不存在：{task_id}")
            current = TaskState(row["state"])
            facts = json.loads(row["facts_json"])
            source_evidence = dict(facts.get("source_evidence") or {})
            previous = source_evidence.get("aftersale_retry")
            try:
                previous_attempts = int(
                    (previous if isinstance(previous, dict) else {}).get(
                        "attempts", 0
                    )
                )
            except (TypeError, ValueError):
                previous_attempts = 0
            context = dict(retry_context or {})
            retry = {
                "reason_code": code,
                "prompt_text": prompt,
                "retry_not_before": retry_at,
                "attempts": max(0, previous_attempts) + 1,
                "business_write_performed": bool(
                    context.get("business_write_performed")
                ),
            }
            for key in (
                "submit_attempted",
                "defer_kind",
                "stage_committed",
                "negotiation_round_submitted",
                "resume_action",
                "reject_surface",
                "shop_id",
                "order_no",
                "aftersale_no",
            ):
                if key in context:
                    retry[key] = context[key]
            source_evidence["aftersale_retry"] = retry
            facts["source_evidence"] = source_evidence
            conn.execute(
                """
                UPDATE tasks
                SET state=?, completed_at=NULL, facts_json=?,
                    version=version+1, updated_at=?
                WHERE id=?
                """,
                (
                    TaskState.RETRY_PENDING,
                    json.dumps(facts, ensure_ascii=False),
                    now,
                    task_id,
                ),
            )
            self._log_with_conn(
                conn,
                task_id,
                f"retry-deferred:{task_id}:{int(row['version']) + 1}",
                "TASK_RETRY_DEFERRED",
                "success",
                {
                    "from": current,
                    "to": TaskState.RETRY_PENDING,
                    **retry,
                    "verified_outcome": True,
                },
            )
        return retry

    def clear_task_retry(self, task_id: int, reason: str = "") -> bool:
        """Explicitly remove a persisted aftersale retry gate.

        Clearing is separate from ordinary status updates so a due retry may
        pass through collection/execution states without losing its attempt
        history.  The runtime should call this after a terminal outcome or an
        explicit operator decision.
        """

        now = _now()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state, facts_json, version FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"任务不存在：{task_id}")
            facts = json.loads(row["facts_json"])
            source_evidence = dict(facts.get("source_evidence") or {})
            previous = source_evidence.pop("aftersale_retry", None)
            if previous is None:
                return False
            facts["source_evidence"] = source_evidence
            conn.execute(
                "UPDATE tasks SET facts_json=?, version=version+1, updated_at=? WHERE id=?",
                (json.dumps(facts, ensure_ascii=False), now, task_id),
            )
            self._log_with_conn(
                conn,
                task_id,
                f"retry-cleared:{task_id}:{int(row['version']) + 1}",
                "TASK_RETRY_CLEARED",
                "success",
                {
                    "state": TaskState(row["state"]),
                    "reason": str(reason or "").strip(),
                    "previous": previous,
                    "business_write_performed": False,
                },
            )
        return True

    def set_task_status_manual(self, task_id: int, target: TaskState, reason: str) -> dict:
        """Set an operator-selected display/queue state with an explicit audit record."""
        with self.connect() as conn:
            row = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(f"任务不存在：{task_id}")
            current = TaskState(row["state"])
            if current is not target:
                now = _now()
                conn.execute(
                    "UPDATE tasks SET state=?, completed_at=?, version=version+1, updated_at=? WHERE id=?",
                    (target, now if target is TaskState.COMPLETED else None, now, task_id),
                )
            self._log_with_conn(
                conn,
                task_id,
                f"manual-status:{task_id}:{current}:{target}:{_now()}",
                "MANUAL_STATE_UPDATE",
                "success",
                {
                    "from": current,
                    "to": target,
                    "reason": reason,
                    "manual": True,
                    "verified_outcome": False,
                },
            )
        task = self.get_task(task_id)
        assert task is not None
        return task

    def mark_refunded_tasks_completed(self) -> int:
        """Repair display state for tasks with an audited successful real refund."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT task_id FROM action_logs
                WHERE task_id IS NOT NULL
                  AND action_type IN ('EXECUTE_PDD_REAL_REFUND', 'RECONCILE_PDD_REAL_REFUND')
                  AND status='success'
                  AND json_extract(details_json, '$.business_write_performed') = 1
                """
            ).fetchall()
            changed = 0
            for row in rows:
                task_id = int(row["task_id"])
                task = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
                if not task or task["state"] == TaskState.COMPLETED:
                    continue
                current = TaskState(task["state"])
                now = _now()
                conn.execute(
                    "UPDATE tasks SET state=?, completed_at=?, version=version+1, updated_at=? WHERE id=?",
                    (TaskState.COMPLETED, now, now, task_id),
                )
                self._log_with_conn(
                    conn,
                    task_id,
                    f"status:{task_id}:{current}:{TaskState.COMPLETED}:reconcile",
                    "STATE_STATUS_UPDATE",
                    "success",
                    {"from": current, "to": TaskState.COMPLETED, "reason": "已有真实退款审计记录", "verified_outcome": True},
                )
                changed += 1
            return changed

    def mark_inflight_tasks_for_review(self) -> int:
        """Fail closed after a process crash during a possible money action.

        ``EXECUTING``/``VERIFYING`` means the UI may have submitted a refund
        before the process died.  Such tasks must never re-enter the automatic
        queue without a page-level reconciliation or human review.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, state FROM tasks WHERE state IN (?, ?)",
                (TaskState.EXECUTING, TaskState.VERIFYING),
            ).fetchall()
            changed = 0
            for row in rows:
                task_id = int(row["id"])
                current = TaskState(row["state"])
                conn.execute(
                    "UPDATE tasks SET state=?, version=version+1, updated_at=? WHERE id=?",
                    (TaskState.NEED_REVIEW, _now(), task_id),
                )
                self._log_with_conn(
                    conn,
                    task_id,
                    f"status:{task_id}:{current}:{TaskState.NEED_REVIEW}:process-restart",
                    "STATE_STATUS_UPDATE",
                    "success",
                    {
                        "from": current,
                        "to": TaskState.NEED_REVIEW,
                        "reason": "进程重启后存在未核验的退款执行状态，禁止自动重试",
                        "verified_outcome": False,
                    },
                )
                changed += 1
            return changed

    def save_decision(self, task_id: int, decision: RuleDecision) -> None:
        data = {
            "matched": decision.matched,
            "rule_id": decision.rule_id,
            "rule_name": decision.rule_name,
            "actions": decision.actions,
            "approval_required": decision.approval_required,
            "reasons": decision.reasons,
        }
        risk = "review" if not decision.matched else ("approval" if decision.approval_required else "low")
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tasks SET matched_rule_id=?, decision_json=?, risk_level=?,
                    version=version+1, updated_at=? WHERE id=?
                """,
                (decision.rule_id, json.dumps(data, ensure_ascii=False), risk, _now(), task_id),
            )

    def load_rules(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT definition_json, enabled FROM rules ORDER BY priority DESC, rule_id"
            ).fetchall()
        rules: list[dict[str, Any]] = []
        for row in rows:
            definition = json.loads(row["definition_json"])
            definition["enabled"] = bool(row["enabled"])
            rules.append(definition)
        return rules

    def seed_rules(self, rules: list[dict[str, Any]]) -> None:
        now = _now()
        with self.connect() as conn:
            for rule in rules:
                conn.execute(
                    """
                    INSERT INTO rules(rule_id, name, version, enabled, priority, definition_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(rule_id) DO UPDATE SET
                        name=excluded.name,
                        version=excluded.version,
                        enabled=rules.enabled,
                        priority=excluded.priority,
                        definition_json=excluded.definition_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        rule["rule_id"],
                        rule.get("name", rule["rule_id"]),
                        int(rule.get("version", 1)),
                        int(bool(rule.get("enabled", False))),
                        int(rule.get("priority", 0)),
                        json.dumps(rule, ensure_ascii=False),
                        now,
                        now,
                    ),
                )

    def list_rules(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM rules ORDER BY priority DESC, rule_id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            item["definition"] = json.loads(item.pop("definition_json"))
            result.append(item)
        return result

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> None:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE rules SET enabled=?, updated_at=? WHERE rule_id=?",
                (int(enabled), _now(), rule_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"规则不存在：{rule_id}")

    def get_rule_enabled(self, rule_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT enabled FROM rules WHERE rule_id=?", (rule_id,)).fetchone()
        if not row:
            raise KeyError(f"规则不存在：{rule_id}")
        return bool(row["enabled"])

    def log_action(
        self,
        task_id: int | None,
        action_key: str,
        action_type: str,
        status: str,
        details: dict[str, Any],
    ) -> bool:
        with self.connect() as conn:
            return self._log_with_conn(conn, task_id, action_key, action_type, status, details)

    def _log_with_conn(
        self,
        conn: sqlite3.Connection,
        task_id: int | None,
        action_key: str,
        action_type: str,
        status: str,
        details: dict[str, Any],
    ) -> bool:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO action_logs(
                task_id, action_key, action_type, status, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task_id, action_key, action_type, status, json.dumps(details, ensure_ascii=False), _now()),
        )
        return cursor.rowcount == 1

    def list_logs(self, task_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        query = "SELECT * FROM action_logs"
        params: list[Any] = []
        if task_id is not None:
            query += " WHERE task_id=?"
            params.append(task_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def get_action_log(self, action_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM action_logs WHERE action_key=?", (action_key,)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json"))
        return item

    def set_paused(self, paused: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO system_settings(key, value, updated_at) VALUES('global_paused', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                ("true" if paused else "false", _now()),
            )

    def is_paused(self) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key='global_paused'"
            ).fetchone()
        return bool(row and row["value"] == "true")

    def set_json_setting(self, key: str, value: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO system_settings(key, value, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), _now()),
            )

    def get_json_setting(self, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
        if not row:
            return dict(default or {})
        value = json.loads(row["value"])
        if not isinstance(value, dict):
            raise ValueError(f"系统配置不是对象：{key}")
        return value

    def dashboard(self) -> dict[str, Any]:
        with self.connect() as conn:
            task_count = conn.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()["count"]
            states = conn.execute(
                "SELECT state, COUNT(*) AS count FROM tasks GROUP BY state ORDER BY state"
            ).fetchall()
            shops = conn.execute(
                "SELECT id, name, edge_profile, enabled FROM shops "
                "WHERE enabled=1 ORDER BY id"
            ).fetchall()
            rules = conn.execute(
                "SELECT COUNT(*) AS total, SUM(enabled) AS enabled FROM rules"
            ).fetchone()
            component_count = conn.execute("SELECT COUNT(*) AS count FROM components").fetchone()["count"]
            workflow_count = conn.execute("SELECT COUNT(*) AS count FROM workflows").fetchone()["count"]
            fact_rows = conn.execute("SELECT id, state, facts_json FROM tasks").fetchall()
        source_counts = {"live": 0}
        target_counts = {
            "in_transit_no_reason": 0,
            "shipped_no_trace": 0,
            "confirm_earlier_shipping": 0,
            "aftersale_shipped_refund": 0,
            "aftersale_return_refund": 0,
            "qianniu_unreceived_refund": 0,
        }
        facts_by_id: dict[int, dict[str, Any]] = {}
        state_by_id: dict[int, str] = {}
        for row in fact_rows:
            facts = json.loads(row["facts_json"])
            facts_by_id[int(row["id"])] = facts
            state_by_id[int(row["id"])] = str(row["state"])
            mode = facts.get("source_evidence", {}).get("collection_mode", "")
            if mode in {"edge_readonly", "edge_uia"}:
                source_counts["live"] += 1
            title = facts.get("work_order_title", "")
            if title == "在途无理由退款处理":
                target_counts["in_transit_no_reason"] += 1
            elif title == "已发货无轨迹退款处理":
                target_counts["shipped_no_trace"] += 1
            elif title == "确认更早发货时间":
                target_counts["confirm_earlier_shipping"] += 1
            elif title == "售后工作台：已发货-退款":
                target_counts["aftersale_shipped_refund"] += 1
            elif title == "售后工作台：退货退款":
                target_counts["aftersale_return_refund"] += 1
            elif title == "千牛未收货退款":
                target_counts["qianniu_unreceived_refund"] += 1
        sync_snapshot = self.get_json_setting("live_sync_snapshot")
        current_ids = {
            int(item)
            for item in sync_snapshot.get(
                "display_task_ids", sync_snapshot.get("task_ids", [])
            )
            if str(item).isdigit()
        }
        if not sync_snapshot.get("run_id"):
            current_ids = set(facts_by_id)
        current_ids = {
            item
            for item in current_ids
            if state_by_id.get(item) not in {TaskState.COMPLETED, TaskState.REJECTED}
        }
        current_facts = [facts_by_id[item] for item in current_ids if item in facts_by_id]
        current_target_counts = {
            "in_transit_no_reason": 0,
            "shipped_no_trace": 0,
            "confirm_earlier_shipping": 0,
            "aftersale_shipped_refund": 0,
            "aftersale_return_refund": 0,
            "qianniu_unreceived_refund": 0,
        }
        for facts in current_facts:
            if facts.get("work_order_title") == "在途无理由退款处理":
                current_target_counts["in_transit_no_reason"] += 1
            elif facts.get("work_order_title") == "已发货无轨迹退款处理":
                current_target_counts["shipped_no_trace"] += 1
            elif facts.get("work_order_title") == "确认更早发货时间":
                current_target_counts["confirm_earlier_shipping"] += 1
            elif facts.get("work_order_title") == "售后工作台：已发货-退款":
                current_target_counts["aftersale_shipped_refund"] += 1
            elif facts.get("work_order_title") == "售后工作台：退货退款":
                current_target_counts["aftersale_return_refund"] += 1
            elif facts.get("work_order_title") == "千牛未收货退款":
                current_target_counts["qianniu_unreceived_refund"] += 1
        current_aftersale_count = sum(
            1
            for facts in current_facts
            if str(facts.get("work_order_title") or "").startswith("售后工作台：")
        )
        current_qianniu_count = sum(
            1
            for facts in current_facts
            if str(
                (facts.get("source_evidence") or {}).get("source_system") or ""
            ) == "qianniu_aftersale"
        )
        current_work_order_count = (
            len(current_facts) - current_aftersale_count - current_qianniu_count
        )
        shop_current_counts: dict[str, int] = {}
        for facts in current_facts:
            shop_id = str(facts.get("shop_id") or "")
            shop_current_counts[shop_id] = shop_current_counts.get(shop_id, 0) + 1
        return {
            "paused": self.is_paused(),
            "task_count": task_count,
            "states": {row["state"]: row["count"] for row in states},
            "shops": [dict(row) for row in shops],
            "rules": {"total": rules["total"], "enabled": rules["enabled"] or 0},
            "sources": source_counts,
            "targets": target_counts,
            "current_task_count": len(current_facts),
            "current_work_order_count": current_work_order_count,
            "current_aftersale_count": current_aftersale_count,
            "current_qianniu_count": current_qianniu_count,
            "history_task_count": max(task_count - len(current_facts), 0),
            "current_targets": current_target_counts,
            "shop_current_counts": shop_current_counts,
            "sync": sync_snapshot,
            "components": component_count,
            "workflows": workflow_count,
        }

    def save_component(self, definition: dict[str, Any]) -> None:
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO components(
                    component_id, name, component_type, definition_json,
                    version, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(component_id) DO UPDATE SET
                    name=excluded.name,
                    component_type=excluded.component_type,
                    definition_json=excluded.definition_json,
                    version=components.version+1,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    definition["component_id"],
                    definition["name"],
                    definition["component_type"],
                    json.dumps(definition, ensure_ascii=False),
                    int(definition.get("version", 1)),
                    int(bool(definition.get("enabled", True))),
                    now,
                    now,
                ),
            )

    def list_components(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM components ORDER BY updated_at DESC").fetchall()
        result = []
        for row in rows:
            item = json.loads(row["definition_json"])
            item.update({"version": row["version"], "enabled": bool(row["enabled"])})
            result.append(item)
        return result

    def get_component(self, component_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT definition_json, version, enabled FROM components WHERE component_id=?",
                (component_id,),
            ).fetchone()
        if not row:
            return None
        item = json.loads(row["definition_json"])
        item.update({"version": row["version"], "enabled": bool(row["enabled"])})
        return item

    def save_workflow(self, definition: dict[str, Any]) -> None:
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workflows(
                    workflow_id, name, definition_json, version, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    name=excluded.name,
                    definition_json=excluded.definition_json,
                    version=workflows.version+1,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    definition["workflow_id"],
                    definition["name"],
                    json.dumps(definition, ensure_ascii=False),
                    int(definition.get("version", 1)),
                    int(bool(definition.get("enabled", False))),
                    now,
                    now,
                ),
            )

    def list_workflows(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM workflows ORDER BY updated_at DESC").fetchall()
        result = []
        for row in rows:
            item = json.loads(row["definition_json"])
            item.update({"version": row["version"], "enabled": bool(row["enabled"])})
            result.append(item)
        return result

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT definition_json, version, enabled FROM workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
        if not row:
            return None
        item = json.loads(row["definition_json"])
        item.update({"version": row["version"], "enabled": bool(row["enabled"])})
        return item

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["facts"] = json.loads(item.pop("facts_json"))
        item["decision"] = json.loads(item.pop("decision_json")) if item["decision_json"] else None
        return item
