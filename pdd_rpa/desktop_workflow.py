from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import time
import uuid

from .db import Repository
from .desktop_executor import DesktopComponentExecutor, DesktopExecutionError


class DesktopWorkflowRunner:
    def __init__(self, repository: Repository, evidence_dir: Path):
        self.repository = repository
        self.evidence_dir = Path(evidence_dir)

    @staticmethod
    def _node_enabled(node: dict[str, Any], context: dict[str, Any]) -> bool:
        """Evaluate an exact context guard without adding a second workflow.

        WDT queries for 示例自营仓 and 示例代发总仓 share the expensive
        search/unique-row verification.  Only the latter needs the lower goods
        grid, so guarded nodes keep that extra UI work out of the normal path.
        """
        guard = node.get("when") or {}
        if not isinstance(guard, dict):
            return False
        return all(str(context.get(key) or "").strip() == str(value or "").strip()
                   for key, value in guard.items())

    def run(
        self,
        workflow_id: str,
        context: dict[str, Any],
        task_id: int | None = None,
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        workflow = self.repository.get_workflow(workflow_id)
        if not workflow:
            raise DesktopExecutionError(f"桌面流程不存在：{workflow_id}")
        trace = []
        execution_id = execution_id or f"{workflow_id}:{task_id or 'adhoc'}:{uuid.uuid4().hex[:12]}"
        executor = DesktopComponentExecutor(self.evidence_dir)
        for index, node in enumerate(workflow.get("nodes") or [], start=1):
            component_id = str(node.get("component_id", ""))
            component = self.repository.get_component(component_id)
            if not component:
                raise DesktopExecutionError(f"流程组件不存在：{component_id}")
            started = datetime.now().isoformat(timespec="seconds")
            if node.get("when") and not self._node_enabled(node, context):
                trace.append(
                    {
                        "index": index,
                        "component_id": component_id,
                        "name": component["name"],
                        "status": "skipped",
                        "started_at": started,
                        "when": dict(node.get("when") or {}),
                    }
                )
                continue
            checkpoint = {
                "workflow_id": workflow_id,
                "execution_id": execution_id,
                "index": index,
                "node_id": str(node.get("node_id", "")),
                "component_id": component_id,
                "name": component["name"],
                "started_at": started,
                "engine": "wdt_worker" if task_id is not None else "in_process",
            }
            if task_id is not None:
                self.repository.log_action(
                    task_id,
                    f"{execution_id}:{index}:started",
                    "WDT_COMPONENT_CHECKPOINT",
                    "started",
                    {**checkpoint, "checkpoint": "before_execute"},
                )
            try:
                execute_started = time.perf_counter()
                value = executor.execute(component, context)
                duration_ms = round((time.perf_counter() - execute_started) * 1000, 1)
                if task_id is not None:
                    self.repository.log_action(
                        task_id,
                        f"{execution_id}:{index}:success",
                        "WDT_COMPONENT_CHECKPOINT",
                        "success",
                        {
                            **checkpoint,
                            "checkpoint": "after_execute",
                            "finished_at": datetime.now().isoformat(timespec="seconds"),
                            "duration_ms": duration_ms,
                            "value": value,
                        },
                    )
                trace.append(
                    {
                        "index": index,
                        "component_id": component_id,
                        "name": component["name"],
                        "status": "success",
                        "started_at": started,
                        "duration_ms": duration_ms,
                        "value": value,
                    }
                )
            except Exception as exc:
                duration_ms = round((time.perf_counter() - execute_started) * 1000, 1)
                if task_id is not None:
                    self.repository.log_action(
                        task_id,
                        f"{execution_id}:{index}:failed",
                        "WDT_COMPONENT_CHECKPOINT",
                        "failed",
                        {
                            **checkpoint,
                            "checkpoint": "execute_error",
                            "finished_at": datetime.now().isoformat(timespec="seconds"),
                            "duration_ms": duration_ms,
                            "error": str(exc),
                        },
                    )
                trace.append(
                    {
                        "index": index,
                        "component_id": component_id,
                        "name": component["name"],
                        "status": "failed",
                        "started_at": started,
                        "duration_ms": duration_ms,
                        "error": str(exc),
                    }
                )
                raise DesktopExecutionError(
                    f"组件“{component['name']}”失败：{exc}"
                ) from exc
        return {
            "workflow_id": workflow_id,
            "execution_id": execution_id,
            "context": context,
            "trace": trace,
        }
