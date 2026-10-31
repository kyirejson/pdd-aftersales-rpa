from __future__ import annotations

import inspect
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from .aftersale_workbench import (
    AFTERSALE_RETURN_REFUND,
    AFTERSALE_SHIPPED_REFUND,
    AGREE_RECALL_AFTER_REFUND,
    AGREE_RETURN_AFTER_REFUND,
    AGREE_REFUND,
    NEED_REVIEW,
    REJECT_REFUND,
    SKIP_REFUND_CLOSED,
)
from .edge_uia import PddAlreadyProcessedError, PddOrderNotFoundError
from .models import TaskState
from .worker_process_registry import ACTIVE_WORKERS
from .workflow_priority import (
    CONFIRM_EARLIER_SHIPPING,
    SHIPPED_NO_TRACE,
)
from .qianniu_aftersale import QIANNIU_UNRECEIVED_REFUND
from .qianniu_cycle import QianniuCycleConfig


SETTING_KEY = "runtime_controller_status"
TERMINAL_STATES = {"IDLE", "COMPLETED", "FAILED", "STOPPED", "INTERRUPTED"}
NON_AUTO_STATES = {
    TaskState.COMPLETED,
    TaskState.REJECTED,
    TaskState.NEED_REVIEW,
    # A task-level pause is different from pausing the whole runtime.  When
    # an operator selects it while a loop is active, the queued item must not
    # be opened by the current cycle; it can be resumed explicitly later.
    TaskState.PAUSED,
    TaskState.WAIT_APPROVAL,
    TaskState.EXECUTING,
    TaskState.VERIFYING,
}


class _InTransitFlowCompleted(Exception):
    def __init__(self, result: dict[str, Any]):
        super().__init__("in-transit flow completed")
        self.result = result


class _ShippedInterceptionFlowCompleted(Exception):
    def __init__(self, result: dict[str, Any]):
        super().__init__("shipped interception flow completed")
        self.result = result


class _EarlierShippingFlowCompleted(Exception):
    def __init__(self, result: dict[str, Any]):
        super().__init__("earlier-shipping flow completed")
        self.result = result


class _AftersaleFlowCompleted(Exception):
    def __init__(self, result: dict[str, Any]):
        super().__init__("aftersale-workbench flow completed")
        self.result = result


class _QianniuFlowCompleted(Exception):
    def __init__(self, result: dict[str, Any]):
        super().__init__("qianniu flow completed")
        self.result = result


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _explicit_true(value: Any) -> bool:
    """Interpret persisted safety markers without treating ``"false"`` as true."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _task_source_is_stale(task: dict[str, Any]) -> bool:
    facts = task.get("facts") or {}
    source_evidence = facts.get("source_evidence") or {}
    return _explicit_true(task.get("source_stale")) or _explicit_true(
        source_evidence.get("source_stale")
    )


class RuntimeControllerError(RuntimeError):
    pass


class RuntimeController:
    """Single-worker execution controller for UI automation that shares desktop state."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.repository = orchestrator.repository
        self._lock = threading.RLock()
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._recovery_snapshot: dict[str, Any] = {}
        self._recovery_timer: threading.Timer | None = None
        saved = self.repository.get_json_setting(SETTING_KEY)
        previous_state = str(saved.get("state") or "")
        if previous_state in {"STARTING", "RUNNING"}:
            saved.update(
                {
                    "state": "INTERRUPTED",
                    "phase": "进程重启，准备自动恢复",
                    "finished_at": _now(),
                    "recovery_pending": True,
                    "recovery_previous_state": previous_state,
                    "recovery_attempts": int(saved.get("recovery_attempts") or 0) + 1,
                    "recovery_of": str(saved.get("run_id") or ""),
                }
            )
            candidate = self.repository.get_json_setting("live_sync_snapshot")
            if candidate.get("in_progress"):
                self._recovery_snapshot = candidate
        elif previous_state == "STOPPING":
            # STOPPING is an intentional user action, not a crash. Never
            # resurrect it just because the backend process was restarted.
            saved.update(
                {
                    "state": "STOPPED",
                    "phase": "用户已停止运行",
                    "finished_at": _now(),
                    "recovery_pending": False,
                    "recovery_active": False,
                }
            )
        elif previous_state == "PAUSED":
            saved.update(
                {
                    "state": "INTERRUPTED",
                    "phase": "进程重启，原运行已暂停，请手动重启",
                    "finished_at": _now(),
                    "recovery_pending": False,
                    "recovery_active": False,
                }
            )
        self._status = {**self._blank_status(), **saved}
        self._status["poll_interval_seconds"] = self._poll_interval_seconds()
        if self._status.get("state") in TERMINAL_STATES:
            self._status["next_sync_at"] = ""
            # An INTERRUPTED state created from a previously RUNNING process
            # intentionally keeps its recovery marker. User-stopped/finished
            # terminal states must clear it so they cannot resurrect.
            if self._status.get("state") != "INTERRUPTED":
                self._status["recovery_pending"] = False
                self._status["recovery_active"] = False
        self._persist()

    @staticmethod
    def _blank_status() -> dict[str, Any]:
        return {
            "run_id": "",
            "state": "IDLE",
            "mode": "dry_run",
            "phase": "等待启动",
            "current_task": None,
            "queue": [],
            "results": [],
            "counts": {"total": 0, "completed": 0, "failed": 0, "skipped": 0},
            "started_at": "",
            "finished_at": "",
            "last_error": "",
            "continuous": False,
            "queue_only": False,
            "queue_snapshot_run_id": "",
            "queue_task_ids": [],
            "cycle": 0,
            "poll_interval_seconds": 20,
            "next_sync_at": "",
            "last_sync": {},
            "workflow_keys": [
                "shipped_no_trace",
                "in_transit_no_reason",
                "confirm_earlier_shipping",
                "aftersale_return_refund",
                "aftersale_shipped_refund",
            ],
            "recovery_pending": False,
            "recovery_previous_state": "",
            "recovery_attempts": 0,
            "recovery_of": "",
            "recovery_active": False,
        }

    def _persist(self) -> None:
        self.repository.set_json_setting(SETTING_KEY, self._status)

    def _update(self, **changes: Any) -> None:
        with self._lock:
            self._status.update(changes)
            self._persist()

    def status(self) -> dict[str, Any]:
        with self._lock:
            data = deepcopy(self._status)
        data["readiness"] = self.readiness()
        return data

    def readiness(self) -> dict[str, Any]:
        dry_run_reasons: list[str] = []
        if self.repository.is_paused():
            dry_run_reasons.append("系统处于全局暂停状态")
        if not self.repository.get_workflow("WF-WDT-SHIPPED-NO-TRACE-CHECK"):
            dry_run_reasons.append("旺店通无轨迹查询流程不存在")
        if not self.repository.get_workflow("WF-WDT-IN-TRANSIT-LOOKUP"):
            dry_run_reasons.append("旺店通在途查询流程不存在")
        production_reasons = list(dry_run_reasons)
        if not self.orchestrator.config.live_actions_enabled:
            production_reasons.append("live_actions_enabled 尚未开启")
        if str(self.orchestrator.config.execution_mode) != "auto":
            production_reasons.append("execution_mode 不是 auto")
        regression = self.repository.get_json_setting("refund_uia_regression")
        if not regression.get("confirmed"):
            production_reasons.append("新 UIA 退款流程尚未完成真实回归确认")
        in_transit_dry_reasons: list[str] = []
        in_transit_production_reasons = list(production_reasons)
        dispatch_readiness = {"ready": False, "reasons": ["消息发送组件状态未知"]}
        dispatch_check = getattr(self.orchestrator, "in_transit_dispatch_readiness", None)
        if callable(dispatch_check):
            dispatch_readiness = dict(dispatch_check() or {})
            if not dispatch_readiness.get("ready"):
                in_transit_production_reasons.extend(
                    str(reason) for reason in dispatch_readiness.get("reasons") or []
                )
        else:
            # Lightweight test orchestrators intentionally do not expose a
            # real desktop sender.  Keep their dry-run contract intact while
            # still making a real production controller fail closed.
            in_transit_production_reasons.append("消息发送组件状态未知")
        earlier_shipping_dry_reasons = (
            ["系统处于全局暂停状态"] if self.repository.is_paused() else []
        )
        earlier_shipping_production_reasons = list(earlier_shipping_dry_reasons)
        if not self.orchestrator.config.live_actions_enabled:
            earlier_shipping_production_reasons.append("live_actions_enabled 尚未开启")
        if str(self.orchestrator.config.execution_mode) != "auto":
            earlier_shipping_production_reasons.append("execution_mode 不是 auto")
        aftersale_config = dict(self.orchestrator.config.raw.get("pdd_readonly") or {})
        aftersale_dry_reasons = (
            []
            if bool(aftersale_config.get("aftersale_enabled", False))
            else ["售后工作台采集尚未开启"]
        )
        aftersale_production_reasons = list(aftersale_dry_reasons)
        if not bool(aftersale_config.get("aftersale_production_confirmed", False)):
            aftersale_production_reasons.append("售后工作台新分支尚未完成只读回归确认")
        if not self.orchestrator.config.live_actions_enabled:
            aftersale_production_reasons.append("live_actions_enabled 尚未开启")
        if str(self.orchestrator.config.execution_mode) != "auto":
            aftersale_production_reasons.append("execution_mode 不是 auto")
        aftersale_shipped_production_reasons = list(aftersale_production_reasons)
        if not dispatch_readiness.get("ready"):
            aftersale_shipped_production_reasons.extend(
                str(reason) for reason in dispatch_readiness.get("reasons") or []
            )
        qianniu_config = QianniuCycleConfig.from_raw(
            dict(self.orchestrator.config.raw.get("qianniu_shop_browser") or {})
        )
        qianniu_dry_reasons = (
            [] if qianniu_config.workflow_enabled else ["千牛工作流开关未开启"]
        )
        qianniu_production_reasons = list(qianniu_dry_reasons)
        if not qianniu_config.production_enabled:
            qianniu_production_reasons.append("千牛正式执行开关未开启")
        if not self.orchestrator.config.live_actions_enabled:
            qianniu_production_reasons.append("live_actions_enabled 尚未开启")
        if str(self.orchestrator.config.execution_mode) != "auto":
            qianniu_production_reasons.append("execution_mode 不是 auto")
        if not dispatch_readiness.get("ready"):
            qianniu_production_reasons.extend(
                str(reason) for reason in dispatch_readiness.get("reasons") or []
            )
        workflows = {
            "shipped_no_trace": {
                "dry_run": {"ready": not dry_run_reasons, "reasons": list(dry_run_reasons)},
                "production": {"ready": not production_reasons, "reasons": list(production_reasons)},
            },
            "in_transit_no_reason": {
                "dry_run": {"ready": not in_transit_dry_reasons, "reasons": in_transit_dry_reasons},
                "production": {
                    "ready": not in_transit_production_reasons,
                    "reasons": in_transit_production_reasons,
                    "dispatch": dispatch_readiness,
                },
            },
            "confirm_earlier_shipping": {
                "dry_run": {
                    "ready": not earlier_shipping_dry_reasons,
                    "reasons": earlier_shipping_dry_reasons,
                },
                "production": {
                    "ready": not earlier_shipping_production_reasons,
                    "reasons": earlier_shipping_production_reasons,
                },
            },
            "aftersale_return_refund": {
                "dry_run": {
                    "ready": not aftersale_dry_reasons,
                    "reasons": list(aftersale_dry_reasons),
                },
                "production": {
                    "ready": not aftersale_production_reasons,
                    "reasons": list(aftersale_production_reasons),
                },
            },
            "aftersale_shipped_refund": {
                "dry_run": {
                    "ready": not aftersale_dry_reasons,
                    "reasons": list(aftersale_dry_reasons),
                },
                "production": {
                    "ready": not aftersale_shipped_production_reasons,
                    "reasons": list(aftersale_shipped_production_reasons),
                    "dispatch": dispatch_readiness,
                },
            },
            "qianniu_unreceived_refund": {
                "dry_run": {
                    "ready": not qianniu_dry_reasons,
                    "reasons": qianniu_dry_reasons,
                },
                "production": {
                    "ready": not qianniu_production_reasons,
                    "reasons": qianniu_production_reasons,
                    "dispatch": dispatch_readiness,
                },
            },
        }
        return {
            "dry_run": {"ready": not dry_run_reasons, "reasons": dry_run_reasons},
            "production": {"ready": not production_reasons, "reasons": production_reasons},
            "supported_workflows": [
                "shipped_no_trace",
                "in_transit_no_reason",
                "confirm_earlier_shipping",
                "aftersale_return_refund",
                "aftersale_shipped_refund",
                "qianniu_unreceived_refund",
            ],
            "pending_workflows": (
                []
                if not aftersale_production_reasons
                else ["aftersale_return_refund", "aftersale_shipped_refund"]
            ),
            "workflows": workflows,
        }

    def _continuous_default(self, mode: str) -> bool:
        runtime = self.orchestrator.config.raw.get("runtime", {})
        configured = runtime.get("continuous")
        if configured is not None:
            return bool(configured) and mode == "production"
        # Keep lightweight/fake orchestrators and dry-run invocations single-cycle;
        # production in the real auto configuration is the persistent worker.
        return mode == "production" and str(self.orchestrator.config.execution_mode) == "auto"

    def _poll_interval_seconds(self) -> float:
        runtime = self.orchestrator.config.raw.get("runtime", {})
        try:
            value = float(runtime.get("poll_interval_seconds", 20))
        except (TypeError, ValueError):
            value = 20.0
        return max(5.0, min(value, 3600.0))

    def _auto_resume_settings(self) -> tuple[bool, float, int]:
        runtime = self.orchestrator.config.raw.get("runtime", {})
        enabled = bool(runtime.get("auto_resume_after_crash", True))
        try:
            delay = max(1.0, min(float(runtime.get("crash_restart_delay_seconds", 3)), 30.0))
        except (TypeError, ValueError):
            delay = 3.0
        try:
            attempts = max(1, min(int(runtime.get("max_crash_resume_attempts", 3)), 10))
        except (TypeError, ValueError):
            attempts = 3
        return enabled, delay, attempts

    def schedule_auto_resume(self) -> None:
        """Schedule one safe resume after a backend process restart.

        The supervisor restarts the API process, while this method resumes the
        persisted controller only when the previous state was genuinely active.
        A user-requested STOPPING state is converted to STOPPED in ``__init__``.
        """
        enabled, delay, max_attempts = self._auto_resume_settings()
        with self._lock:
            if not enabled or not self._status.get("recovery_pending"):
                return
            attempts = int(self._status.get("recovery_attempts") or 0)
            if attempts > max_attempts:
                self._status.update(
                    {
                        "recovery_pending": False,
                        "phase": f"自动恢复已连续失败 {max_attempts} 次，等待人工处理",
                        "last_error": "UIA/后端崩溃自动恢复达到上限",
                    }
                )
                self._persist()
                return
            if self._recovery_timer and self._recovery_timer.is_alive():
                return
            self._recovery_timer = threading.Timer(delay, self._recover_after_restart)
            self._recovery_timer.daemon = True
            self._recovery_timer.start()

    def _recover_after_restart(self) -> None:
        with self._lock:
            if not self._status.get("recovery_pending"):
                return
            mode = str(self._status.get("mode") or "production")
            workflow_keys = list(
                self._status.get("workflow_keys")
                or [
                    "shipped_no_trace",
                    "in_transit_no_reason",
                    "confirm_earlier_shipping",
                    "aftersale_return_refund",
                    "aftersale_shipped_refund",
                ]
            )
            continuous = bool(self._status.get("continuous"))
            queue_only = bool(self._status.get("queue_only"))
            frozen_queue_task_ids = [
                int(item) for item in self._status.get("queue_task_ids", [])
            ]
            queue_snapshot_run_id = str(self._status.get("queue_snapshot_run_id") or "")
            recovery_of = str(self._status.get("recovery_of") or "")
            recovery_attempts = int(self._status.get("recovery_attempts") or 0)
            self._status.update(
                {
                    "recovery_pending": False,
                    "recovery_active": True,
                    "phase": "正在从崩溃断点恢复",
                }
            )
            self._persist()
        try:
            self.start(
                mode,
                workflow_keys,
                continuous,
                queue_only=queue_only,
                _frozen_queue_task_ids=frozen_queue_task_ids,
                _queue_snapshot_run_id=queue_snapshot_run_id,
                _recovery=True,
                _recovery_of=recovery_of,
                _recovery_attempts=recovery_attempts,
            )
        except Exception as exc:
            with self._lock:
                self._status.update(
                    {
                        "state": "INTERRUPTED",
                        "phase": "自动恢复未启动，等待人工处理",
                        "last_error": str(exc),
                        "recovery_active": False,
                        "finished_at": _now(),
                    }
                )
                self._persist()

    def start(
        self,
        mode: str = "dry_run",
        workflow_keys: list[str] | None = None,
        continuous: bool | None = None,
        *,
        queue_only: bool = False,
        _recovery: bool = False,
        _recovery_of: str = "",
        _recovery_attempts: int = 0,
        _frozen_queue_task_ids: list[int] | None = None,
        _queue_snapshot_run_id: str = "",
    ) -> dict[str, Any]:
        workflow_keys = workflow_keys or [
            "shipped_no_trace",
            "in_transit_no_reason",
            "confirm_earlier_shipping",
            "aftersale_return_refund",
            "aftersale_shipped_refund",
        ]
        qianniu_cycle = QianniuCycleConfig.from_raw(
            dict(self.orchestrator.config.raw.get("qianniu_shop_browser") or {})
        )
        if qianniu_cycle.workflow_enabled and "qianniu_unreceived_refund" not in workflow_keys:
            workflow_keys = [*workflow_keys, "qianniu_unreceived_refund"]
        if mode not in {"dry_run", "production"}:
            raise RuntimeControllerError("运行模式必须是 dry_run 或 production")
        if not set(workflow_keys).issubset(
            {
                "shipped_no_trace",
                "in_transit_no_reason",
                "confirm_earlier_shipping",
                "aftersale_return_refund",
                "aftersale_shipped_refund",
                "qianniu_unreceived_refund",
            }
        ) or not workflow_keys:
            raise RuntimeControllerError(
                "只允许启动已发货无轨迹、在途无理由、确认更早发货时间和"
                "售后工作台（退货退款/已发货退款）流程"
            )
        readiness = self.readiness()
        unavailable: list[str] = []
        for workflow_key in workflow_keys:
            workflow_readiness = dict(readiness.get("workflows", {}).get(workflow_key, {}).get(mode) or {})
            if not workflow_readiness:
                # Backward-compatible fallback for small test doubles.
                workflow_readiness = dict(readiness.get(mode) or {})
            if not workflow_readiness.get("ready"):
                reasons = "；".join(str(item) for item in workflow_readiness.get("reasons") or [])
                unavailable.append(f"{workflow_key}：{reasons or '未就绪'}")
        if unavailable:
            raise RuntimeControllerError("；".join(unavailable))
        frozen_queue_task_ids: list[int] = []
        queue_snapshot_run_id = ""
        if queue_only:
            # Validate before creating a runtime thread so a stale/empty page
            # cannot look like a successfully started production run.
            if _recovery:
                if not _frozen_queue_task_ids or not _queue_snapshot_run_id:
                    raise RuntimeControllerError(
                        "当前队列恢复缺少已冻结任务 ID 或同步批次，已禁止自动改取新队列"
                    )
                current_discovery = self._frozen_queue_discovery(_frozen_queue_task_ids)
                queue_snapshot_run_id = str(_queue_snapshot_run_id)
            else:
                current_discovery = self._current_queue_discovery()
                queue_snapshot_run_id = str(
                    (current_discovery.get("sync") or {}).get("run_id") or ""
                )
            current_queue = self._build_queue(current_discovery, workflow_keys)
            if not current_queue:
                raise RuntimeControllerError("当前实时队列中没有可处理的目标工单或售后单")
            frozen_queue_task_ids = [int(item["task_id"]) for item in current_queue]
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeControllerError("已有流程正在运行")
            if self._status.get("state") not in TERMINAL_STATES:
                raise RuntimeControllerError(f"当前状态不允许启动：{self._status.get('state')}")
            self._pause.clear()
            self._stop.clear()
            if not _recovery:
                self._recovery_snapshot = {}
            self._status = self._blank_status()
            continuous = self._continuous_default(mode) if continuous is None else bool(continuous)
            if mode != "production" or queue_only:
                continuous = False
            self._status.update(
                {
                    "run_id": uuid.uuid4().hex,
                    "state": "STARTING",
                    "mode": mode,
                    "phase": (
                        "准备单轮处理当前实时队列（跳过店铺同步）"
                        if queue_only
                        else "准备第 1 轮同步全部店铺工单"
                    ),
                    "started_at": _now(),
                    "workflow_keys": workflow_keys,
                    "continuous": continuous,
                    "queue_only": bool(queue_only),
                    "queue_snapshot_run_id": queue_snapshot_run_id,
                    "queue_task_ids": frozen_queue_task_ids,
                    "cycle": 0,
                    "poll_interval_seconds": self._poll_interval_seconds(),
                    "last_sync": {},
                    "recovery_active": _recovery,
                    "recovery_pending": False,
                    "recovery_of": _recovery_of if _recovery else "",
                    "recovery_attempts": int(_recovery_attempts or 0) if _recovery else 0,
                }
            )
            self._persist()
            self._thread = threading.Thread(target=self._run, name="pdd-rpa-runtime", daemon=True)
            self._thread.start()
        return self.status()

    def pause(self) -> dict[str, Any]:
        with self._lock:
            if self._status.get("state") not in {"STARTING", "RUNNING"}:
                raise RuntimeControllerError("当前没有可暂停的运行")
            self._pause.set()
            self._status.update({"state": "PAUSED", "phase": "已暂停，将在当前组件结束后停住"})
            self._persist()
        return self.status()

    def resume(self) -> dict[str, Any]:
        with self._lock:
            if self._status.get("state") != "PAUSED":
                raise RuntimeControllerError("当前运行不是暂停状态")
            self._pause.clear()
            self._status.update({"state": "RUNNING", "phase": "继续运行"})
            self._persist()
        return self.status()

    def stop(self) -> dict[str, Any]:
        current_task_id: int | None = None
        with self._lock:
            if self._status.get("state") in TERMINAL_STATES:
                raise RuntimeControllerError("当前没有可停止的运行")
            current = self._status.get("current_task") or {}
            try:
                current_task_id = int(current.get("task_id")) if current.get("task_id") is not None else None
            except (TypeError, ValueError):
                current_task_id = None
            self._stop.set()
            self._pause.clear()
            self._status.update(
                {
                    "state": "STOPPING",
                    "phase": "立即停止：正在终止当前自动化子进程",
                    "next_sync_at": "",
                    "stop_requested_at": _now(),
                    "recovery_pending": False,
                    "recovery_active": False,
                }
            )
            self._persist()

        # Do not hold the controller lock while terminating a worker.  The
        # process registry contains only exact PIDs created by this runtime;
        # on Windows /T also closes native UIA descendants that otherwise keep
        # communicate() blocked for a long time.
        worker_pids = ACTIVE_WORKERS.terminate_all()
        freeze_live_sync = getattr(self.orchestrator, "finalize_live_sync_after_stop", None)
        if callable(freeze_live_sync):
            try:
                freeze_live_sync()
            except Exception:
                # Immediate stop must remain best-effort for legacy/fake
                # repositories that do not have the durable display snapshot.
                pass
        if current_task_id is not None:
            get_task = getattr(self.repository, "get_task", None)
            mark_status = getattr(self.repository, "mark_task_status", None)
            if callable(get_task) and callable(mark_status):
                try:
                    task = get_task(current_task_id)
                    state = TaskState(str(task.get("state"))) if task else None
                    if state and state not in {
                        TaskState.COMPLETED,
                        TaskState.REJECTED,
                        TaskState.FAILED,
                        TaskState.NEED_REVIEW,
                        TaskState.VERIFYING,
                    }:
                        mark_status(
                            current_task_id,
                            TaskState.NEED_REVIEW,
                            "用户立即停止运行，当前工单可能已进入外部操作，需人工复核后再继续",
                        )
                except Exception:
                    # Stopping must remain best-effort even if a legacy/fake
                    # repository cannot persist the review marker.
                    pass
        result = self.status()
        result["terminated_worker_pids"] = worker_pids
        result["immediate_stop_requested"] = True
        return result

    def _checkpoint(self) -> None:
        if self._stop.is_set():
            raise InterruptedError("用户停止运行")
        while self._pause.is_set():
            if self._stop.wait(0.2):
                raise InterruptedError("用户停止运行")

    def _call_cancelable(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        """Pass the stop event to newer connectors without breaking test/fake ones."""
        try:
            parameters = inspect.signature(method).parameters.values()
            supports_cancel = any(
                parameter.name == "cancel_event"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_cancel = False
        if supports_cancel:
            kwargs["cancel_event"] = self._stop
        return method(*args, **kwargs)

    def _queued_task_manual_state(self, task_id: int) -> TaskState | None:
        """Re-read a queued task immediately before opening any external UI.

        Discovery builds a queue snapshot, while an operator can change a
        task from the dashboard during the loop.  The snapshot must therefore
        never be treated as authorization to act.  Keep this best-effort for
        legacy/fake repositories that do not expose ``get_task``.
        """
        get_task = getattr(self.repository, "get_task", None)
        if not callable(get_task):
            return None
        try:
            task = get_task(task_id)
        except Exception:
            return None
        if not task:
            return None
        try:
            state = TaskState(task.get("state", TaskState.DISCOVERED))
        except (TypeError, ValueError):
            return None
        return state if state in NON_AUTO_STATES else None

    @staticmethod
    def _aftersale_retry_is_due(task: dict[str, Any]) -> bool:
        """Fail closed until a persisted PDD reject cooldown has expired."""

        try:
            state = TaskState(task.get("state", TaskState.DISCOVERED))
        except (TypeError, ValueError):
            return False
        if state is not TaskState.RETRY_PENDING:
            return True
        facts = dict(task.get("facts") or {})
        source_evidence = dict(facts.get("source_evidence") or {})
        retry = source_evidence.get("aftersale_retry")
        if not isinstance(retry, dict):
            return False
        retry_not_before = str(retry.get("retry_not_before") or "").strip()
        if not retry_not_before:
            return False
        try:
            due_at = datetime.fromisoformat(retry_not_before)
        except ValueError:
            return False
        now = datetime.now(tz=due_at.tzinfo) if due_at.tzinfo else datetime.now()
        return now >= due_at

    def _load_active_scan_tasks(self, task_ids: list[int]) -> list[dict[str, Any]]:
        """Load the full current-scan task rows, bypassing the 200-row cap.

        The default ``list_prioritized_tasks`` view is capped at the 200 most
        recently updated rows for the dashboard.  The runtime queue must see
        every task the current scan confirmed, so prefer the by-ids loader
        whenever the orchestrator supports it.
        """
        list_tasks = self.orchestrator.list_prioritized_tasks
        try:
            parameters = inspect.signature(list_tasks).parameters.values()
            supports_ids = any(
                parameter.name == "task_ids"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_ids = False
        if supports_ids:
            return list_tasks(task_ids=task_ids)
        loader = getattr(self.orchestrator, "list_prioritized_tasks_by_ids", None)
        if callable(loader):
            return loader(task_ids)
        return list_tasks()

    def _build_queue(self, discovery: dict[str, Any], workflow_keys: list[str]) -> list[dict[str, Any]]:
        active_ids = {
            int(task_id)
            for result in discovery.get("results", [])
            if result.get("ok")
            for task_id in result.get("task_ids", [])
        }
        titles = {
            "shipped_no_trace": SHIPPED_NO_TRACE,
            # In-transit is a separate branch; it must never be routed through
            # the shipped-no-trace refund components.
            "in_transit_no_reason": "在途无理由退款处理",
            "confirm_earlier_shipping": CONFIRM_EARLIER_SHIPPING,
            "aftersale_return_refund": AFTERSALE_RETURN_REFUND,
                "aftersale_shipped_refund": AFTERSALE_SHIPPED_REFUND,
                "qianniu_unreceived_refund": QIANNIU_UNRECEIVED_REFUND,
        }
        target_titles = {titles[key] for key in workflow_keys if key in titles}
        queue: list[dict[str, Any]] = []
        tasks = discovery.get("_tasks")
        if not isinstance(tasks, list):
            # Never fall back to the dashboard's 200-row history window here:
            # a full multi-shop scan touches more rows than that cap, and the
            # earliest-scanned shop's work orders would be pushed out of the
            # updated_at window by later shops before this queue is built.
            tasks = self._load_active_scan_tasks(sorted(active_ids))
        for task in tasks:
            if task["id"] not in active_ids:
                continue
            # A partial aftersale scan can preserve last cycle's rows for
            # visibility.  Those rows are useful evidence, never current
            # authorization to open a detail page or perform a write.
            if _task_source_is_stale(task):
                continue
            facts = task.get("facts") or {}
            source_evidence = facts.get("source_evidence") or {}
            if source_evidence.get("source_system") == "aftersale_workbench":
                if _explicit_true(source_evidence.get("display_only")):
                    continue
                if (
                    "automation_eligible" in source_evidence
                    and not _explicit_true(source_evidence.get("automation_eligible"))
                ):
                    continue
            if facts.get("work_order_title") not in target_titles:
                continue
            try:
                state = TaskState(task.get("state", TaskState.DISCOVERED))
            except ValueError:
                state = TaskState.DISCOVERED
            if state in NON_AUTO_STATES:
                continue
            if not self._aftersale_retry_is_due(task):
                continue
            workflow_key = next(
                (key for key, title in titles.items() if title == facts.get("work_order_title")),
                "",
            )
            queue.append(
                {
                    "task_id": task["id"],
                    "shop_id": facts.get("shop_id"),
                    "shop_name": facts.get("shop_name"),
                    "order_no": facts.get("order_no"),
                    "priority": task.get("queue_priority", 0),
                    "workflow_key": workflow_key,
                    "state": task.get("state", TaskState.DISCOVERED),
                    "shop_scan_order": (facts.get("source_evidence") or {}).get("shop_scan_order", 0),
                    "page_no": (facts.get("source_evidence") or {}).get("page_no", 1),
                    "page_index": (facts.get("source_evidence") or {}).get("page_index", 0),
                }
            )
        workflow_order = {
            "shipped_no_trace": 0,
            "in_transit_no_reason": 1,
            "confirm_earlier_shipping": 2,
            "aftersale_return_refund": 3,
            "aftersale_shipped_refund": 4,
            "qianniu_unreceived_refund": 5,
        }
        queue.sort(
            key=lambda item: (
                int(item.get("shop_scan_order") or 0),
                workflow_order.get(str(item.get("workflow_key")), 99),
                int(item.get("page_no") or 1),
                int(item.get("page_index") or 0),
            )
        )
        return queue

    def _current_queue_discovery(self) -> dict[str, Any]:
        """Build a read-only discovery-shaped snapshot from the visible live queue."""
        snapshot = self.repository.get_json_setting("live_sync_snapshot")
        if not snapshot.get("run_id"):
            raise RuntimeControllerError("尚无有效的实时同步快照，请先同步店铺工单")
        if snapshot.get("in_progress"):
            raise RuntimeControllerError("店铺同步仍在进行，完成或停止同步后才能处理当前队列")
        list_tasks = self.orchestrator.list_prioritized_tasks
        try:
            parameters = inspect.signature(list_tasks).parameters.values()
            supports_view = any(
                parameter.name == "view"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_view = False
        if not supports_view:
            raise RuntimeControllerError("当前项目不支持安全读取实时待处理队列")
        tasks = [
            task
            for task in list_tasks(view="pending")
            if not _task_source_is_stale(task)
        ]
        task_ids = [int(task["id"]) for task in tasks]
        return {
            "ok": True,
            "current_queue": True,
            "results": [
                {
                    "shop_id": "current_live_queue",
                    "shop_name": "当前实时队列",
                    "ok": True,
                    "error": None,
                    "task_ids": task_ids,
                    "total": len(task_ids),
                }
            ],
            "sync": snapshot,
            "_tasks": tasks,
        }

    def _frozen_queue_discovery(self, task_ids: list[int]) -> dict[str, Any]:
        """Reload exactly the task IDs confirmed at queue-only start/recovery."""
        frozen_ids = list(dict.fromkeys(int(item) for item in task_ids))
        loader = getattr(self.orchestrator, "list_prioritized_tasks_by_ids", None)
        if not callable(loader):
            raise RuntimeControllerError("当前项目不支持安全恢复已冻结的实时队列")
        tasks = loader(frozen_ids)
        return {
            "ok": True,
            "current_queue": True,
            "results": [
                {
                    "shop_id": "frozen_live_queue",
                    "shop_name": "已冻结实时队列",
                    "ok": True,
                    "error": None,
                    "task_ids": frozen_ids,
                    "total": len(frozen_ids),
                }
            ],
            "sync": {
                "run_id": str(self._status.get("queue_snapshot_run_id") or ""),
                "frozen": True,
            },
            "_tasks": tasks,
        }

    def _process_in_transit_item(
        self,
        queue_item: dict[str, Any],
        production: bool,
        *,
        steps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run one retained-detail in-transit route without touching money first."""
        task_id = int(queue_item["task_id"])
        steps = steps if steps is not None else []
        detail = self.orchestrator.collect_task_detail_readonly(task_id, keep_detail_open=True)
        steps.append({"step": "PDD_DETAIL", "status": "success", "tracking_no": detail.get("tracking_no"), "detail_retained": True})
        self._checkpoint()
        wdt = self._call_cancelable(self.orchestrator.check_wdt_by_components, task_id)
        steps.append({"step": "WDT_CHECK", "status": "success", "order_status": wdt.get("order_status"), "warehouse": wdt.get("warehouse"), "logistics_company": wdt.get("logistics_company")})
        routing = self.orchestrator.prepare_in_transit_routing(task_id)
        if not routing.get("ok"):
            if routing.get("manual_review"):
                reason = str(routing.get("reason") or "旺店通仓库未完成在途自动化配置")
                self._mark_status(task_id, TaskState.NEED_REVIEW, reason)
                unsupported_warehouse = routing.get("reason_code") == "UNSUPPORTED_WDT_WAREHOUSE"
                steps.append(
                    {
                        "step": "WDT_WAREHOUSE_GATE" if unsupported_warehouse else "ROUTE_MANUAL_REVIEW",
                        "status": "skipped",
                        "warehouse": routing.get("wdt_warehouse") or "未读取",
                        "reason_code": routing.get("reason_code") or "ROUTE_UNMAPPED",
                        "reason": reason,
                    }
                )
                return {
                    "status": (
                        "SKIPPED_UNSUPPORTED_WAREHOUSE"
                        if unsupported_warehouse
                        else "NEED_REVIEW_ROUTE_UNMAPPED"
                    ),
                    "steps": steps,
                    "error": reason,
                    "finished_at": _now(),
                    "routing": routing,
                }
            raise RuntimeError(routing.get("reason") or "在途物流路由无法匹配")
        steps.append({"step": "ROUTE", "status": "success", "route_type": routing.get("route_type"), "group": routing.get("group"), "message": routing.get("message")})
        if not production:
            steps.append({"step": "INTERCEPT", "status": "preview", "sent": False})
            return {"status": "READY_FOR_PRODUCTION", "steps": steps, "finished_at": _now(), "routing": routing}
        # From this point the flow may send an external interception message
        # and then submit a refund decision.  Mark it as in-flight first so a
        # backend crash cannot enqueue the same order again automatically.
        self._mark_status(
            task_id,
            TaskState.EXECUTING,
            "在途拦截/退款外部写入即将开始，进程重启后必须人工核验",
        )
        # Persist the external-write checkpoint before activating a desktop
        # chat client. Any later error is an uncertain dispatch and must not
        # cause an automatic resend of the same tracking number.
        steps.append({"step": "INTERCEPT", "status": "started", "sent": False})
        dispatch = self._call_cancelable(
            self.orchestrator.dispatch_in_transit_interception, task_id, routing
        )
        steps[-1].update({"status": "success", "sent": True, "send": dispatch.get("send")})
        routing = self._finish_in_transit_pdd_after_interception(task_id, steps)
        detail_cleanup = dict(routing.get("detail_cleanup") or {})
        if routing.get("submission_ineffective"):
            self._mark_status(
                task_id,
                TaskState.NEED_REVIEW,
                "已协商一致并提交，但表单未变化；已跳过且不会自动重试",
            )
            return {
                "status": "SKIPPED_SUBMISSION_INEFFECTIVE",
                "steps": steps,
                "error": str(routing.get("skip_reason") or "已协商一致并提交后表单未变化"),
                "finished_at": _now(),
                "routing": routing,
                "detail_closed_inline": self._detail_cleanup_handled(detail_cleanup),
            }
        self._mark_status(task_id, TaskState.COMPLETED, "在途拦截并完成退款终态")
        return {
            "status": "COMPLETED",
            "steps": steps,
            "finished_at": _now(),
            "routing": routing,
            "detail_closed_inline": self._detail_cleanup_handled(detail_cleanup),
        }

    def _process_shipped_interception_after_wdt(
        self,
        queue_item: dict[str, Any],
        production: bool,
        *,
        steps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Route an 已发货 no-trace order through interception before refund."""
        task_id = int(queue_item["task_id"])
        routing = self.orchestrator.prepare_in_transit_routing(task_id)
        if not routing.get("ok"):
            reason = str(routing.get("reason") or "已发货订单拦截路由无法匹配")
            self._mark_status(task_id, TaskState.NEED_REVIEW, reason)
            steps.append({"step": "ROUTE", "status": "needs_review", "reason": reason})
            return {
                "status": "NEED_REVIEW_INTERCEPTION_ROUTE",
                "steps": steps,
                "error": reason,
                "finished_at": _now(),
                "routing": routing,
            }
        steps.append(
            {
                "step": "ROUTE",
                "status": "success",
                "route_type": routing.get("route_type"),
                "group": routing.get("group"),
            }
        )
        if not production:
            steps.append({"step": "INTERCEPT", "status": "preview", "sent": False})
            return {
                "status": "READY_FOR_PRODUCTION",
                "steps": steps,
                "finished_at": _now(),
                "routing": routing,
            }

        self._mark_status(
            task_id,
            TaskState.EXECUTING,
            "旺店通状态为已发货，开始快递拦截；发送回读成功后才退款",
        )
        steps.append({"step": "INTERCEPT", "status": "started", "sent": False})
        dispatch = self._call_cancelable(
            self.orchestrator.dispatch_in_transit_interception,
            task_id,
            routing,
        )
        steps[-1].update(
            {"status": "success", "sent": True, "send": dispatch.get("send")}
        )
        completed = self.orchestrator.complete_shipped_no_trace_after_interception(
            task_id,
            live=True,
        )
        cleanup = dict(completed.get("detail_cleanup") or {})
        steps.append(
            {
                "step": "PDD_SHIPPED_REFUND_AFTER_INTERCEPTION",
                "status": "success",
                "trace": completed.get("trace"),
                "detail_cleanup": cleanup,
            }
        )
        self._mark_status(task_id, TaskState.COMPLETED, "已发货订单拦截成功并完成退款")
        return {
            "status": "COMPLETED",
            "steps": steps,
            "finished_at": _now(),
            "routing": routing,
            "result": completed,
            "detail_closed_inline": self._detail_cleanup_handled(cleanup),
        }

    def _process_earlier_shipping_item(
        self,
        queue_item: dict[str, Any],
        production: bool,
        *,
        steps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Open one detail page and submit its dynamically revealed form."""
        task_id = int(queue_item["task_id"])
        steps = steps if steps is not None else []
        detail = self.orchestrator.collect_task_detail_readonly(
            task_id,
            keep_detail_open=True,
            require_logistics=False,
        )
        steps.append(
            {
                "step": "PDD_DETAIL",
                "status": "success",
                "detail_retained": True,
                "logistics_required": False,
            }
        )
        self._checkpoint()
        if production:
            self._mark_status(
                task_id,
                TaskState.EXECUTING,
                "确认更早发货时间即将提交，进程重启后必须人工核验",
            )
        steps.append(
            {
                "step": "PDD_EARLIER_SHIPPING",
                "status": "started" if production else "preview",
            }
        )
        result = self.orchestrator.complete_earlier_shipping(
            task_id,
            live=production,
        )
        steps[-1].update(
            {
                "status": "success",
                "selected_option": result.get("selected_option"),
                "submit_deferred_until_selection": bool(
                    result.get("submit_deferred_until_selection")
                ),
            }
        )
        cleanup = dict(result.get("detail_cleanup") or {})
        if production:
            self._mark_status(task_id, TaskState.COMPLETED, "已确认更早发货时间")
            status = "COMPLETED"
        else:
            status = "READY_FOR_PRODUCTION"
        return {
            "status": status,
            "steps": steps,
            "finished_at": _now(),
            "result": result,
            "detail_closed_inline": self._detail_cleanup_handled(cleanup),
        }

    def _process_aftersale_item(
        self,
        queue_item: dict[str, Any],
        production: bool,
        *,
        steps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Process one 售后工作台 item while keeping its detail tab retained."""

        task_id = int(queue_item["task_id"])
        steps = steps if steps is not None else []
        detail = self._call_cancelable(
            self.orchestrator.collect_aftersale_detail,
            task_id,
            keep_detail_open=True,
        )
        decision = dict(detail.get("decision") or {})
        action = str(decision.get("action") or NEED_REVIEW)
        facts = dict(detail.get("facts") or {})
        steps.append(
            {
                "step": "AFTERSALE_DETAIL",
                "status": "success",
                "refund_type": facts.get("refund_type"),
                "has_extend_receipt": bool(facts.get("has_extend_receipt")),
                "action": action,
                "reason": decision.get("reason"),
            }
        )
        self._checkpoint()

        if action == SKIP_REFUND_CLOSED:
            cleanup = dict(detail.get("detail_cleanup") or {})
            if not self._detail_cleanup_handled(cleanup):
                cleanup = self._call_cancelable(
                    self.orchestrator.close_aftersale_detail, task_id
                )
            reason = str(
                decision.get("reason")
                or "售后状态包含退款关闭或退款成功，按规则忽略"
            )
            self._mark_status(task_id, TaskState.REJECTED, reason)
            steps.append(
                {
                    "step": "AFTERSALE_SKIP_REFUND_CLOSED",
                    "status": "skipped",
                    "reason": reason,
                    "detail_cleanup": cleanup,
                    "business_write_performed": False,
                }
            )
            return {
                "status": "SKIPPED_REFUND_CLOSED",
                "steps": steps,
                "finished_at": _now(),
                "detail_closed_inline": self._detail_cleanup_handled(cleanup),
            }

        if action in {
            AGREE_REFUND,
            AGREE_RETURN_AFTER_REFUND,
            AGREE_RECALL_AFTER_REFUND,
            REJECT_REFUND,
        }:
            self._update(phase="售后普通退款：旺店通查询订单与物流信息")
            wdt = self._call_cancelable(
                self.orchestrator.check_wdt_by_components, task_id
            )
            steps.append(
                {
                    "step": "WDT_CHECK",
                    "status": "success",
                    "order_status": wdt.get("order_status"),
                    "warehouse": wdt.get("warehouse"),
                    "logistics_company": wdt.get("logistics_company"),
                    "requires_interception": bool(wdt.get("requires_interception")),
                }
            )
            warehouse_decision = dict(wdt.get("aftersale_decision") or {})
            if warehouse_decision:
                previous_action = action
                action = str(
                    warehouse_decision.get("action") or NEED_REVIEW
                )
                decision = warehouse_decision
                steps.append(
                    {
                        "step": "AFTERSALE_WAREHOUSE_THRESHOLD",
                        "status": "success",
                        "warehouse": wdt.get("warehouse"),
                        "max_agree_refund_trace_count": wdt.get(
                            "max_agree_refund_trace_count"
                        ),
                        "previous_action": previous_action,
                        "action": action,
                        "reason": warehouse_decision.get("reason"),
                    }
                )
                if action not in {
                    AGREE_REFUND,
                    AGREE_RETURN_AFTER_REFUND,
                    AGREE_RECALL_AFTER_REFUND,
                    REJECT_REFUND,
                }:
                    cleanup = self._call_cancelable(
                        self.orchestrator.close_aftersale_detail, task_id
                    )
                    reason = str(
                        warehouse_decision.get("reason")
                        or "按旺店通仓库阈值重新判断后没有可安全执行的售后动作"
                    )
                    self._mark_status(task_id, TaskState.NEED_REVIEW, reason)
                    return {
                        "status": "NEED_REVIEW_WAREHOUSE_THRESHOLD",
                        "steps": steps,
                        "error": reason,
                        "finished_at": _now(),
                        "detail_closed_inline": self._detail_cleanup_handled(
                            cleanup
                        ),
                    }
            if not bool(wdt.get("requires_interception")):
                cleanup = self._call_cancelable(
                    self.orchestrator.close_aftersale_detail, task_id
                )
                reason = "旺店通结果没有确认该已发货订单需要拦截，禁止执行售后处理动作"
                self._mark_status(task_id, TaskState.NEED_REVIEW, reason)
                steps.append(
                    {
                        "step": "WDT_INTERCEPTION_GATE",
                        "status": "needs_review",
                        "reason": reason,
                    }
                )
                return {
                    "status": "NEED_REVIEW_WDT_INTERCEPTION_GATE",
                    "steps": steps,
                    "error": reason,
                    "finished_at": _now(),
                    "detail_closed_inline": self._detail_cleanup_handled(cleanup),
                }
            routing = self.orchestrator.prepare_in_transit_routing(task_id)
            if not routing.get("ok"):
                cleanup = self._call_cancelable(
                    self.orchestrator.close_aftersale_detail, task_id
                )
                reason = str(routing.get("reason") or "售后已发货退款拦截路由无法匹配")
                self._mark_status(task_id, TaskState.NEED_REVIEW, reason)
                steps.append(
                    {"step": "ROUTE", "status": "needs_review", "reason": reason}
                )
                return {
                    "status": "NEED_REVIEW_INTERCEPTION_ROUTE",
                    "steps": steps,
                    "error": reason,
                    "finished_at": _now(),
                    "routing": routing,
                    "detail_closed_inline": self._detail_cleanup_handled(cleanup),
                }
            steps.append(
                {
                    "step": "ROUTE",
                    "status": "success",
                    "route_type": routing.get("route_type"),
                    "group": routing.get("group"),
                }
            )
            if not production:
                cleanup = self._call_cancelable(
                    self.orchestrator.close_aftersale_detail, task_id
                )
                steps.append(
                    {"step": "INTERCEPT", "status": "preview", "sent": False}
                )
                steps.append(
                    {"step": "AFTERSALE_ACTION", "status": "preview", "action": action}
                )
                return {
                    "status": "READY_FOR_PRODUCTION",
                    "steps": steps,
                    "finished_at": _now(),
                    "routing": routing,
                    "detail_closed_inline": self._detail_cleanup_handled(cleanup),
                }

            safety_kwargs: dict[str, Any] = {"live": True}
            if action != AGREE_REFUND:
                safety_kwargs["action"] = action
            safety = self.orchestrator.validate_aftersale_shipped_refund_safety(
                task_id, **safety_kwargs
            )
            steps.append(
                {
                    "step": "AFTERSALE_REFUND_SAFETY",
                    "status": "success",
                    "safety": safety,
                }
            )
            self._mark_status(
                task_id,
                TaskState.EXECUTING,
                "售后普通退款拦截/退款外部写入即将开始，进程重启后必须人工核验",
            )
            steps.append({"step": "INTERCEPT", "status": "started", "sent": False})
            dispatch = self._call_cancelable(
                self.orchestrator.dispatch_in_transit_interception,
                task_id,
                routing,
            )
            steps[-1].update(
                {"status": "success", "sent": True, "send": dispatch.get("send")}
            )
            steps.append(
                {"step": "AFTERSALE_ACTION", "status": "started", "action": action}
            )
            completion_kwargs: dict[str, Any] = {"live": True}
            if action != AGREE_REFUND:
                completion_kwargs["action"] = action
            completed = self._call_cancelable(
                self.orchestrator.complete_aftersale_shipped_refund_after_interception,
                task_id,
                **completion_kwargs,
            )
            cleanup = dict(completed.get("detail_cleanup") or {})
            # This completion wrapper returns one flat canonical outcome.  Do
            # not prefer a nested worker payload here or outer cooldown/review
            # flags could be lost and an unfinished action misread as success.
            action_result = dict(completed)
            deferred_result = self._handle_aftersale_reject_deferred(
                task_id,
                action,
                action_result,
                steps,
                cleanup,
                routing=routing,
            )
            if deferred_result is not None:
                return deferred_result
            if bool(action_result.get("write_outcome_unknown")) or bool(
                action_result.get("needs_review")
            ) or not bool(action_result.get("ok", True)):
                reason = str(
                    action_result.get("error")
                    or "售后退款动作结果无法核验，禁止自动重试"
                )
                steps[-1].update(
                    {
                        "status": "needs_review",
                        "reason": reason,
                        "result": action_result,
                    }
                )
                self._mark_status(task_id, TaskState.NEED_REVIEW, reason)
                return {
                    "status": "NEED_REVIEW_ACTION_OUTCOME",
                    "steps": steps,
                    "error": reason,
                    "finished_at": _now(),
                    "routing": routing,
                    "detail_closed_inline": self._detail_cleanup_handled(cleanup),
                }
            if completed.get("skipped"):
                reason = str(completed.get("skip_reason") or "提交前状态变化，停止自动退款")
                steps[-1].update(
                    {
                        "status": "skipped",
                        "reason": reason,
                        "business_write_performed": False,
                    }
                )
                state = (
                    TaskState.REJECTED
                    if "退款关闭" in reason
                    else TaskState.NEED_REVIEW
                )
                self._mark_status(task_id, state, reason)
                return {
                    "status": (
                        "SKIPPED_REFUND_CLOSED"
                        if state == TaskState.REJECTED
                        else "NEED_REVIEW_LOGISTICS_CHANGED_AFTER_INTERCEPTION"
                    ),
                    "steps": steps,
                    "error": reason,
                    "finished_at": _now(),
                    "routing": routing,
                    "detail_closed_inline": self._detail_cleanup_handled(cleanup),
                }
            steps[-1].update(
                {
                    "status": "success",
                    "result": completed,
                    "detail_cleanup": cleanup,
                }
            )
            completion_reason = {
                AGREE_REFUND: "售后普通退款拦截成功、已同意退款并完成在线登记",
                AGREE_RETURN_AFTER_REFUND: (
                    "售后已发货退款拦截成功、已同意快递退回后退款"
                    "（超过仓库直接退款阈值，无需在线登记）"
                ),
                AGREE_RECALL_AFTER_REFUND: (
                    "售后已发货退款拦截成功、已同意召回后退款"
                    "（超过仓库直接退款阈值，无需在线登记）"
                ),
                REJECT_REFUND: (
                    "售后已发货退款拦截成功、已驳回退款"
                    "（超过仓库直接退款阈值，无需在线登记）"
                ),
            }[action]
            self._mark_status(task_id, TaskState.COMPLETED, completion_reason)
            return {
                "status": "COMPLETED",
                "steps": steps,
                "finished_at": _now(),
                "routing": routing,
                "result": completed,
                "detail_closed_inline": self._detail_cleanup_handled(cleanup),
            }

        if action == NEED_REVIEW:
            # No-action details are now closed inline by the collector that
            # opened the exact candidate page.  Do not launch a second worker
            # to rediscover the same-order detail unless inline cleanup truly
            # did not complete.
            cleanup = dict(detail.get("detail_cleanup") or {})
            if not self._detail_cleanup_handled(cleanup):
                cleanup = self._call_cancelable(
                    self.orchestrator.close_aftersale_detail, task_id
                )
            self._mark_status(
                task_id,
                TaskState.NEED_REVIEW,
                str(decision.get("reason") or "售后详情没有延长收货，转人工处理"),
            )
            steps.append(
                {
                    "step": "AFTERSALE_DETAIL_CLOSE",
                    "status": "success" if self._detail_cleanup_handled(cleanup) else "warning",
                    "detail_cleanup": cleanup,
                }
            )
            return {
                "status": "NEED_REVIEW_NO_EXTEND_RECEIPT",
                "steps": steps,
                "finished_at": _now(),
                "detail_closed_inline": self._detail_cleanup_handled(cleanup),
            }

        if not production:
            cleanup = self._call_cancelable(
                self.orchestrator.close_aftersale_detail, task_id
            )
            steps.append(
                {
                    "step": "AFTERSALE_ACTION",
                    "status": "preview",
                    "action": action,
                }
            )
            return {
                "status": "READY_FOR_PRODUCTION",
                "steps": steps,
                "finished_at": _now(),
                "detail_closed_inline": self._detail_cleanup_handled(cleanup),
            }

        self._mark_status(
            task_id,
            TaskState.EXECUTING,
            "售后工作台外部写入即将开始，进程重启后必须人工核验",
        )
        self._checkpoint()
        steps.append({"step": "AFTERSALE_ACTION", "status": "started", "action": action})
        completed = self._call_cancelable(
            self.orchestrator.complete_aftersale_action,
            task_id,
            action,
            live=True,
        )
        steps[-1].update({"status": "success", "result": completed.get("result")})
        cleanup = dict(completed.get("detail_cleanup") or {})
        action_result = dict(completed.get("result") or {})
        deferred_result = self._handle_aftersale_reject_deferred(
            task_id,
            action,
            action_result,
            steps,
            cleanup,
        )
        if deferred_result is not None:
            return deferred_result
        if bool(action_result.get("skipped")) and (
            "退款关闭" in str(action_result.get("skip_reason") or "")
        ):
            reason = str(action_result.get("skip_reason") or "售后状态包含退款关闭，按规则忽略")
            steps[-1].update(
                {
                    "status": "skipped",
                    "reason": reason,
                    "business_write_performed": False,
                }
            )
            self._mark_status(task_id, TaskState.REJECTED, reason)
            return {
                "status": "SKIPPED_REFUND_CLOSED",
                "steps": steps,
                "finished_at": _now(),
                "detail_closed_inline": self._detail_cleanup_handled(cleanup),
            }
        if bool(action_result.get("skipped")):
            reason = str(
                action_result.get("skip_reason")
                or "提交前售后状态或当前动作已变化，转人工核验"
            )
            steps[-1].update({"status": "needs_review", "reason": reason})
            self._mark_status(task_id, TaskState.NEED_REVIEW, reason)
            return {
                "status": "NEED_REVIEW_ACTION_CHANGED",
                "steps": steps,
                "error": reason,
                "finished_at": _now(),
                "detail_closed_inline": self._detail_cleanup_handled(cleanup),
            }
        if (
            bool(action_result.get("write_outcome_unknown"))
            or bool(action_result.get("needs_review"))
            or not bool(action_result.get("ok", True))
        ):
            reason = str(
                action_result.get("error")
                or "售后工作台动作未形成可核验终态，转人工核验"
            )
            steps[-1].update(
                {"status": "needs_review", "reason": reason, "result": action_result}
            )
            self._mark_status(task_id, TaskState.NEED_REVIEW, reason)
            return {
                "status": "NEED_REVIEW_ACTION_OUTCOME",
                "steps": steps,
                "error": reason,
                "finished_at": _now(),
                "detail_closed_inline": self._detail_cleanup_handled(cleanup),
            }
        self._mark_status(task_id, TaskState.COMPLETED, "售后工作台工单已完成")
        return {
            "status": "COMPLETED",
            "steps": steps,
            "finished_at": _now(),
            "detail_closed_inline": self._detail_cleanup_handled(cleanup),
        }

    def _handle_aftersale_reject_deferred(
        self,
        task_id: int,
        action: str,
        action_result: dict[str, Any],
        steps: list[dict[str, Any]],
        cleanup: dict[str, Any],
        *,
        routing: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Persist a proven reject cooldown without replaying group messages.

        A retry of the interception branch reuses the already verified group
        message for the same task/tracking/group/message tuple, so the 30-minute
        PDD cooldown never creates a duplicate interception notification.
        """

        if action != REJECT_REFUND or not bool(action_result.get("deferred")):
            return None
        business_write_performed = bool(
            action_result.get("business_write_performed")
        )
        submit_attempted = bool(action_result.get("submit_attempted"))
        reason_code = str(action_result.get("reason_code") or "").strip()
        prompt_text = str(action_result.get("prompt_text") or "").strip()
        try:
            retry_after_seconds = int(action_result.get("retry_after_seconds") or 0)
        except (TypeError, ValueError):
            retry_after_seconds = 0
        pre_submit_cooldown = (
            reason_code == "PDD_REJECT_COOLDOWN"
            and not business_write_performed
            and not submit_attempted
        )
        verified_negotiation_cooldown = (
            reason_code == "PDD_REJECT_NEGOTIATION_COOLDOWN"
            and business_write_performed
            and submit_attempted
            and str(action_result.get("defer_kind") or "")
            == "POST_SUBMIT_VERIFIED"
            and bool(action_result.get("stage_committed"))
            and bool(action_result.get("negotiation_round_submitted"))
            and bool(action_result.get("write_outcome_verified"))
            and not bool(action_result.get("write_outcome_unknown"))
        )
        if (
            not (pre_submit_cooldown or verified_negotiation_cooldown)
            or not bool(action_result.get("retryable"))
            or retry_after_seconds < 30 * 60
            or not prompt_text
        ):
            reason = "售后驳回返回了不完整或写入后的冷却状态，转人工核验"
            steps[-1].update(
                {
                    "status": "needs_review",
                    "reason": reason,
                    "result": action_result,
                }
            )
            self._mark_status(task_id, TaskState.NEED_REVIEW, reason)
            result = {
                "status": "NEED_REVIEW_UNSAFE_RETRY_STATE",
                "steps": steps,
                "error": reason,
                "finished_at": _now(),
                "detail_closed_inline": self._detail_cleanup_handled(cleanup),
            }
            if routing is not None:
                result["routing"] = routing
            return result
        retry_not_before = (
            datetime.now() + timedelta(seconds=retry_after_seconds)
        ).isoformat(timespec="seconds")
        defer_retry = getattr(self.repository, "defer_task_retry", None)
        if not callable(defer_retry):
            reason = "仓储层不支持售后冷却持久化，转人工核验"
            steps[-1].update(
                {
                    "status": "needs_review",
                    "reason": reason,
                    "result": action_result,
                }
            )
            self._mark_status(task_id, TaskState.NEED_REVIEW, reason)
            result = {
                "status": "NEED_REVIEW_RETRY_STORAGE_UNAVAILABLE",
                "steps": steps,
                "error": reason,
                "finished_at": _now(),
                "detail_closed_inline": self._detail_cleanup_handled(cleanup),
            }
            if routing is not None:
                result["routing"] = routing
            return result
        get_task = getattr(self.repository, "get_task", None)
        task = get_task(task_id) if callable(get_task) else {}
        task = task or {}
        facts = dict(task.get("facts") or {})
        retry_context = {
            "business_write_performed": business_write_performed,
            "submit_attempted": submit_attempted,
            "defer_kind": str(action_result.get("defer_kind") or "PRE_SUBMIT"),
            "stage_committed": bool(action_result.get("stage_committed")),
            "negotiation_round_submitted": bool(
                action_result.get("negotiation_round_submitted")
            ),
            "resume_action": str(action_result.get("resume_action") or action),
            "reject_surface": str(action_result.get("reject_surface") or ""),
            "shop_id": str(facts.get("shop_id") or ""),
            "order_no": str(facts.get("order_no") or ""),
            "aftersale_no": str(facts.get("aftersale_no") or ""),
        }
        try:
            parameters = inspect.signature(defer_retry).parameters.values()
            supports_context = len(inspect.signature(defer_retry).parameters) >= 5 or any(
                parameter.kind is inspect.Parameter.VAR_POSITIONAL
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_context = False
        retry_args = (
            (task_id, retry_not_before, reason_code, prompt_text, retry_context)
            if supports_context
            else (task_id, retry_not_before, reason_code, prompt_text)
        )
        retry = defer_retry(*retry_args)
        steps[-1].update(
            {
                "status": "deferred",
                "reason": prompt_text,
                "retry": retry,
                "business_write_performed": business_write_performed,
                "submit_attempted": submit_attempted,
            }
        )
        result = {
            "status": "RETRY_PENDING_REJECT_COOLDOWN",
            "steps": steps,
            "retry": retry,
            "finished_at": _now(),
            "detail_closed_inline": self._detail_cleanup_handled(cleanup),
        }
        if routing is not None:
            result["routing"] = routing
        return result

    def _finish_in_transit_pdd_after_interception(
        self, task_id: int, steps: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Run the single PDD post-interception sequence for every carrier.

        中通、韵达、顺丰、圆通 differ only in how ``INTERCEPT`` sends the
        message.  Once the send has succeeded, the PDD detail workflow is
        carrier-neutral: it handles either the normal 主观原因 page or the
        alternate 处理情况 page from the refreshed latest logistics.
        """
        self._checkpoint()
        completed = self.orchestrator.complete_in_transit_after_interception(
            task_id,
            live=True,
        )
        refreshed = dict(completed.get("refreshed") or {})
        steps.append(
            {
                "step": "PDD_REFRESH_LOGISTICS",
                "status": "success",
                "latest_logistics_text": refreshed.get("latest_logistics_text"),
            }
        )
        routing = dict(completed.get("routing") or {})
        if not routing.get("ok"):
            raise RuntimeError(routing.get("reason") or "拦截后物流路由无法匹配")
        reason = dict(completed.get("reason") or {})
        steps.append(
            {
                "step": "PDD_REFUND_REASON",
                "status": "success",
                "trace": reason.get("trace"),
            }
        )
        routing_action = str(completed.get("requested_action") or "REJECT_REFUND")
        final_action = str(completed.get("effective_action") or routing_action)
        if final_action != routing_action:
            routing["routing_final_action"] = routing_action
            routing["final_action"] = final_action
        routing["detail_form"] = reason.get("detail_form")
        routing["detail_decision"] = reason.get("decision")
        terminal = dict(completed.get("terminal") or {})
        submission_ineffective = bool(terminal.get("submission_ineffective"))
        steps.append(
            {
                "step": "PDD_REFUND_FINAL",
                "status": "skipped" if submission_ineffective else "success",
                "action": terminal.get("effective_action") or final_action,
                "trace": terminal.get("trace"),
                "reason": terminal.get("skip_reason", ""),
            }
        )
        detail_cleanup = dict(completed.get("detail_cleanup") or {})
        if detail_cleanup:
            steps.append(
                {
                    "step": "PDD_DETAIL_CLOSE",
                    "status": "success"
                    if self._detail_cleanup_handled(detail_cleanup)
                    else "warning",
                    "detail_tabs_closed": int(detail_cleanup.get("closed") or 0),
                    "detail_tabs_navigated": int(detail_cleanup.get("navigated") or 0),
                    "list_ready": bool(detail_cleanup.get("list_ready")),
                    "error": detail_cleanup.get("error", ""),
                    "inline": True,
                }
            )
        routing["detail_cleanup"] = detail_cleanup
        routing["submission_ineffective"] = submission_ineffective
        routing["skip_reason"] = str(terminal.get("skip_reason") or "")
        return routing

    def _mark_status(self, task_id: int, target: TaskState, reason: str) -> None:
        mark_status = getattr(self.repository, "mark_task_status", None)
        if callable(mark_status):
            mark_status(task_id, target, reason)
        if target in {
            TaskState.COMPLETED,
            TaskState.REJECTED,
            TaskState.NEED_REVIEW,
            TaskState.FAILED,
        }:
            clear_retry = getattr(self.repository, "clear_task_retry", None)
            if callable(clear_retry):
                clear_retry(task_id, reason)

    @staticmethod
    def _detail_cleanup_handled(cleanup: dict[str, Any] | None) -> bool:
        data = dict(cleanup or {})
        return bool(
            data.get("list_ready")
            and (
                data.get("handled")
                or data.get("closed")
                or data.get("navigated")
            )
        )

    def _has_real_refund_write(self, task_id: int) -> bool:
        list_logs = getattr(self.repository, "list_logs", None)
        if not callable(list_logs):
            return False
        return any(
            item.get("action_type") in {"EXECUTE_PDD_REAL_REFUND", "RECONCILE_PDD_REAL_REFUND"}
            and item.get("status") == "success"
            and (item.get("details") or {}).get("business_write_performed")
            for item in list_logs(task_id=task_id, limit=100)
        )

    def _wait_for_next_cycle(self, cycle: int) -> None:
        interval = float(self._status.get("poll_interval_seconds") or self._poll_interval_seconds())
        next_at = datetime.now() + timedelta(seconds=interval)
        self._update(
            state="RUNNING",
            phase=f"第 {cycle} 轮完成，等待下一轮同步（{int(interval)} 秒）",
            current_task=None,
            next_sync_at=next_at.isoformat(timespec="seconds"),
        )
        deadline = time.monotonic() + interval
        while True:
            self._checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(0.5, remaining))

    def _run(self) -> None:
        try:
            production = self._status.get("mode") == "production"
            continuous = bool(self._status.get("continuous"))
            queue_only = bool(self._status.get("queue_only"))
            workflow_keys = list(
                self._status.get("workflow_keys")
                or [
                    "shipped_no_trace",
                    "in_transit_no_reason",
                    "confirm_earlier_shipping",
                ]
            )
            breaker = int(self.orchestrator.config.raw.get("safety", {}).get("global_failure_circuit_breaker", 3))
            cycle_errors = 0
            pending_recovery_snapshot = dict(self._recovery_snapshot)
            while True:
                self._checkpoint()
                cycle = int(self._status.get("cycle") or 0) + 1
                self._update(
                    state="RUNNING",
                    cycle=cycle,
                    phase=(
                        "读取当前实时待处理队列（跳过店铺同步）"
                        if queue_only
                        else f"第 {cycle} 轮：同步全部店铺工单并筛选目标"
                    ),
                    current_task=None,
                    next_sync_at="",
                )
                try:
                    if queue_only:
                        discovery = self._frozen_queue_discovery(
                            [int(item) for item in self._status.get("queue_task_ids", [])]
                        )
                    elif pending_recovery_snapshot:
                        discovery = self._call_cancelable(
                            self.orchestrator.discover_live_readonly,
                            resume_snapshot=pending_recovery_snapshot
                        )
                    else:
                        discovery = self._call_cancelable(self.orchestrator.discover_live_readonly)
                    pending_recovery_snapshot = {}
                    self._recovery_snapshot = {}
                    self._update(
                        recovery_active=False,
                        recovery_attempts=0,
                        recovery_of="",
                    )
                    cycle_errors = 0
                except InterruptedError:
                    raise
                except Exception as exc:
                    cycle_errors += 1
                    failure_phase = (
                        f"读取已冻结当前队列失败：{exc}"
                        if queue_only
                        else f"第 {cycle} 轮同步失败：{exc}"
                    )
                    self._update(last_error=str(exc), phase=failure_phase)
                    if queue_only:
                        raise RuntimeControllerError(failure_phase) from exc
                    if not continuous or cycle_errors >= breaker:
                        raise RuntimeControllerError(f"连续同步失败达到熔断阈值 {breaker}：{exc}") from exc
                    self._wait_for_next_cycle(cycle)
                    continue

                self._checkpoint()
                queue = self._build_queue(discovery, workflow_keys)
                sync = discovery.get("sync") or {}
                shop_sync_errors = [
                    item for item in discovery.get("results", []) if not item.get("ok")
                ]
                aftersale_sync_errors = [
                    item
                    for item in discovery.get("results", [])
                    if str(item.get("aftersale_error") or "").strip()
                ]
                sync_error_messages: list[str] = []
                for item in shop_sync_errors:
                    shop_label = str(
                        item.get("shop_name") or item.get("shop_id") or "未知店铺"
                    )
                    sync_error_messages.append(
                        f"{shop_label}：{str(item.get('error') or '店铺同步失败')}"
                    )
                for item in aftersale_sync_errors:
                    shop_label = str(
                        item.get("shop_name") or item.get("shop_id") or "未知店铺"
                    )
                    sync_error_messages.append(
                        f"{shop_label}：售后待处理扫描失败、已保留上轮数据："
                        f"{str(item.get('aftersale_error') or '').strip()}"
                    )
                phase = (
                    f"已读取当前实时队列，准备单轮处理 {len(queue)} 条目标工单/售后单"
                    if queue_only
                    else f"第 {cycle} 轮同步完成，发现 {len(queue)} 条目标工单/售后单"
                )
                cycle_sync_notice = ""
                if not queue_only:
                    notices = []
                    if shop_sync_errors:
                        notices.append(
                            f"{len(shop_sync_errors)} 家店铺同步失败，将在下一轮重试"
                        )
                    if aftersale_sync_errors:
                        notices.append(
                            f"{len(aftersale_sync_errors)} 家店铺售后待处理扫描失败、"
                            "已保留上轮数据"
                        )
                    cycle_sync_notice = "；".join(notices)
                    if cycle_sync_notice:
                        phase += f"；{cycle_sync_notice}"
                self._update(
                    queue=queue,
                    counts={"total": len(queue), "completed": 0, "failed": 0, "skipped": 0},
                    last_sync=sync,
                    last_error="；".join(sync_error_messages),
                    phase=phase,
                )
                consecutive_failures = 0
                for queue_item in queue:
                    self._checkpoint()
                    manual_state = self._queued_task_manual_state(queue_item["task_id"])
                    if manual_state is not None:
                        counts = dict(self._status["counts"])
                        result = {
                            **queue_item,
                            "started_at": _now(),
                            "finished_at": _now(),
                            "status": "SKIPPED_MANUAL_STATUS",
                            "error": f"任务已人工改为{manual_state.value}，跳过本轮自动处理",
                            "steps": [
                                {
                                    "step": "MANUAL_STATUS_CHECK",
                                    "status": "skipped",
                                    "state": manual_state.value,
                                }
                            ],
                        }
                        counts["skipped"] += 1
                        results = [*self._status.get("results", []), result][-200:]
                        self._update(
                            results=results,
                            counts=counts,
                            current_task=None,
                            phase=f"跳过人工状态工单：{queue_item.get('order_no') or queue_item['task_id']}",
                        )
                        consecutive_failures = 0
                        continue
                    initial_phase = (
                        "读取千牛售后详情与旺店通订单"
                        if queue_item.get("workflow_key") == "qianniu_unreceived_refund"
                        else "读取拼多多工单详情与快递单号"
                    )
                    self._update(current_task=queue_item, phase=initial_phase)
                    result = {**queue_item, "started_at": _now(), "steps": []}
                    detail_closed_inline = False
                    try:
                        if queue_item.get("workflow_key") == "qianniu_unreceived_refund":
                            if production:
                                self._mark_status(
                                    queue_item["task_id"],
                                    TaskState.COLLECTING_DATA,
                                    "自动循环开始处理第12家千牛未收货退款",
                                )
                            self._update(
                                phase="第12家千牛：旺店通校验、群拦截、钉钉登记与拦截快递"
                            )
                            raise _QianniuFlowCompleted(
                                self._call_cancelable(
                                    self.orchestrator.process_qianniu_task,
                                    queue_item["task_id"],
                                    live=production,
                                )
                            )
                        if queue_item.get("workflow_key") in {
                            "aftersale_return_refund",
                            "aftersale_shipped_refund",
                        }:
                            if production:
                                self._mark_status(
                                    queue_item["task_id"],
                                    TaskState.COLLECTING_DATA,
                                    "自动循环开始处理售后工作台工单",
                                )
                            self._update(phase="执行售后工作台固定组件")
                            raise _AftersaleFlowCompleted(
                                self._process_aftersale_item(
                                    queue_item,
                                    production,
                                    steps=result["steps"],
                                )
                            )
                        if queue_item.get("workflow_key") == "confirm_earlier_shipping":
                            if production:
                                self._mark_status(
                                    queue_item["task_id"],
                                    TaskState.COLLECTING_DATA,
                                    "自动循环开始处理确认更早发货时间工单",
                                )
                            self._update(phase="执行确认更早发货时间固定组件")
                            raise _EarlierShippingFlowCompleted(
                                self._process_earlier_shipping_item(
                                    queue_item,
                                    production,
                                    steps=result["steps"],
                                )
                            )
                        if queue_item.get("workflow_key") == "in_transit_no_reason":
                            if production:
                                self._mark_status(
                                    queue_item["task_id"],
                                    TaskState.COLLECTING_DATA,
                                    "自动循环开始处理在途无理由工单",
                                )
                            self._update(phase="执行在途无理由拦截与退款路由")
                            raise _InTransitFlowCompleted(
                                self._process_in_transit_item(
                                    queue_item,
                                    production,
                                    steps=result["steps"],
                                )
                            )
                        if production:
                            self._mark_status(
                                queue_item["task_id"], TaskState.COLLECTING_DATA, "自动循环开始采集工单数据"
                            )
                        for attempt in range(1, 3):
                            try:
                                detail = self.orchestrator.collect_task_detail_readonly(
                                    queue_item["task_id"], keep_detail_open=True
                                )
                                result["steps"].append(
                                    {
                                        "step": "PDD_DETAIL",
                                        "status": "success",
                                        "attempt": attempt,
                                        "tracking_no": detail.get("tracking_no"),
                                        "detail_retained": bool(detail.get("detail_retained")),
                                    }
                                )
                                break
                            except Exception as exc:
                                if isinstance(exc, (PddOrderNotFoundError, PddAlreadyProcessedError)):
                                    raise
                                result["steps"].append({"step": "PDD_DETAIL", "status": "retry" if attempt == 1 else "failed", "attempt": attempt, "error": str(exc)})
                                if attempt == 2:
                                    raise
                                if self._stop.wait(1):
                                    raise InterruptedError("用户立即停止运行")
                        self._checkpoint()
                        self._update(phase="旺店通查询订单与退款状态")
                        wdt = self._call_cancelable(
                            self.orchestrator.check_wdt_by_components,
                            queue_item["task_id"],
                        )
                        result["steps"].append(
                            {
                                "step": "WDT_CHECK",
                                "status": "success",
                                "order_status": wdt.get("order_status"),
                                "warehouse": wdt.get("warehouse"),
                                "logistics_company": wdt.get("logistics_company"),
                                "requires_interception": bool(wdt.get("requires_interception")),
                            }
                        )
                        if (
                            queue_item.get("workflow_key") == "shipped_no_trace"
                            and bool(wdt.get("requires_interception"))
                        ):
                            self._update(
                                phase="已发货无轨迹：发送快递拦截并在回读成功后退款"
                            )
                            raise _ShippedInterceptionFlowCompleted(
                                self._process_shipped_interception_after_wdt(
                                    queue_item,
                                    production,
                                    steps=result["steps"],
                                )
                            )
                        if production:
                            self._checkpoint()
                            self._update(phase="预检并执行拼多多 UIA 退款组件")
                            self._mark_status(
                                queue_item["task_id"], TaskState.EXECUTING, "旺店通校验通过，开始预检并执行退款"
                            )
                            current_task = self.repository.get_task(queue_item["task_id"])
                            if not current_task:
                                raise RuntimeControllerError(f"任务在执行前消失：{queue_item['task_id']}")
                            current_facts = current_task["facts"]
                            real = self.orchestrator.execute_refund_pilot(
                                queue_item["task_id"],
                                str(current_facts.get("order_no") or ""),
                                float(current_facts.get("refund_amount") or 0),
                                reconcile_inline=True,
                                close_inline=True,
                            )
                            preflight = dict(real.get("preflight") or {})
                            result["steps"].append(
                                {
                                    "step": "PDD_REFUND_PREVIEW",
                                    "status": "success",
                                    "capture_pending": preflight.get("capture_pending", []),
                                    "detail_retained": bool(preflight.get("detail_retained")),
                                    "inline": True,
                                }
                            )
                            result["steps"].append(
                                {
                                    "step": "PDD_REFUND_EXECUTE",
                                    "status": "success",
                                    "refund_amount": real.get("refund_amount"),
                                    "state": real.get("success"),
                                }
                            )
                            reconciled = dict(real.get("reconciliation") or {})
                            result["steps"].append(
                                {
                                    "step": "PDD_REFUND_RECONCILE",
                                    "status": "success",
                                    "state": reconciled.get("success"),
                                    "inline": True,
                                }
                            )
                            detail_cleanup = dict(real.get("detail_cleanup") or {})
                            detail_closed_inline = self._detail_cleanup_handled(
                                detail_cleanup
                            )
                            if detail_cleanup:
                                result["steps"].append(
                                    {
                                        "step": "PDD_LIST_RECOVER",
                                        "status": "success"
                                        if detail_cleanup.get("list_ready")
                                        else "warning",
                                        "list_ready": bool(detail_cleanup.get("list_ready")),
                                        "detail_tabs_closed": int(
                                            detail_cleanup.get("closed") or 0
                                        ),
                                        "detail_tabs_navigated": int(
                                            detail_cleanup.get("navigated") or 0
                                        ),
                                        "inline": True,
                                    }
                                )
                            result.update({"status": "COMPLETED", "finished_at": _now()})
                        else:
                            self._checkpoint()
                            self._update(phase="预检拼多多退款固定组件")
                            preview = self.orchestrator.preview_refund_components(
                                queue_item["task_id"], retain_detail=True
                            )
                            result["steps"].append(
                                {
                                    "step": "PDD_REFUND_PREVIEW",
                                    "status": "success",
                                    "capture_pending": preview.get("capture_pending", []),
                                    "detail_retained": bool(preview.get("detail_retained")),
                                }
                            )
                            result.update({"status": "READY_FOR_PRODUCTION", "finished_at": _now()})
                        consecutive_failures = 0
                        counts = dict(self._status["counts"])
                        counts["completed"] += 1
                    except _InTransitFlowCompleted as completed:
                        counts = dict(self._status["counts"])
                        result.update(completed.result)
                        detail_closed_inline = bool(completed.result.get("detail_closed_inline"))
                        if str(completed.result.get("status") or "").startswith("SKIPPED_"):
                            counts["skipped"] += 1
                        else:
                            counts["completed"] += 1
                        consecutive_failures = 0
                    except _ShippedInterceptionFlowCompleted as completed:
                        counts = dict(self._status["counts"])
                        result.update(completed.result)
                        detail_closed_inline = bool(
                            completed.result.get("detail_closed_inline")
                        )
                        if str(completed.result.get("status") or "").startswith(
                            "NEED_REVIEW"
                        ):
                            counts["skipped"] += 1
                        else:
                            counts["completed"] += 1
                        consecutive_failures = 0
                    except _EarlierShippingFlowCompleted as completed:
                        counts = dict(self._status["counts"])
                        result.update(completed.result)
                        detail_closed_inline = bool(
                            completed.result.get("detail_closed_inline")
                        )
                        if str(completed.result.get("status") or "").startswith("SKIPPED_"):
                            counts["skipped"] += 1
                        else:
                            counts["completed"] += 1
                        consecutive_failures = 0
                    except _AftersaleFlowCompleted as completed:
                        counts = dict(self._status["counts"])
                        result.update(completed.result)
                        detail_closed_inline = bool(
                            completed.result.get("detail_closed_inline")
                        )
                        aftersale_status = str(
                            completed.result.get("status") or ""
                        )
                        if aftersale_status.startswith(
                            ("SKIPPED_", "NEED_REVIEW", "RETRY_PENDING")
                        ):
                            counts["skipped"] += 1
                        else:
                            counts["completed"] += 1
                        consecutive_failures = 0
                    except _QianniuFlowCompleted as completed:
                        counts = dict(self._status["counts"])
                        result.update(completed.result)
                        if str(completed.result.get("status") or "").startswith(
                            ("SKIPPED_", "NEED_REVIEW")
                        ):
                            counts["skipped"] += 1
                        else:
                            counts["completed"] += 1
                        consecutive_failures = 0
                    except PddOrderNotFoundError as exc:
                        counts = dict(self._status["counts"])
                        result["steps"].append({"step": "PDD_DETAIL", "status": "skipped", "reason": str(exc)})
                        result.update({"status": "SKIPPED_NOT_FOUND", "error": str(exc), "finished_at": _now()})
                        self._mark_status(queue_item["task_id"], TaskState.REJECTED, "订单号在当前待处理列表中不存在，跳过")
                        counts["skipped"] += 1
                        consecutive_failures = 0
                    except PddAlreadyProcessedError as exc:
                        counts = dict(self._status["counts"])
                        result["steps"].append({"step": "PDD_DETAIL", "status": "skipped", "reason": str(exc)})
                        result.update({"status": "SKIPPED_ALREADY_PROCESSED", "error": str(exc), "finished_at": _now()})
                        self._mark_status(queue_item["task_id"], TaskState.REJECTED, "工单已处理或退款进行中，跳过")
                        counts["skipped"] += 1
                        consecutive_failures = 0
                    except Exception as exc:
                        counts = dict(self._status["counts"])
                        error = str(exc)
                        if self._stop.is_set():
                            # A child killed by the immediate-stop button may
                            # surface as a normal non-zero worker failure.  It
                            # is not a business failure and must never be
                            # retried as an automatic refund.
                            result.update(
                                {
                                    "status": "STOPPED",
                                    "error": "用户立即停止运行",
                                    "finished_at": _now(),
                                }
                            )
                            self._mark_status(
                                queue_item["task_id"],
                                TaskState.NEED_REVIEW,
                                "用户立即停止运行，当前工单停止后需人工复核",
                            )
                            counts["skipped"] += 1
                            consecutive_failures = 0
                        elif (
                            production
                            and queue_item.get("workflow_key")
                            == "qianniu_unreceived_refund"
                            and self._queued_task_manual_state(queue_item["task_id"])
                            is TaskState.NEED_REVIEW
                        ):
                            result.update(
                                {
                                    "status": "NEED_REVIEW_EXTERNAL_WRITE",
                                    "error": error,
                                    "finished_at": _now(),
                                }
                            )
                            counts["skipped"] += 1
                            consecutive_failures = 0
                        elif any(
                            marker in error
                            for marker in (
                                "页面身份不符",
                                "页面店铺身份不符",
                                "页面订单身份不符",
                                "页面语义未就绪",
                            )
                        ):
                            result.update(
                                {
                                    "status": "NEED_REVIEW_PAGE_IDENTITY",
                                    "error": error,
                                    "finished_at": _now(),
                                }
                            )
                            result["steps"].append(
                                {
                                    "step": "PDD_PAGE_IDENTITY",
                                    "status": "blocked",
                                    "error": error,
                                }
                            )
                            self._mark_status(
                                queue_item["task_id"],
                                TaskState.NEED_REVIEW,
                                "拼多多页面类型、店铺或订单身份不符，已禁止跨页面操作",
                            )
                            counts["skipped"] += 1
                            consecutive_failures = 0
                        elif (
                            production
                            and queue_item.get("workflow_key")
                            in {
                                "shipped_no_trace",
                                "in_transit_no_reason",
                                "aftersale_shipped_refund",
                            }
                            and any(
                                step.get("step") in {"INTERCEPT", "AFTERSALE_INTERCEPT"}
                                for step in result.get("steps", [])
                            )
                        ):
                            # A desktop message can be delivered immediately
                            # before a focus/readback error is raised.  Once
                            # the interception component was entered, fail
                            # closed rather than sending the same number again
                            # on the next polling round.
                            result.update({"status": "NEED_REVIEW_EXTERNAL_WRITE", "error": error, "finished_at": _now()})
                            self._mark_status(
                                queue_item["task_id"],
                                TaskState.NEED_REVIEW,
                                "快递拦截已开始但未完成终态核验，禁止自动重试",
                            )
                            counts["skipped"] += 1
                            consecutive_failures = 0
                        elif (
                            production
                            and queue_item.get("workflow_key") == "confirm_earlier_shipping"
                            and any(
                                step.get("step") == "PDD_EARLIER_SHIPPING"
                                for step in result.get("steps", [])
                            )
                        ):
                            result.update(
                                {
                                    "status": "NEED_REVIEW_EXTERNAL_WRITE",
                                    "error": error,
                                    "finished_at": _now(),
                                }
                            )
                            self._mark_status(
                                queue_item["task_id"],
                                TaskState.NEED_REVIEW,
                                "确认更早发货时间提交已开始但终态未核验，禁止自动重试",
                            )
                            counts["skipped"] += 1
                            consecutive_failures = 0
                        elif (
                            production
                            and queue_item.get("workflow_key")
                            in {"aftersale_return_refund", "aftersale_shipped_refund"}
                            and any(
                                step.get("step") == "AFTERSALE_ACTION"
                                and step.get("status") == "started"
                                for step in result.get("steps", [])
                            )
                        ):
                            result.update(
                                {
                                    "status": "NEED_REVIEW_EXTERNAL_WRITE",
                                    "error": error,
                                    "finished_at": _now(),
                                }
                            )
                            self._mark_status(
                                queue_item["task_id"],
                                TaskState.NEED_REVIEW,
                                "售后工作台提交已开始但终态未核验，禁止自动重试",
                            )
                            counts["skipped"] += 1
                            consecutive_failures = 0
                        elif (
                            queue_item.get("workflow_key")
                            in {"aftersale_return_refund", "aftersale_shipped_refund"}
                            and "当前已不在待处理列表" in error
                        ):
                            result.update(
                                {
                                    "status": "SKIPPED_NOT_FOUND",
                                    "error": error,
                                    "finished_at": _now(),
                                }
                            )
                            self._mark_status(
                                queue_item["task_id"],
                                TaskState.REJECTED,
                                "售后订单当前已不在待处理列表，跳过",
                            )
                            counts["skipped"] += 1
                            consecutive_failures = 0
                        elif production and self._has_real_refund_write(queue_item["task_id"]):
                            result.update({"status": "VERIFYING_REAL_WRITE", "error": error, "finished_at": _now()})
                            self._mark_status(
                                queue_item["task_id"],
                                TaskState.VERIFYING,
                                "已存在真实退款写入记录，停止重试并等待结果核验",
                            )
                            counts["skipped"] += 1
                            consecutive_failures = 0
                        elif "订单状态不允许退款" in error:
                            status = error.rsplit("订单状态不允许退款：", 1)[-1].splitlines()[0].strip()
                            reason = f"旺店通订单状态不符合退款条件：{status or '未知'}"
                            result["steps"].append({"step": "WDT_CHECK", "status": "skipped", "reason": reason})
                            result.update({"status": "SKIPPED_NOT_ELIGIBLE", "error": reason, "finished_at": _now()})
                            self._mark_status(
                                queue_item["task_id"],
                                TaskState.COMPLETED,
                                "旺店通订单状态不在自动退款名单，已按规则处理且未退款",
                            )
                            counts["skipped"] += 1
                            consecutive_failures = 0
                        elif "安全上限" in error or "已有真实退款写入记录" in error:
                            result.update({"status": "SKIPPED_SAFETY_GUARD", "error": error, "finished_at": _now()})
                            self._mark_status(queue_item["task_id"], TaskState.REJECTED, "安全保护拦截，未执行退款")
                            counts["skipped"] += 1
                            consecutive_failures = 0
                        elif "PDD_DETAIL_WORKER_CRASHED" in error or "PDD_DETAIL_WORKER_TIMEOUT" in error:
                            result.update({"status": "PDD_DETAIL_WORKER_CRASHED", "error": error, "finished_at": _now()})
                            result["steps"].append({"step": "PDD_DETAIL", "status": "worker_crashed", "error": error})
                            self._mark_status(
                                queue_item["task_id"],
                                TaskState.NEED_REVIEW,
                                "拼多多详情页隔离 Worker 异常，未执行退款，等待人工复核",
                            )
                            counts["skipped"] += 1
                            consecutive_failures = 0
                        elif "WDT_WORKER_CRASHED" in error or "WDT_WORKER_TIMEOUT" in error:
                            result.update({"status": "WDT_WORKER_CRASHED", "error": error, "finished_at": _now()})
                            result["steps"].append({"step": "WDT_CHECK", "status": "worker_crashed", "error": error})
                            self._mark_status(
                                queue_item["task_id"],
                                TaskState.NEED_REVIEW,
                                "旺店通隔离 Worker 异常，未执行退款，等待人工复核",
                            )
                            counts["skipped"] += 1
                            consecutive_failures = 0
                        elif "PDD_REFUND_WORKER" in error:
                            # A child can submit immediately before losing its
                            # IPC channel. Never requeue a money action after a
                            # post-WDT UIA worker failure; require manual review.
                            result.update({"status": "PDD_REFUND_WORKER_FAILED", "error": error, "finished_at": _now()})
                            result["steps"].append({"step": "PDD_REFUND", "status": "worker_failed", "error": error})
                            self._mark_status(
                                queue_item["task_id"],
                                TaskState.NEED_REVIEW,
                                "拼多多退款 UIA Worker 异常，未自动重试，等待人工复核",
                            )
                            counts["skipped"] += 1
                            consecutive_failures = 0
                        else:
                            result.update({"status": "FAILED", "error": error, "finished_at": _now()})
                            self._mark_status(queue_item["task_id"], TaskState.FAILED, f"自动循环失败：{error}")
                            consecutive_failures += 1
                            counts["failed"] += 1
                    if queue_item.get("workflow_key") == "qianniu_unreceived_refund":
                        cleanup = None
                        cleanup_args = ()
                    elif queue_item.get("workflow_key") in {
                        "aftersale_return_refund",
                        "aftersale_shipped_refund",
                    }:
                        cleanup = getattr(self.orchestrator, "close_aftersale_detail", None)
                        cleanup_args = (queue_item["task_id"],)
                    else:
                        cleanup = getattr(self.orchestrator, "close_active_shop_detail", None)
                        cleanup_args = (queue_item["shop_id"],)
                    if callable(cleanup) and not detail_closed_inline:
                        try:
                            restored = self._call_cancelable(cleanup, *cleanup_args)
                            if self._detail_cleanup_handled(restored):
                                result["steps"].append(
                                    {
                                        "step": "PDD_LIST_RECOVER",
                                        "status": "success" if restored.get("list_ready") else "warning",
                                        "list_ready": bool(restored.get("list_ready")),
                                        "detail_tabs_closed": int(restored.get("closed") or 0),
                                        "detail_tabs_navigated": int(
                                            restored.get("navigated") or 0
                                        ),
                                    }
                                )
                        except Exception as cleanup_error:
                            result["steps"].append(
                                {
                                    "step": "PDD_LIST_RECOVER",
                                    "status": "warning",
                                    "error": str(cleanup_error),
                                }
                            )
                    results = [*self._status.get("results", []), result][-200:]
                    self._update(results=results, counts=counts)
                    if self._stop.is_set():
                        raise InterruptedError("用户立即停止运行")
                    if consecutive_failures >= breaker:
                        raise RuntimeControllerError(f"连续失败达到熔断阈值 {breaker}，已停止运行")

                if not continuous:
                    completion_phase = (
                        "当前实时队列单轮处理完成"
                        if queue_only
                        else (
                            "正式执行队列处理完成"
                            if production
                            else "只读演练队列处理完成"
                        )
                    )
                    if cycle_sync_notice:
                        completion_phase += f"；{cycle_sync_notice}"
                    self._update(
                        state="COMPLETED",
                        phase=completion_phase,
                        current_task=None,
                        finished_at=_now(),
                    )
                    return
                self._wait_for_next_cycle(cycle)
        except InterruptedError as exc:
            self._update(state="STOPPED", phase=str(exc), current_task=None, next_sync_at="", finished_at=_now())
        except Exception as exc:
            self._update(state="FAILED", phase="运行失败", last_error=str(exc), current_task=None, next_sync_at="", finished_at=_now())
