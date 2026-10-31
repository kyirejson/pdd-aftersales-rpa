from __future__ import annotations

import json
import html
import csv
import io
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
import time
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .components import ComponentValidationError, catalog, validate_component, validate_workflow
from .console_auth import ConsoleAuthConfig, ConsoleAuthManager
from .config import load_config
from .db import Repository
from .desktop_capture import DesktopCaptureService
from .element_capture import ElementCaptureService
from .orchestrator import DISPLAY_STATE_LABELS, Orchestrator
from .in_transit_routing import RoutingConfigConflict, RoutingConfigError
from .connectors.desktop_message import DesktopMessageConfigError
from .workflow_priority import WorkflowPriorityError
from .runtime_controller import RuntimeController, RuntimeControllerError
from .models import TaskState


class ToggleRuleRequest(BaseModel):
    enabled: bool


class PreviewMessageRequest(BaseModel):
    group_name: str


class InTransitFinishRequest(BaseModel):
    action: str
    live: bool = False


class RefundPilotRequest(BaseModel):
    confirm_order: str
    confirm_amount: float


class PauseRequest(BaseModel):
    paused: bool


class TaskStatusUpdateRequest(BaseModel):
    state: str
    reason: str = "售后流程控制台人工修改"


class PddReadOnlyRequest(BaseModel):
    shop_id: str | None = None


class DefinitionRequest(BaseModel):
    definition: dict


class RoutingDefinitionRequest(BaseModel):
    definition: dict
    expected_version: int | None = None


class RoutingPreviewRequest(BaseModel):
    inputs: dict


class MessageDispatchPreflightRequest(BaseModel):
    platform: str
    group_name: str


class RuntimeStartRequest(BaseModel):
    mode: str = "dry_run"
    workflow_keys: list[str] = Field(
        default_factory=lambda: [
            "shipped_no_trace",
            "in_transit_no_reason",
            "confirm_earlier_shipping",
        ]
    )
    continuous: bool | None = None
    queue_only: bool = False


class CaptureStartRequest(BaseModel):
    shop_id: str


class CaptureComponentRequest(BaseModel):
    name: str
    component_type: str = "ClickElement"
    capture: dict


class NamedCaptureRequest(BaseModel):
    shop_id: str
    text: str
    control_type: str = ""


class DesktopCaptureStartRequest(BaseModel):
    application: str = "旺店通ERP旗舰版"
    delay_seconds: int = 3


class DesktopCaptureComponentRequest(BaseModel):
    name: str
    component_type: str
    capture: dict
    component_id: str = ""
    config: dict = Field(default_factory=dict)


config = load_config()
CONSOLE_AUTH_COOKIE = "pdd_rpa_console_session"
auth_manager = ConsoleAuthManager(
    ConsoleAuthConfig.from_file(config.project_root / "runtime" / "console_access.json")
)
repository = Repository(config.database_path)
orchestrator = Orchestrator(config, repository)
capture_service = ElementCaptureService(config.raw["pdd_readonly"], config.shops)
desktop_capture_service = DesktopCaptureService(config.evidence_dir)
orchestrator.initialize()
runtime_controller = RuntimeController(orchestrator)

MANUAL_TASK_STATES = {
    TaskState.DISCOVERED,
    TaskState.NEED_REVIEW,
    TaskState.COMPLETED,
    TaskState.FAILED,
}
RUNTIME_BUSY_STATES = {"STARTING", "RUNNING", "PAUSED", "STOPPING"}

rules_path = config.project_root / "config" / "rules.json"
repository.seed_rules(json.loads(rules_path.read_text(encoding="utf-8")))
if not repository.list_workflows():
    repository.save_workflow(
        validate_workflow(
            {
                "workflow_id": "WF-PDD-DETAIL-READONLY",
                "name": "拼多多工单详情只读采集",
                "description": "唯一定位工单，点击立即处理后读取快递单号并关闭详情页",
                "enabled": False,
                "nodes": [
                    {"node_id": "activate", "component_type": "ActivateWindow", "name": "激活店铺窗口"},
                    {"node_id": "identity", "component_type": "AssertShopIdentity", "name": "校验店铺身份"},
                    {"node_id": "find", "component_type": "FindWorkOrderCard", "name": "按订单号查找工单卡片"},
                    {"node_id": "click", "component_type": "ClickElement", "name": "点击立即处理", "locator": {"text": "立即处理", "anchor_text": "{{order_no}}"}},
                    {"node_id": "read", "component_type": "ReadDetailFields", "name": "读取快递单号"},
                    {"node_id": "evidence", "component_type": "SaveEvidence", "name": "保存详情证据"},
                    {"node_id": "close", "component_type": "CloseDetailTab", "name": "关闭详情标签"},
                ],
                "edges": [
                    {"from": "activate", "to": "identity"}, {"from": "identity", "to": "find"},
                    {"from": "find", "to": "click"}, {"from": "click", "to": "read"},
                    {"from": "read", "to": "evidence"}, {"from": "evidence", "to": "close"},
                ],
            }
        )
    )

app = FastAPI(title="拼多多售后 RPA 控制台", version="0.1.0")
static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

_login_failures: dict[str, list[float]] = {}
_login_failure_lock = Lock()
_LOGIN_WINDOW_SECONDS = 10 * 60
_LOGIN_MAX_FAILURES = 5
_LOGIN_LOCK_SECONDS = 5 * 60


def _safe_next_path(value: str | None) -> str:
    candidate = str(value or "/").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_retry_after(client_key: str, now: float | None = None) -> int:
    current = time.time() if now is None else now
    with _login_failure_lock:
        recent = [
            stamp
            for stamp in _login_failures.get(client_key, [])
            if current - stamp < _LOGIN_WINDOW_SECONDS
        ]
        _login_failures[client_key] = recent
    if len(recent) < _LOGIN_MAX_FAILURES:
        return 0
    remaining = _LOGIN_LOCK_SECONDS - (current - recent[-1])
    if remaining <= 0:
        _clear_login_failures(client_key)
        return 0
    return max(1, int(remaining))


def _record_login_failure(client_key: str) -> None:
    now = time.time()
    with _login_failure_lock:
        recent = [
            stamp
            for stamp in _login_failures.get(client_key, [])
            if now - stamp < _LOGIN_WINDOW_SECONDS
        ]
        recent.append(now)
        _login_failures[client_key] = recent[-_LOGIN_MAX_FAILURES:]


def _clear_login_failures(client_key: str) -> None:
    with _login_failure_lock:
        _login_failures.pop(client_key, None)


def _login_page(next_path: str = "/", error: str = "") -> str:
    safe_next = html.escape(_safe_next_path(next_path), quote=True)
    safe_error = html.escape(error)
    error_html = f'<p class="error" role="alert">{safe_error}</p>' if safe_error else ""
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>登录 · 售后流程控制台</title>
    <style>
      :root {{ color-scheme: dark; --bg:#0d1110; --panel:#151b19; --line:#2b3732; --text:#eef5f0; --muted:#94a49c; --green:#57d18c; --red:#ff7b72; }}
      * {{ box-sizing:border-box; }}
      body {{ min-height:100vh; margin:0; display:grid; place-items:center; padding:24px; background:radial-gradient(circle at 20% 0%,#1b2c24 0,var(--bg) 40%); color:var(--text); font:14px/1.5 Inter,"Microsoft YaHei",sans-serif; }}
      main {{ width:min(420px,100%); padding:30px; border:1px solid var(--line); border-radius:18px; background:var(--panel); box-shadow:0 24px 80px #0008; }}
      .eyebrow {{ margin:0 0 6px; color:var(--green); font-size:11px; font-weight:800; letter-spacing:2px; }}
      h1 {{ margin:0; font-size:28px; }}
      .sub {{ margin:8px 0 24px; color:var(--muted); }}
      label {{ display:grid; gap:7px; margin:14px 0; color:var(--muted); font-weight:700; }}
      input {{ width:100%; border:1px solid var(--line); border-radius:10px; padding:12px; background:#0e1412; color:var(--text); font:inherit; }}
      input:focus {{ outline:2px solid #57d18c66; border-color:var(--green); }}
      button {{ width:100%; margin-top:10px; border:0; border-radius:10px; padding:12px; background:var(--green); color:#07120c; font-weight:900; cursor:pointer; }}
      .error {{ margin:0 0 12px; padding:10px 12px; border:1px solid #71332e; border-radius:9px; background:#46211f; color:var(--red); }}
      .notice {{ margin:20px 0 0; color:var(--muted); font-size:12px; }}
    </style>
  </head>
  <body>
    <main>
      <p class="eyebrow">PDD AFTER-SALES RPA</p>
      <h1>售后流程控制台</h1>
      <p class="sub">仅允许已授权的局域网设备访问</p>
      {error_html}
      <form method="post" action="/login">
        <input type="hidden" name="next" value="{safe_next}" />
        <label>用户名<input name="username" autocomplete="username" required autofocus /></label>
        <label>密码<input name="password" type="password" autocomplete="current-password" required /></label>
        <button type="submit">登录控制台</button>
      </form>
      <p class="notice">会话最长保持 12 小时。请勿在公共电脑上保存密码。</p>
    </main>
  </body>
</html>"""


def _is_authenticated(request: Request) -> bool:
    token = request.cookies.get(CONSOLE_AUTH_COOKIE, "")
    return bool(token and auth_manager.validate_session(token))


@app.middleware("http")
async def require_console_login(request: Request, call_next):
    path = request.url.path
    if path not in {"/login"} and not _is_authenticated(request):
        if path.startswith("/api/"):
            response = JSONResponse({"detail": "控制台登录已失效，请重新登录"}, status_code=401)
        else:
            next_path = path
            if request.url.query:
                next_path = f"{next_path}?{request.url.query}"
            response = RedirectResponse(f"/login?next={quote(next_path, safe='/')}", status_code=303)
    else:
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if path == "/login" or path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def console_login_page(request: Request, next: str = "/"):
    if _is_authenticated(request):
        return RedirectResponse(_safe_next_path(next), status_code=303)
    return HTMLResponse(_login_page(next))


@app.post("/login", include_in_schema=False)
async def console_login(request: Request):
    body = await request.body()
    if len(body) > 4096:
        return HTMLResponse(_login_page("/", "登录请求无效"), status_code=400)
    fields = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    username = str((fields.get("username") or [""])[0])
    password = str((fields.get("password") or [""])[0])
    next_path = _safe_next_path(str((fields.get("next") or ["/"])[0]))
    client_key = _client_key(request)
    retry_after = _login_retry_after(client_key)
    if retry_after:
        response = HTMLResponse(
            _login_page(next_path, f"尝试次数过多，请在约 {retry_after} 秒后重试"),
            status_code=429,
        )
        response.headers["Retry-After"] = str(retry_after)
        return response
    if not auth_manager.verify_credentials(username, password):
        _record_login_failure(client_key)
        return HTMLResponse(_login_page(next_path, "用户名或密码错误"), status_code=401)
    _clear_login_failures(client_key)
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(
        CONSOLE_AUTH_COOKIE,
        auth_manager.issue_session(),
        max_age=auth_manager.config.session_ttl_seconds,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return response


@app.post("/logout", include_in_schema=False)
def console_logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(CONSOLE_AUTH_COOKIE, path="/", httponly=True, samesite="strict")
    return response


@app.on_event("startup")
async def _schedule_runtime_recovery() -> None:
    # The supervisor restarts the API process after a native UIA crash.  The
    # controller then resumes from its durable shop checkpoint, never from a
    # blind full run.
    runtime_controller.schedule_auto_resume()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/designer", include_in_schema=False)
def designer() -> FileResponse:
    return FileResponse(static_dir / "designer.html")


@app.get("/routing", include_in_schema=False)
def routing() -> FileResponse:
    return FileResponse(static_dir / "routing.html")


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "execution_mode": config.execution_mode,
        "live_actions_enabled": config.live_actions_enabled,
        "connectors": orchestrator.connector_health(),
    }


@app.get("/api/runtime/status")
def runtime_status() -> dict:
    return runtime_controller.status()


@app.post("/api/runtime/start")
def runtime_start(request: RuntimeStartRequest) -> dict:
    try:
        return runtime_controller.start(
            request.mode,
            request.workflow_keys,
            request.continuous,
            queue_only=request.queue_only,
        )
    except RuntimeControllerError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/runtime/pause")
def runtime_pause() -> dict:
    try:
        return runtime_controller.pause()
    except RuntimeControllerError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/runtime/resume")
def runtime_resume() -> dict:
    try:
        return runtime_controller.resume()
    except RuntimeControllerError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/runtime/stop")
def runtime_stop() -> dict:
    try:
        return runtime_controller.stop()
    except RuntimeControllerError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/in-transit-routing/config")
def get_in_transit_routing_config() -> dict:
    return orchestrator.get_in_transit_config()


@app.put("/api/in-transit-routing/config")
def update_in_transit_routing_config(request: RoutingDefinitionRequest) -> dict:
    try:
        return {
            "ok": True,
            "config": orchestrator.update_in_transit_config(
                request.definition,
                expected_version=request.expected_version,
            ),
        }
    except RoutingConfigConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except RoutingConfigError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/in-transit-routing/preview")
def preview_in_transit_routing(request: RoutingPreviewRequest) -> dict:
    try:
        return orchestrator.preview_in_transit_routing(request.inputs)
    except RoutingConfigError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/message-dispatch/config")
def get_message_dispatch_config() -> dict:
    return orchestrator.get_message_dispatch_config()


@app.put("/api/message-dispatch/config")
def update_message_dispatch_config(request: DefinitionRequest) -> dict:
    try:
        return {"ok": True, "config": orchestrator.update_message_dispatch_config(request.definition)}
    except DesktopMessageConfigError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/message-dispatch/readiness")
def message_dispatch_readiness() -> dict:
    return orchestrator.in_transit_dispatch_readiness()


@app.post("/api/message-dispatch/preflight")
def message_dispatch_preflight(request: MessageDispatchPreflightRequest) -> dict:
    try:
        return {"ok": True, "result": orchestrator.preflight_message_group(request.platform, request.group_name)}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/dashboard")
def dashboard() -> dict:
    data = repository.dashboard()
    data["execution_mode"] = config.execution_mode
    data["live_actions_enabled"] = config.live_actions_enabled
    data["pdd_readonly"] = orchestrator.pdd_readonly.health().data
    data["qianniu_shop"] = orchestrator.qianniu_shop_summary()
    return data


@app.get("/api/tasks")
def tasks(state: str | None = None, view: str = "pending") -> list[dict]:
    try:
        return orchestrator.list_prioritized_tasks(state=state, view=view)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _history_bounds(date_from: str, date_to: str) -> tuple[str, str]:
    def parse(value: str, label: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(422, f"{label}必须是 YYYY-MM-DD") from exc

    start = parse(date_from, "开始日期")
    end = parse(date_to, "结束日期")
    if start and end and start > end:
        raise HTTPException(422, "开始日期不能晚于结束日期")
    return (
        start.isoformat(timespec="seconds") if start else "",
        (end + timedelta(days=1)).isoformat(timespec="seconds") if end else "",
    )


def _decorate_history_items(items: list[dict]) -> list[dict]:
    for task in items:
        facts = task.get("facts") or {}
        source = (facts.get("source_evidence") or {}).get("source_system")
        task["task_kind"] = (
            "qianniu" if source == "qianniu_aftersale"
            else "aftersale" if source == "aftersale_workbench"
            else "work_order"
        )
        task["display_state"] = DISPLAY_STATE_LABELS.get(
            task.get("state"), task.get("state")
        )
        task["processed_at"] = task.get("completed_at") or task.get("updated_at")
    return items


def _processed_history_query(
    *,
    date_from: str,
    date_to: str,
    shop_id: str,
    source: str,
    task_type: str,
    search: str,
    page: int,
    page_size: int,
) -> dict:
    if source not in {"", "all", "work_order", "aftersale", "qianniu"}:
        raise HTTPException(422, "已处理任务来源筛选无效")
    start_at, end_before = _history_bounds(date_from, date_to)
    result = repository.list_processed_task_history(
        start_at=start_at,
        end_before=end_before,
        shop_id=shop_id,
        source="" if source == "all" else source,
        task_type=task_type,
        search=search,
        page=page,
        page_size=page_size,
    )
    result["items"] = _decorate_history_items(result["items"])
    result["date_from"] = date_from
    result["date_to"] = date_to
    return result


@app.get("/api/tasks/processed-history")
def processed_task_history(
    date_from: str = "",
    date_to: str = "",
    shop_id: str = "",
    source: str = "all",
    task_type: str = "",
    search: str = "",
    page: int = 1,
    page_size: int = 100,
) -> dict:
    return _processed_history_query(
        date_from=date_from,
        date_to=date_to,
        shop_id=shop_id,
        source=source,
        task_type=task_type,
        search=search,
        page=page,
        page_size=page_size,
    )


@app.get("/api/tasks/processed-export")
def processed_task_export(
    date_from: str = "",
    date_to: str = "",
    shop_id: str = "",
    source: str = "all",
    task_type: str = "",
    search: str = "",
) -> Response:
    first = _processed_history_query(
        date_from=date_from,
        date_to=date_to,
        shop_id=shop_id,
        source=source,
        task_type=task_type,
        search=search,
        page=1,
        page_size=500,
    )
    items = list(first["items"])
    for page_no in range(2, int(first["pages"]) + 1):
        items.extend(
            _processed_history_query(
                date_from=date_from,
                date_to=date_to,
                shop_id=shop_id,
                source=source,
                task_type=task_type,
                search=search,
                page=page_no,
                page_size=500,
            )["items"]
        )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "任务ID", "任务来源", "店铺", "任务类型", "工单/售后号", "平台订单号",
        "退款金额", "退款类型", "快递公司", "物流单号", "旺店通订单状态",
        "任务状态", "处理完成时间",
    ])
    for task in items:
        facts = task.get("facts") or {}
        kind = task.get("task_kind")
        writer.writerow([
            task.get("id"),
            "千牛未收货退款" if kind == "qianniu" else "售后工作台" if kind == "aftersale" else "工单管理",
            facts.get("shop_name"),
            facts.get("work_order_title"),
            facts.get("work_order_no"),
            facts.get("order_no"),
            facts.get("refund_amount"),
            facts.get("refund_type"),
            facts.get("carrier"),
            facts.get("tracking_no"),
            facts.get("wdt_order_status"),
            task.get("display_state"),
            task.get("processed_at"),
        ])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = quote(f"已处理任务_{stamp}.csv")
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@app.get("/api/workflow-priorities")
def workflow_priorities() -> dict:
    return orchestrator.get_workflow_priority_config()


@app.put("/api/workflow-priorities")
def update_workflow_priorities(request: DefinitionRequest) -> dict:
    try:
        return {"ok": True, "config": orchestrator.update_workflow_priority_config(request.definition)}
    except WorkflowPriorityError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: int) -> dict:
    task = repository.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    source_evidence = (task.get("facts") or {}).get("source_evidence") or {}
    source_system = str(source_evidence.get("source_system") or "")
    if source_system == "qianniu_aftersale":
        task["task_kind"] = "qianniu"
        task["task_source_label"] = "千牛未收货退款"
    elif source_system == "aftersale_workbench":
        task["task_kind"] = "aftersale"
        task["task_source_label"] = "售后工作台"
    else:
        task["task_kind"] = "work_order"
        task["task_source_label"] = "工单管理"
    task["queue_scope"] = str(source_evidence.get("queue_scope") or "")
    task["display_state"] = DISPLAY_STATE_LABELS.get(task["state"], task["state"])
    task["logs"] = repository.list_logs(task_id=task_id)
    return task


@app.get("/api/tasks/{task_id}/evidence/{evidence_id}")
def task_evidence_file(task_id: int, evidence_id: int) -> FileResponse:
    evidence = next(
        (
            item
            for item in repository.list_task_evidence(task_id)
            if int(item["id"]) == int(evidence_id)
        ),
        None,
    )
    if not evidence:
        raise HTTPException(404, "任务证据不存在")
    root = Path(config.evidence_dir).resolve()
    path = Path(str(evidence["file_path"])).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(403, "证据文件超出审计目录")
    if not path.is_file():
        raise HTTPException(404, "证据文件已丢失")
    return FileResponse(path)


@app.patch("/api/tasks/{task_id}/status")
def update_task_status(task_id: int, request: TaskStatusUpdateRequest) -> dict:
    try:
        target = TaskState(request.state)
    except ValueError as exc:
        raise HTTPException(422, f"不支持的任务状态：{request.state}") from exc
    if target not in MANUAL_TASK_STATES:
        raise HTTPException(422, "人工状态只能设置为：待处理、失败、已处理、转人工")
    runtime_snapshot = runtime_controller.status()
    runtime_state = str(runtime_snapshot.get("state") or "")
    current_task = runtime_snapshot.get("current_task") or {}
    try:
        current_task_id = int(current_task.get("task_id"))
    except (TypeError, ValueError):
        current_task_id = None
    if runtime_state in RUNTIME_BUSY_STATES and current_task_id == task_id:
        raise HTTPException(
            409,
            "当前工单正在自动处理中，请等待该单完成或先停止运行后再修改",
        )
    try:
        task = repository.set_task_status_manual(task_id, target, request.reason.strip() or "售后流程控制台人工修改")
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    task["display_state"] = DISPLAY_STATE_LABELS.get(task["state"], task["state"])
    return {"ok": True, "task": task}


@app.get("/api/interception-registers")
def interception_registers(record_date: str | None = None, limit: int = 500) -> list[dict]:
    """登记已完成同意退款的快递拦截记录。"""
    return repository.list_interception_registers(record_date=record_date, limit=limit)


@app.post("/api/interception-registers/dingtalk/sync")
def sync_pending_interception_registers_to_dingtalk(limit: int = 100) -> dict:
    """Retry local register rows that have not reached the shared DingTalk sheet."""
    return orchestrator.sync_pending_dingtalk_interception_registers(limit=limit)


@app.post("/api/interception-registers/{register_id}/dingtalk/sync")
def append_interception_register_to_dingtalk(register_id: int) -> dict:
    """Append one retained register row only; never rerun PDD actions."""
    try:
        return orchestrator.sync_dingtalk_interception_register(int(register_id))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/pdd/readonly-discover")
def pdd_readonly_discover(request: PddReadOnlyRequest) -> dict:
    runtime_state = str(runtime_controller.status().get("state") or "")
    if runtime_state in {"STARTING", "RUNNING", "PAUSED", "STOPPING"}:
        raise HTTPException(409, "运行队列正在使用桌面，不能并发启动店铺同步")
    try:
        return orchestrator.discover_live_readonly(request.shop_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/pdd/open-shop-windows")
def open_shop_windows() -> dict:
    try:
        return orchestrator.open_shop_windows()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/tasks/{task_id}/evaluate")
def evaluate(task_id: int) -> dict:
    try:
        return orchestrator.enrich_and_evaluate(task_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/tasks/{task_id}/collect-detail-readonly")
def collect_detail_readonly(task_id: int) -> dict:
    try:
        return orchestrator.collect_task_detail_readonly(task_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/tasks/{task_id}/collect-aftersale-detail-readonly")
def collect_aftersale_detail_readonly(task_id: int) -> dict:
    task = repository.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    source_evidence = (task.get("facts") or {}).get("source_evidence") or {}
    source_system = str(source_evidence.get("source_system") or "").strip()
    if source_system != "aftersale_workbench":
        raise HTTPException(409, "当前任务不是售后工作台任务")
    queue_scope = str(source_evidence.get("queue_scope") or "").strip()
    if queue_scope and queue_scope != "pending_soon_overdue":
        raise HTTPException(409, "仅支持售后工作台“待处理即将逾期”任务的详情读取")
    try:
        # The read-only endpoint must never retain an actionable detail tab for
        # a following write worker.  The isolated collector verifies the exact
        # order, saves its evidence, and restores the verified list anchor.
        return orchestrator.collect_aftersale_detail(
            task_id,
            keep_detail_open=False,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/tasks/{task_id}/check-wdt-components")
def check_wdt_components(task_id: int) -> dict:
    try:
        return orchestrator.check_wdt_by_components(task_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/tasks/{task_id}/preview-refund-components")
def preview_refund_components(task_id: int) -> dict:
    try:
        # The production flow keeps the exact detail tab open through WDT;
        # force the isolated post-WDT UIA worker for the manual button too.
        return orchestrator.preview_refund_components(task_id, retain_detail=True)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/tasks/{task_id}/execute-refund-pilot")
def execute_refund_pilot(task_id: int, request: RefundPilotRequest) -> dict:
    try:
        return orchestrator.execute_refund_pilot(
            task_id, request.confirm_order, request.confirm_amount
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/tasks/{task_id}/reconcile-refund-result")
def reconcile_refund_result(task_id: int) -> dict:
    try:
        return orchestrator.reconcile_refund_result(task_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/tasks/{task_id}/preview-intercept")
def preview_intercept(task_id: int, request: PreviewMessageRequest) -> dict:
    try:
        return orchestrator.preview_intercept(task_id, request.group_name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/tasks/{task_id}/prepare-in-transit-reason")
def prepare_in_transit_reason(task_id: int, live: bool = False) -> dict:
    try:
        return orchestrator.prepare_in_transit_reason(task_id, live=live)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/tasks/{task_id}/finish-in-transit")
def finish_in_transit(task_id: int, request: InTransitFinishRequest) -> dict:
    try:
        return orchestrator.finish_in_transit_refund(task_id, request.action, live=request.live)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/rules")
def rules() -> list[dict]:
    return repository.list_rules()


@app.post("/api/rules/{rule_id}/enabled")
def toggle_rule(rule_id: str, request: ToggleRuleRequest) -> dict:
    try:
        repository.set_rule_enabled(rule_id, request.enabled)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "rule_id": rule_id, "enabled": request.enabled}


@app.post("/api/system/pause")
def pause(request: PauseRequest) -> dict:
    repository.set_paused(request.paused)
    repository.log_action(
        None,
        f"SYSTEM_PAUSE:{request.paused}:{repository.dashboard()['task_count']}",
        "SYSTEM_PAUSE" if request.paused else "SYSTEM_RESUME",
        "success",
        {"paused": request.paused},
    )
    return {"ok": True, "paused": request.paused}


@app.get("/api/logs")
def logs() -> list[dict]:
    return repository.list_logs()


@app.get("/api/components/catalog")
def component_catalog() -> list[dict]:
    return catalog()


@app.get("/api/components")
def components() -> list[dict]:
    return repository.list_components()


@app.post("/api/components")
def save_component(request: DefinitionRequest) -> dict:
    try:
        definition = validate_component(request.definition)
        repository.save_component(definition)
        return {"ok": True, "component": definition}
    except ComponentValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/workflows")
def workflows() -> list[dict]:
    return repository.list_workflows()


@app.post("/api/workflows")
def save_workflow(request: DefinitionRequest) -> dict:
    try:
        definition = validate_workflow(request.definition)
        repository.save_workflow(definition)
        return {"ok": True, "workflow": definition}
    except ComponentValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/capture/start")
def start_capture(request: CaptureStartRequest) -> dict:
    try:
        return capture_service.start(request.shop_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/capture/{shop_id}")
def capture_status(shop_id: str) -> dict:
    try:
        return capture_service.status(shop_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/capture/{shop_id}/cancel")
def cancel_capture(shop_id: str) -> dict:
    try:
        return capture_service.cancel(shop_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/capture/component")
def component_from_capture(request: CaptureComponentRequest) -> dict:
    try:
        definition = capture_service.component_from_capture(
            request.capture, request.name, request.component_type
        )
        repository.save_component(definition)
        return {"ok": True, "component": definition}
    except ComponentValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/capture/named")
def capture_named_element(request: NamedCaptureRequest) -> dict:
    """Capture an exact visible UIA element (including the PDD login page)."""
    try:
        capture = capture_service.capture_named(
            request.shop_id, request.text, request.control_type
        )
        return {"ok": True, "capture": capture}
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/desktop-capture/start")
def start_desktop_capture(request: DesktopCaptureStartRequest) -> dict:
    try:
        return desktop_capture_service.start(request.application, request.delay_seconds)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/desktop-capture/status")
def desktop_capture_status() -> dict:
    return desktop_capture_service.status()


@app.post("/api/desktop-capture/cancel")
def cancel_desktop_capture() -> dict:
    return desktop_capture_service.cancel()


@app.post("/api/desktop-capture/component")
def desktop_component_from_capture(request: DesktopCaptureComponentRequest) -> dict:
    try:
        definition = desktop_capture_service.component_from_capture(
            request.capture, request.name, request.component_type
        )
        if request.component_id.strip():
            definition["component_id"] = request.component_id.strip()
        if request.config:
            definition["config"] = dict(request.config)
        definition = validate_component(definition)
        repository.save_component(definition)
        return {"ok": True, "component": definition}
    except ComponentValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
