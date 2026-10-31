const $ = (selector) => document.querySelector(selector);
let dashboard = null;
let priorityConfig = null;
let runtimeStatus = null;
let showProcessed = false;
let selectedShopId = "";
let selectedWorkOrderType = "";
let selectedTaskSource = "all";
let selectedTaskStatus = "";
let selectedTaskUrgency = "";
let taskSearch = "";
let selectedTaskId = null;
let processedPage = 1;
let processedPages = 1;
let processedTotal = 0;
const processedPageSize = 100;
let processedDateFrom = "";
let processedDateTo = "";
let loadedTasks = [];
let taskIndex = new Map();
let taskSearchTimer = null;
let lastRegisterRefreshKey = "";
const manualTaskStates = [
  ["DISCOVERED", "待处理"],
  ["FAILED", "失败"],
  ["COMPLETED", "已处理"],
  ["NEED_REVIEW", "转人工"],
];

// The database keeps detailed execution states for recovery/audit.  The
// operator can only choose the four business states requested by the user.
const taskStateAliases = {
  DISCOVERED: "DISCOVERED",
  COLLECTING_DATA: "DISCOVERED",
  WAIT_RULE_MATCH: "DISCOVERED",
  READY: "DISCOVERED",
  RETRY_PENDING: "DISCOVERED",
  EXECUTING: "DISCOVERED",
  VERIFYING: "DISCOVERED",
  NEED_REVIEW: "NEED_REVIEW",
  WAIT_APPROVAL: "NEED_REVIEW",
  PAUSED: "NEED_REVIEW",
  DATA_CONFLICT: "NEED_REVIEW",
  COMPLETED: "COMPLETED",
  FAILED: "FAILED",
  REJECTED: "FAILED",
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 401) {
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
    throw new Error("控制台登录已失效");
  }
  if (!response.ok) throw new Error((await response.json()).detail || `请求失败 ${response.status}`);
  return response.json();
}

function notice(message, error = false) {
  const node = $("#notice");
  node.textContent = message;
  node.className = error ? "error" : "ok";
  node.setAttribute("role", error ? "alert" : "status");
  node.setAttribute("aria-live", error ? "assertive" : "polite");
  setTimeout(() => (node.textContent = ""), 4500);
}

function metric(label, value, hint) {
  return `<article class="metric"><span>${label}</span><strong>${value}</strong><small>${hint}</small></article>`;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

function updateTaskViewButton() {
  const button = $("#toggle-processed");
  if (!button) return;
  const processed = (dashboard?.states?.COMPLETED || 0) + (dashboard?.states?.REJECTED || 0);
  button.textContent = showProcessed ? "返回待处理任务" : `查看已处理任务（${processed}）`;
  const exportButton = $("#export-processed");
  if (exportButton) {
    exportButton.hidden = !showProcessed;
    exportButton.disabled = !showProcessed || !processedTotal;
    exportButton.textContent = processedTotal
      ? `导出筛选结果（${processedTotal}）`
      : "导出当前结果";
  }
}

function processedQuery(includePage = true) {
  const params = new URLSearchParams();
  if (processedDateFrom) params.set("date_from", processedDateFrom);
  if (processedDateTo) params.set("date_to", processedDateTo);
  if (selectedShopId) params.set("shop_id", selectedShopId);
  if (selectedTaskSource && selectedTaskSource !== "all") params.set("source", selectedTaskSource);
  if (selectedWorkOrderType) params.set("task_type", selectedWorkOrderType);
  if (taskSearch.trim()) params.set("search", taskSearch.trim());
  if (includePage) {
    params.set("page", String(processedPage));
    params.set("page_size", String(processedPageSize));
  }
  return params.toString();
}

function exportProcessedTasks() {
  if (!showProcessed || !processedTotal) {
    notice("当前筛选条件下没有可导出的已处理任务", true);
    return;
  }
  window.location.assign(`/api/tasks/processed-export?${processedQuery(false)}`);
  notice(`正在导出当前筛选条件下的 ${processedTotal} 条已处理任务`);
}

function taskStateSelect(task) {
  const current = taskStateAliases[String(task.state || "DISCOVERED")] || "NEED_REVIEW";
  const options = [...manualTaskStates];
  return `<select class="task-state-select ${current.toLowerCase()}" aria-label="人工修改任务状态"
    onchange="changeTaskStatus(${Number(task.id)}, this, '${esc(current)}')">
    ${options.map(([value, label]) => `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(label)}</option>`).join("")}
  </select>`;
}

function renderShopFilter() {
  const shops = dashboard?.shops || [];
  const qianniu = dashboard?.qianniu_shop;
  if (selectedShopId && selectedShopId !== qianniu?.id && !shops.some((shop) => shop.id === selectedShopId)) selectedShopId = "";
  const select = $("#task-shop-filter");
  const options = shops.map((shop) =>
    `<option value="${esc(shop.id)}">${esc(shop.name)}</option>`
  );
  if (qianniu && qianniu.enabled) {
    options.push(`<option value="${esc(qianniu.id)}">${esc(qianniu.name)}（天猫）</option>`);
  }
  select.innerHTML = `<option value="">全部店铺</option>${options.join("")}`;
  select.value = selectedShopId;
}

function isAftersaleTask(task) {
  const facts = task?.facts || {};
  return task?.task_kind === "aftersale"
    || facts.source_evidence?.source_system === "aftersale_workbench"
    || String(facts.work_order_title || "").startsWith("售后工作台：");
}

function isQianniuTask(task) {
  const facts = task?.facts || {};
  return task?.task_kind === "qianniu"
    || facts.source_evidence?.source_system === "qianniu_aftersale"
    || facts.source_evidence?.platform === "tmall"
    || String(facts.work_order_title || "").startsWith("千牛未收货退款");
}

function taskSource(task) {
  if (isQianniuTask(task)) return "qianniu";
  return isAftersaleTask(task) ? "aftersale" : "work_order";
}

function taskSourceLabel(task) {
  const source = taskSource(task);
  if (source === "qianniu") return "千牛未收货退款";
  return source === "aftersale" ? "售后待处理" : "工单管理";
}

function taskTitle(task) {
  return String(task?.facts?.work_order_title || "待处理任务").trim();
}

function taskSourceBadge(task, compact = false) {
  const source = taskSource(task);
  const label = source === "aftersale" ? "售后单" : source === "qianniu" ? "千牛" : "工单";
  return `<span class="task-source-badge ${source}${compact ? " compact" : ""}">${label}</span>`;
}

function aftersaleRemainingText(facts) {
  const source = facts?.source_evidence || {};
  return String(
    facts?.aftersale_remaining_text
    || source.aftersale_card?.urgency_text
    || source.aftersale_card?.remaining_text
    || source.aftersale_card?.refund_reason
    || ""
  ).trim();
}

function taskSourceStale(task) {
  return isAftersaleTask(task) && Boolean(task?.source_stale);
}

function taskSourceSyncError(task) {
  return String(task?.source_sync_error || task?.aftersale_sync_error || "").trim();
}

function sourceStaleAlert(task) {
  if (!taskSourceStale(task)) return "";
  const error = taskSourceSyncError(task);
  return `<div class="source-stale-alert"><b>上轮售后数据</b><span>本轮扫描失败，禁止自动处理</span>${error ? `<small>${esc(shortText(error, 110))}</small>` : ""}</div>`;
}

function parseRemainingSeconds(value) {
  const text = String(value || "").replaceAll(" ", "").trim();
  if (!text) return null;
  if (text.includes("已逾期") || text.startsWith("-")) return 0;
  const match = text.match(/^(?:(\d+)天)?(?:(\d+)时)?(?:(\d+)分)?(?:(\d+)秒)?$/);
  if (!match || !match.slice(1).some((item) => item !== undefined)) return null;
  return (Number(match[1] || 0) * 86400)
    + (Number(match[2] || 0) * 3600)
    + (Number(match[3] || 0) * 60)
    + Number(match[4] || 0);
}

function aftersaleUrgency(task) {
  if (!isAftersaleTask(task)) return "normal";
  const facts = task.facts || {};
  const explicit = Number(facts.aftersale_remaining_seconds);
  const seconds = Number.isFinite(explicit) && explicit >= 0
    ? explicit
    : parseRemainingSeconds(aftersaleRemainingText(facts));
  if (seconds === null) return "normal";
  if (seconds <= 0) return "overdue";
  if (seconds <= 2 * 3600) return "critical";
  if (seconds <= 6 * 3600) return "warning";
  if (seconds <= 24 * 3600) return "soon";
  return "normal";
}

function urgencyLabel(level) {
  return ({
    overdue: "已逾期",
    critical: "即将逾期",
    warning: "临近逾期",
    soon: "24 小时内",
    normal: "剩余时间",
  })[level] || "剩余时间";
}

function aftersaleUrgencyBadge(task) {
  const facts = task.facts || {};
  const remaining = aftersaleRemainingText(facts) || "未读取";
  const level = aftersaleUrgency(task);
  return `<span class="task-urgency ${level}"><b>${esc(urgencyLabel(level))}</b><span>${esc(remaining)}</span></span>`;
}

function aftersaleActualReason(facts) {
  const remaining = aftersaleRemainingText(facts);
  const reason = String(facts?.refund_reason || "").trim();
  const looksLikeUrgency = parseRemainingSeconds(reason) !== null
    || /(?:即将逾期|待处理即将逾期|已逾期)/.test(reason);
  return reason && reason !== remaining && !looksLikeUrgency ? reason : "详情尚未读取";
}

function taskMatchesUrgency(task) {
  if (!selectedTaskUrgency) return true;
  if (!isAftersaleTask(task)) return false;
  const level = aftersaleUrgency(task);
  if (selectedTaskUrgency === "overdue") return level === "overdue";
  if (selectedTaskUrgency === "critical") return level === "critical";
  if (selectedTaskUrgency === "warning") return ["critical", "warning"].includes(level);
  if (selectedTaskUrgency === "soon") return ["critical", "warning", "soon"].includes(level);
  return true;
}

function actionLabel(value) {
  return ({
    EXTEND_RECEIPT: "延长收货",
    NEED_REVIEW: "转人工",
    AGREE_REFUND: "同意退款",
    AGREE_RETURN_AFTER_REFUND: "同意快递退回后退款",
    AGREE_RECALL_AFTER_REFUND: "同意召回后退款",
    REJECT_REFUND: "驳回退款",
  })[String(value || "")] || String(value || "");
}

function aftersaleOutcome(facts) {
  const source = facts?.source_evidence || {};
  const retry = source.aftersale_retry || {};
  if (retry.retry_not_before) return `平台冷却中 · ${formatTimestamp(retry.retry_not_before)} 后重试`;
  const decision = facts?.aftersale_decision || source.aftersale_detail?.decision || {};
  const action = facts?.aftersale_final_action || decision.action || "";
  if (action === "EXTEND_RECEIPT" && facts?.aftersale_business_write_performed) return "已延长收货";
  if (action === "AGREE_REFUND" && facts?.aftersale_business_write_performed) return "已同意退款";
  if (action === "AGREE_RETURN_AFTER_REFUND" && facts?.aftersale_business_write_performed) return "已同意快递退回后退款";
  if (action === "AGREE_RECALL_AFTER_REFUND" && facts?.aftersale_business_write_performed) return "已同意召回后退款";
  if (action === "REJECT_REFUND" && facts?.aftersale_business_write_performed) return "已驳回退款";
  if (action === "EXTEND_RECEIPT" || facts?.has_extend_receipt) return "可延长收货";
  if (action === "AGREE_REFUND") return "待同意退款";
  if (action === "AGREE_RETURN_AFTER_REFUND") return "待同意快递退回后退款";
  if (action === "AGREE_RECALL_AFTER_REFUND") return "待同意召回后退款";
  if (action === "REJECT_REFUND") return "待驳回退款";
  if (action === "NEED_REVIEW") return "需人工处理";
  return source.aftersale_detail ? "详情已核验" : "等待读取详情";
}

function formatTimestamp(value) {
  const text = String(value || "").trim();
  if (!text) return "-";
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return text.replace("T", " ");
  return date.toLocaleString("zh-CN", { hour12: false });
}

function shortText(value, limit = 150) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

function renderWorkOrderTypeFilter(tasks) {
  const wrapper = $("#task-type-filter-wrap");
  wrapper.hidden = false;
  const sourceTasks = tasks.filter((task) =>
    selectedTaskSource === "all" || taskSource(task) === selectedTaskSource
  );
  const types = [...new Set(sourceTasks.map((task) =>
    String(task.facts?.work_order_title || "待处理任务").trim()
  ).filter(Boolean))].sort((left, right) => left.localeCompare(right, "zh-CN"));
  if (selectedWorkOrderType && !types.includes(selectedWorkOrderType)) selectedWorkOrderType = "";
  const select = $("#task-type-filter");
  select.innerHTML = `<option value="">全部任务类型</option>${types.map((type) =>
    `<option value="${esc(type)}">${esc(type)}</option>`
  ).join("")}`;
  select.value = selectedWorkOrderType;
}

function taskEvidenceLinks(task) {
  const latestByType = new Map();
  for (const item of (task.evidence || [])) {
    const type = String(item.evidence_type || "other");
    if (!latestByType.has(type)) latestByType.set(type, item);
  }
  const labels = {
    interception_group: "拦截群",
    pdd_aftersale_detail: "售后详情",
    pdd_work_order_detail: isAftersaleTask(task) ? "售后详情" : "工单详情",
    qianniu_aftersale_detail: "天猫售后详情",
    qianniu_logistics_trajectory: "天猫物流轨迹",
    qianniu_intercept_terminal: "天猫拦截结果",
  };
  return [...latestByType.entries()].map(([type, item]) =>
    `<a class="link" target="_blank" href="/api/tasks/${Number(task.id)}/evidence/${Number(item.id)}">${esc(labels[type] || "处理证据")}</a>`
  ).join(" ") || "-";
}

function renderSourceTabs(tasks) {
  const counts = { all: tasks.length, work_order: 0, aftersale: 0, qianniu: 0 };
  for (const task of tasks) counts[taskSource(task)] += 1;
  $("#task-source-all-count").textContent = String(counts.all);
  $("#task-source-work-order-count").textContent = String(counts.work_order);
  $("#task-source-aftersale-count").textContent = String(counts.aftersale);
  $("#task-source-qianniu-count").textContent = String(counts.qianniu);
  document.querySelectorAll(".task-source-tab").forEach((button) => {
    const active = button.dataset.source === selectedTaskSource;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  const urgencyWrap = $("#task-urgency-filter-wrap");
  urgencyWrap.hidden = selectedTaskSource !== "aftersale";
}

function renderTasksHeader() {
  const headers = selectedTaskSource === "work_order"
    ? ["来源 / 店铺", "工单 / 订单", "退款 / 原因", "物流信息", "旺店通", "同步状态", "任务状态", "详情"]
    : selectedTaskSource === "aftersale"
      ? ["来源 / 店铺", "售后订单", "剩余处理时间", "退款信息", "发货 / 快递", "当前处置", "任务状态", "详情"]
      : selectedTaskSource === "qianniu"
        ? ["来源 / 店铺", "售后 / 订单", "退款 / 状态", "物流 / 轨迹", "旺店通 / 决策", "同步状态", "任务状态", "详情"]
        : ["来源 / 店铺", "任务 / 订单", "时限 / 金额", "物流 / 售后", "业务进度", "同步状态", "任务状态", "详情"];
  $("#tasks-head").innerHTML = `<tr>${headers.map((label) => `<th>${esc(label)}</th>`).join("")}</tr>`;
}

function taskMatchesSearch(task) {
  const query = taskSearch.trim().toLocaleLowerCase("zh-CN");
  if (!query) return true;
  const facts = task.facts || {};
  const haystack = [
    facts.shop_name, facts.edge_profile, facts.work_order_title, facts.work_order_no,
    facts.order_no, facts.carrier, facts.tracking_no, facts.refund_reason,
    facts.wdt_order_id, facts.wdt_warehouse, ...(facts.wdt_product_names || []),
  ].join(" ").toLocaleLowerCase("zh-CN");
  return haystack.includes(query);
}

function taskSyncHtml(task) {
  if (taskSourceStale(task)) {
    return '<span class="status stale">上轮售后数据</span><small>本轮扫描失败 · 禁止自动处理</small>';
  }
  if (showProcessed) {
    return `<span class="status completed">已完成</span><small>${esc(formatTimestamp(task.completed_at || task.updated_at))}</small>`;
  }
  const currentClass = task.current_sync ? "enabled" : "disabled";
  const currentText = task.current_sync ? "本轮已同步" : "上轮保留";
  return `<span class="status ${currentClass}">${currentText}</span><small>${esc(formatTimestamp(task.sync_finished_at))}</small>`;
}

function workOrderBusinessState(facts) {
  const source = facts.source_evidence || {};
  if (facts.refund_agreed || facts.real_refund_completed) return "退款已处理";
  if (source.interception_message?.sent_echo) return "拦截消息已发送";
  if (facts.wdt_result_count > 0 || facts.wdt_order_status) {
    return `旺店通${facts.wdt_order_status ? `：${facts.wdt_order_status}` : "已核验"}`;
  }
  return "等待业务核验";
}

function renderWorkOrderRow(task) {
  const facts = task.facts || {};
  const selected = Number(task.id) === Number(selectedTaskId) ? "task-selected" : "";
  const wdt = facts.wdt_order_status
    ? `${facts.wdt_order_status}${facts.wdt_eligible ? " · 可退款" : " · 不可退款"}`
    : (facts.wdt_result_count > 0 ? `已匹配 ${facts.wdt_result_count} 单` : "尚未查询");
  const deadline = facts.work_order_deadline ? `截止 ${facts.work_order_deadline}` : "未读取截止时间";
  return `<tr class="task-row work-order ${selected}" data-task-id="${Number(task.id)}">
    <td>${taskSourceBadge(task)}<b>${esc(facts.shop_name || "未知店铺")}</b><small>${esc(facts.edge_profile || "-")} · 工单管理</small></td>
    <td><b>${esc(taskTitle(task))}</b><small>订单 ${esc(facts.order_no || "-")} · 工单 ${esc(facts.work_order_no || "-")}</small></td>
    <td><strong class="money">¥${Number(facts.refund_amount || 0).toFixed(2)}</strong><small>${esc(facts.refund_reason || "退款详情待读取")} · ${esc(deadline)}</small></td>
    <td>${esc(facts.carrier || "快递待读取")}<small>${esc(facts.tracking_no || "物流单号待读取")}</small></td>
    <td>${esc(wdt)}<small>${esc(facts.wdt_warehouse || facts.wdt_platform || workOrderBusinessState(facts))}</small></td>
    <td>${taskSyncHtml(task)}</td>
    <td>${taskStateSelect(task)}<small>优先级 ${esc(task.queue_priority)} · 队列 #${esc(task.queue_rank)}</small></td>
    <td><button class="task-detail-button" type="button" onclick="showTask(${Number(task.id)})">查看详情</button></td>
  </tr>`;
}

function renderAftersaleRow(task) {
  const facts = task.facts || {};
  const source = facts.source_evidence || {};
  const decision = facts.aftersale_decision || source.aftersale_detail?.decision || {};
  const selected = Number(task.id) === Number(selectedTaskId) ? "task-selected" : "";
  const shipping = facts.shipping_status || facts.pdd_order_status || "发货状态待读取";
  const aftersaleStatus = facts.aftersale_status || source.aftersale_card?.status || "待处理即将逾期";
  const outcome = taskSourceStale(task) ? "禁止自动处理" : aftersaleOutcome(facts);
  const outcomeClass = outcome.includes("人工") ? "manual" : outcome.startsWith("已") ? "done" : "pending";
  const outcomeHtml = taskSourceStale(task)
    ? '<span class="status stale">上轮售后数据</span><small>本轮扫描失败，禁止自动处理</small>'
    : `<span class="task-outcome ${outcomeClass}">${esc(outcome)}</span><small>${esc(decision.reason || "自动处理前会再次核验详情")}</small>`;
  return `<tr class="task-row aftersale ${taskSourceStale(task) ? "source-stale" : ""} ${selected}" data-task-id="${Number(task.id)}">
    <td>${taskSourceBadge(task)}<b>${esc(facts.shop_name || "未知店铺")}</b><small>${esc(facts.edge_profile || "-")} · 待处理即将逾期</small></td>
    <td><b>${esc(facts.order_no || "-")}</b><small>${esc(facts.refund_type || "退货退款")} · 售后记录</small></td>
    <td>${aftersaleUrgencyBadge(task)}</td>
    <td><strong class="money">¥${Number(facts.refund_amount || 0).toFixed(2)}</strong><small>${esc(aftersaleActualReason(facts))}</small></td>
    <td>${esc(shipping)}<small>${esc(aftersaleStatus)} · ${esc(facts.return_carrier || facts.carrier || "退货快递待读取")}</small></td>
    <td>${outcomeHtml}</td>
    <td>${taskStateSelect(task)}<small>优先级 ${esc(task.queue_priority)} · 队列 #${esc(task.queue_rank)}</small></td>
    <td><button class="task-detail-button aftersale" type="button" onclick="showTask(${Number(task.id)})">查看售后</button></td>
  </tr>`;
}

function qianniuDecisionOf(task) {
  const source = (task?.facts?.source_evidence) || {};
  return source.qianniu_decision || {};
}

function qianniuActionLabel(value) {
  return ({
    agree_refund: "同意退款",
    intercept_express: "拦截快递",
    manual_review: "人工复核",
  })[String(value || "")] || "";
}

function qianniuOutcome(task) {
  const state = String(task?.state || "");
  const source = task?.facts?.source_evidence || {};
  const feedback = source.qianniu_platform_feedback || {};
  const decision = qianniuDecisionOf(task);
  const action = String(decision.action || "");
  if (feedback.verified && feedback.terminal === "EXPRESS_INTERCEPTION_SUBMITTED") {
    return { text: "拦截快递已提交", kind: "done" };
  }
  if (state === "COMPLETED" && action === "agree_refund") {
    return { text: "同意退款已完成", kind: "done" };
  }
  if (state === "COMPLETED" && action === "intercept_express") {
    return { text: "拦截快递已完成", kind: "done" };
  }
  if (state === "COMPLETED") return { text: "天猫售后已完成", kind: "done" };
  if (state === "NEED_REVIEW") return { text: "需人工复核", kind: "manual" };
  if (action === "agree_refund") return { text: "待同意退款", kind: "pending" };
  if (action === "intercept_express") return { text: "待拦截快递", kind: "pending" };
  if (action === "manual_review") return { text: "需人工处理", kind: "manual" };
  if ((task?.facts?.wdt_result_count || 0) > 0) return { text: "已核验待决策", kind: "pending" };
  return { text: "等待旺店通核验", kind: "pending" };
}

function renderQianniuRow(task) {
  const facts = task.facts || {};
  const source = facts.source_evidence || {};
  const decision = qianniuDecisionOf(task);
  const selected = Number(task.id) === Number(selectedTaskId) ? "task-selected" : "";
  const shipping = facts.pdd_order_status || "发货状态待读取";
  const aftersaleStatus = source.aftersale_status || "退款待处理";
  const hasTrajectory = facts.logistics_trajectory_count !== undefined
    && facts.logistics_trajectory_count !== null
    && facts.logistics_trajectory_count !== "";
  const trajectory = hasTrajectory
    ? `${Number(facts.logistics_trajectory_count)} 条轨迹`
    : "轨迹未读取";
  const carrier = facts.carrier || "快递待读取";
  const wdt = facts.wdt_order_status
    ? `${facts.wdt_order_status}${facts.wdt_warehouse ? ` · ${facts.wdt_warehouse}` : ""}`
    : (facts.wdt_result_count > 0 ? "已查询" : "尚未查询");
  const outcome = qianniuOutcome(task);
  const actions = (source.available_actions || []).join(" / ");
  return `<tr class="task-row qianniu ${selected}" data-task-id="${Number(task.id)}">
    <td>${taskSourceBadge(task)}<b>${esc(facts.shop_name || "示例天猫旗舰店")}</b><small>天猫 · 未收货退款</small></td>
    <td><b>售后 ${esc(facts.work_order_no || "-")}</b><small>订单 ${esc(facts.order_no || "-")}</small></td>
    <td><strong class="money">¥${Number(facts.refund_amount || 0).toFixed(2)}</strong><small>${esc(facts.refund_type || "未收货退款")} · ${esc(aftersaleStatus)}${actions ? ` · 可操作：${esc(actions)}` : ""}</small></td>
    <td>${esc(shipping)}<small>${esc(carrier)} · ${esc(facts.tracking_no || "单号待读取")} · ${esc(trajectory)}</small></td>
    <td>${esc(wdt)}<span class="task-outcome ${outcome.kind}">${esc(outcome.text)}</span><small>${esc(decision.reason || "决策前会核验物流轨迹")}</small></td>
    <td>${taskSyncHtml(task)}</td>
    <td>${taskStateSelect(task)}<small>优先级 ${esc(task.queue_priority)} · 队列 #${esc(task.queue_rank)}</small></td>
    <td><button class="task-detail-button qianniu" type="button" onclick="showTask(${Number(task.id)})">查看千牛</button></td>
  </tr>`;
}

function renderTaskWorkspace(allTasks) {
  renderSourceTabs(allTasks);
  renderWorkOrderTypeFilter(allTasks);
  renderTasksHeader();
  const tasks = allTasks.filter((task) => {
    const facts = task.facts || {};
    const sourceMatches = selectedTaskSource === "all" || taskSource(task) === selectedTaskSource;
    const shopMatches = !selectedShopId || facts.shop_id === selectedShopId;
    const typeMatches = !selectedWorkOrderType || taskTitle(task) === selectedWorkOrderType;
    const statusMatches = !selectedTaskStatus
      || (taskStateAliases[String(task.state || "DISCOVERED")] || "NEED_REVIEW") === selectedTaskStatus;
    const urgencyMatches = taskMatchesUrgency(task);
    return sourceMatches && shopMatches && typeMatches && statusMatches
      && urgencyMatches && taskMatchesSearch(task);
  });
  updateTaskViewButton();
  const modeText = showProcessed ? "已处理" : "实时待处理";
  const sourceText = selectedTaskSource === "aftersale"
    ? "售后待处理"
    : selectedTaskSource === "qianniu" ? "千牛售后" : selectedTaskSource === "work_order" ? "工单管理" : "全部来源";
  const historyTotal = showProcessed ? processedTotal : allTasks.length;
  $("#task-view-summary").textContent = `${modeText} · ${sourceText} · 当前显示 ${tasks.length} / ${historyTotal} 条`;
  const pager = $("#processed-pagination");
  const dateFilter = $("#processed-date-filter");
  if (pager) pager.hidden = !showProcessed;
  if (dateFilter) dateFilter.hidden = !showProcessed;
  if (showProcessed) {
    $("#processed-page-summary").textContent = `第 ${processedPage} / ${processedPages} 页 · 共 ${processedTotal} 条`;
    $("#processed-prev").disabled = processedPage <= 1;
    $("#processed-next").disabled = processedPage >= processedPages;
  }
  if (!tasks.length) {
    $("#tasks").innerHTML = `<tr><td colspan="8" class="empty">当前筛选条件下暂无${showProcessed ? "已处理" : "待处理"}任务</td></tr>`;
    return;
  }
  const rows = [];
  const sources = selectedTaskSource === "all"
    ? ["work_order", "aftersale", "qianniu"]
    : [selectedTaskSource];
  for (const sourceName of sources) {
    const sourceTasks = tasks.filter((task) => taskSource(task) === sourceName);
    if (!sourceTasks.length) continue;
    if (selectedTaskSource === "all") {
      const title = sourceName === "aftersale" ? "售后待处理" : sourceName === "qianniu" ? "千牛售后（天猫）" : "工单管理";
      const hint = sourceName === "aftersale"
        ? "来自“待处理即将逾期”筛选"
        : sourceName === "qianniu" ? "天猫未收货退款 · 第 12 家扫描位" : "拼多多工单管理任务";
      rows.push(`<tr class="source-divider ${sourceName}"><td colspan="8"><b>${title}</b><span>${sourceTasks.length} 条 · ${hint}</span></td></tr>`);
    }
    const groups = new Map();
    for (const task of sourceTasks) {
      const shopId = task.facts?.shop_id || "unknown";
      if (!groups.has(shopId)) groups.set(shopId, []);
      groups.get(shopId).push(task);
    }
    for (const shopTasks of groups.values()) {
      const shop = shopTasks[0].facts || {};
      const currentCount = shopTasks.filter((task) => task.current_sync).length;
      const summary = showProcessed
        ? `已处理 ${shopTasks.length}`
        : `待处理 ${shopTasks.length} · 本轮同步 ${currentCount}`;
      const shopLabel = sourceName === "qianniu"
        ? `${esc(shop.shop_name || "示例天猫旗舰店")}（天猫）`
        : esc(shop.shop_name || "未知店铺");
      rows.push(`<tr class="shop-divider ${sourceName}"><td colspan="8"><b>${shopLabel}</b><small>${esc(shop.edge_profile || "-")} · ${esc(summary)}</small></td></tr>`);
      rows.push(...shopTasks.map((task) => {
        if (taskSource(task) === "qianniu") return renderQianniuRow(task);
        return isAftersaleTask(task) ? renderAftersaleRow(task) : renderWorkOrderRow(task);
      }));
    }
  }
  $("#tasks").innerHTML = rows.join("");
}

async function loadDashboard() {
  dashboard = await api("/api/dashboard");
  $("#mode").innerHTML = `<b>${dashboard.execution_mode.toUpperCase()}</b><span>真实操作：${dashboard.live_actions_enabled ? "开启" : "关闭"}</span>`;
  $("#metrics").innerHTML = [
    metric("实时待处理总数", dashboard.current_task_count ?? dashboard.task_count, "停止后保留最后有效队列"),
    metric("工单管理待处理", dashboard.current_work_order_count ?? 0, "11 家店铺工单管理"),
    metric("售后待处理", dashboard.current_aftersale_count ?? 0, "仅来自“待处理即将逾期”筛选"),
    metric("千牛售后待处理", dashboard.current_qianniu_count ?? 0, dashboard.qianniu_shop?.name ? `${dashboard.qianniu_shop.name} · 天猫` : "天猫未收货退款"),
  ].join("");
  renderShopFilter();
  updateTaskViewButton();
}

async function loadTasks() {
  let allTasks;
  if (showProcessed) {
    const history = await api(`/api/tasks/processed-history?${processedQuery(true)}`);
    allTasks = history.items || [];
    processedTotal = Number(history.total || 0);
    processedPage = Number(history.page || 1);
    processedPages = Number(history.pages || 1);
  } else {
    allTasks = await api("/api/tasks?view=pending");
    processedTotal = 0;
    processedPage = 1;
    processedPages = 1;
  }
  loadedTasks = allTasks;
  taskIndex = new Map(allTasks.map((task) => [Number(task.id), task]));
  renderTaskWorkspace(allTasks);
}

async function loadInterceptionRegisters() {
  const records = await api("/api/interception-registers");
  const count = $("#register-count");
  if (count) count.textContent = `${records.length} 条`;
  const body = $("#interception-registers");
  if (!body) return;
  if (!records.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">暂无登记记录</td></tr>';
    return;
  }
  const syncNote = (record, syncStatus) => {
    if (syncStatus === "succeeded") {
      return record.dingtalk_duplicate_tracking ? "已填入（重复）" : "已填入";
    }
    if (syncStatus === "running") return "正在填入";
    if (syncStatus === "pending") return "待填入";
    if (syncStatus === "failed") return "填入失败";
    return "待填入";
  };
  body.innerHTML = records.map((record) => {
    const syncStatus = record.dingtalk_sync_status || "pending";
    const retry = syncStatus === "pending"
      ? ` <button class="link register-retry" data-register-id="${Number(record.id)}" onclick="appendInterceptionRegister(${Number(record.id)})">追加填入</button>`
      : "";
    return `
    <tr>
      <td>${esc(record.record_date)}</td>
      <td><b>${esc(record.platform_label || "拼多多")}</b><small>${esc(record.shop_name || "店铺待读取")}</small></td>
      <td>${esc(record.carrier || "-")}</td>
      <td>${esc(record.tracking_no || "-")}</td>
      <td>${esc(record.order_no || "-")}</td>
      <td>${esc(record.refund_reason || "-")}</td>
      <td>¥${Number(record.amount || 0).toFixed(2)}</td>
      <td>${esc(syncNote(record, syncStatus))}${retry}</td>
    </tr>`;
  }).join("");
}

async function appendInterceptionRegister(registerId) {
  const buttons = [...document.querySelectorAll(`.register-retry[data-register-id="${registerId}"]`)];
  buttons.forEach((button) => {
    button.disabled = true;
    button.textContent = "正在填入…";
  });
  try {
    notice("正在将该条本地登记追加到钉钉在线表格；不会重新退款或发送拦截消息。");
    const result = await api(`/api/interception-registers/${registerId}/dingtalk/sync`, { method: "POST" });
    await loadInterceptionRegisters();
    notice(result.already_synced ? "该条登记已填入钉钉。" : "钉钉在线表格追加完成。");
  } catch (error) {
    await loadInterceptionRegisters();
    notice(error.message, true);
  }
}

async function loadPriorities() {
  priorityConfig = await api("/api/workflow-priorities");
  const byKey = Object.fromEntries(priorityConfig.workflows.map((item) => [item.workflow_key, item]));
  $("#priority-shipped").value = byKey.shipped_no_trace.priority;
  $("#priority-transit").value = byKey.in_transit_no_reason.priority;
  $("#priority-earlier-shipping").value = byKey.confirm_earlier_shipping.priority;
  $("#priority-aftersale-return").value = byKey.aftersale_return_refund.priority;
  $("#priority-aftersale-shipped").value = byKey.aftersale_shipped_refund.priority;
}

function runtimeBusy(state) {
  return ["STARTING", "RUNNING", "PAUSED", "STOPPING"].includes(state);
}

function completedCycleRegisterKey(status) {
  if (!status) return "";
  const terminal = ["COMPLETED", "FAILED", "STOPPED"].includes(status.state);
  const waitingForNextCycle = status.state === "RUNNING"
    && Boolean(status.continuous)
    && !status.current_task
    && Boolean(status.next_sync_at);
  if (!terminal && !waitingForNextCycle) return "";
  return `${status.run_id || "runtime"}:${status.cycle || 0}:${status.next_sync_at || status.state}`;
}

function renderRuntime(data) {
  runtimeStatus = data;
  const counts = data.counts || { total: 0, completed: 0, failed: 0, skipped: 0 };
  const done = counts.completed + counts.failed + counts.skipped;
  const percent = counts.total ? Math.round(done * 100 / counts.total) : 0;
  $("#runtime-state").textContent = data.state;
  $("#runtime-state").className = `status ${String(data.state).toLowerCase()}`;
  $("#runtime-phase").textContent = data.last_error ? `${data.phase}：${data.last_error}` : data.phase;
  const pollInterval = Math.round(Number(data.poll_interval_seconds) || 20);
  $("#runtime-loop").textContent = data.queue_only
    ? (runtimeBusy(data.state)
      ? "运行方式：单轮处理当前实时队列（已跳过店铺同步）"
      : "运行方式：当前队列单轮处理（当前已停止，可重新启动）")
    : (data.continuous
      ? (runtimeBusy(data.state)
        ? `运行方式：自动循环 · 间隔 ${pollInterval} 秒 · 第 ${data.cycle || 0} 轮 · 下一轮 ${data.next_sync_at || "处理中"}`
        : `运行方式：自动循环（当前已停止，可重新启动；间隔 ${pollInterval} 秒）`)
      : "运行方式：等待启动");
  $("#runtime-progress-bar").style.width = `${percent}%`;
  $(".runtime-progress").setAttribute("aria-valuenow", String(percent));
  $("#runtime-summary").textContent = `本轮总数 ${counts.total} · 完成 ${counts.completed} · 失败 ${counts.failed} · 跳过 ${counts.skipped} · 进度 ${percent}%`;
  $("#runtime-current").textContent = data.current_task
    ? `正在处理：${data.current_task.shop_name} / ${data.current_task.order_no} / 优先级 ${data.current_task.priority}`
    : "当前没有运行任务";
  const transitReadiness = data.readiness?.workflows?.in_transit_no_reason?.production;
  const transitStatus = $("#in-transit-readiness");
  if (transitStatus) {
    transitStatus.textContent = transitReadiness?.ready
      ? "在途无理由：快递群消息组件已就绪；正式运行会在每家店的无轨迹工单之后自动处理在途工单"
      : `在途无理由：暂不外发，${(transitReadiness?.reasons || ["消息组件尚未捕获"]).join("；")}`;
  }
  const aftersaleReadiness = data.readiness?.workflows?.aftersale_return_refund?.production;
  const aftersaleShippedReadiness = data.readiness?.workflows?.aftersale_shipped_refund?.production;
  const aftersaleStatus = $("#aftersale-readiness");
  if (aftersaleStatus) {
    const readyLabels = [
      ...(aftersaleReadiness?.ready ? ["退货退款延长收货"] : []),
      ...(aftersaleShippedReadiness?.ready ? ["已发货普通退款（≤3条同意；>3条退回/召回后退款或驳回）"] : []),
    ];
    const blockedReasons = [
      ...(!aftersaleReadiness?.ready ? (aftersaleReadiness?.reasons || []) : []),
      ...(!aftersaleShippedReadiness?.ready ? (aftersaleShippedReadiness?.reasons || []) : []),
    ];
    aftersaleStatus.textContent = readyLabels.length
      ? `售后工作台：已启用 ${readyLabels.join("、")}；仅采集待处理即将逾期`
      : `售后工作台：正式提交暂未开启，${[...new Set(blockedReasons.length ? blockedReasons : ["等待只读回归"])].join("；")}`;
  }
  $("#start-production").disabled = runtimeBusy(data.state) || !data.readiness.production.ready;
  $("#start-production").title = data.readiness.production.reasons.join("；");
  const currentQueueButton = $("#start-current-queue");
  const currentQueueEmpty = dashboard && Number(dashboard.current_task_count || 0) <= 0;
  currentQueueButton.disabled = runtimeBusy(data.state)
    || !data.readiness.production.ready
    || Boolean(currentQueueEmpty);
  currentQueueButton.title = currentQueueEmpty
    ? "当前没有实时待处理工单或售后单"
    : "跳过 11 店同步，单轮处理当前实时待处理队列";
  const runtimeToggle = $("#runtime-toggle");
  const canPause = ["STARTING", "RUNNING"].includes(data.state);
  const canResume = data.state === "PAUSED";
  runtimeToggle.disabled = !canPause && !canResume;
  runtimeToggle.dataset.action = canResume ? "resume" : "pause";
  runtimeToggle.setAttribute("aria-label", canResume ? "继续运行" : "暂停运行");
  runtimeToggle.title = canResume ? "继续运行" : "暂停运行";
  $("#runtime-toggle-icon").setAttribute("d", canResume ? "M8 5v14l11-7z" : "M6 5h4v14H6zM14 5h4v14h-4z");
  runtimeToggle.querySelector(".sr-only").textContent = canResume ? "继续运行" : "暂停运行";
  $("#runtime-stop").disabled = !["STARTING", "RUNNING", "PAUSED"].includes(data.state);
  const results = [...(data.results || [])].reverse();
  $("#runtime-results").innerHTML = results.length ? results.map((item) => `<tr>
    <td>#${item.task_id}</td>
    <td>${item.shop_name}<small>${item.order_no}</small></td>
    <td><span class="status ${String(item.status).toLowerCase()}">${item.status}</span></td>
    <td>${item.error || item.steps?.map((step) => step.step).join(" → ") || "-"}</td>
  </tr>`).join("") : '<tr><td colspan="4" class="empty">暂无运行记录</td></tr>';
}

async function loadRuntime() {
  const data = await api("/api/runtime/status");
  renderRuntime(data);
  return data;
}

function renderFactGrid(items) {
  return `<dl class="detail-facts">${items.map(([label, value, emphasis = false]) => `
    <div class="detail-fact${emphasis ? " emphasis" : ""}">
      <dt>${esc(label)}</dt><dd>${esc(value === undefined || value === null || value === "" ? "-" : value)}</dd>
    </div>`).join("")}</dl>`;
}

function renderDetailSection(title, content, hint = "") {
  return `<section class="detail-section">
    <div class="detail-section-head"><h3>${esc(title)}</h3>${hint ? `<span>${esc(hint)}</span>` : ""}</div>
    ${content}
  </section>`;
}

function renderEvidenceGallery(task) {
  const evidence = task.evidence || [];
  if (!evidence.length) {
    return renderDetailSection(
      "截图证据",
      '<div class="detail-empty-state"><b>尚无任务级截图</b><span>读取或处理该任务后，会在这里展示详情页和发送结果截图。</span></div>',
      "0 张"
    );
  }
  const cards = evidence.map((item) => {
    const type = String(item.evidence_type || "other");
    const label = type === "interception_group"
      ? "拦截消息发送证明"
      : (type === "pdd_aftersale_detail" || isAftersaleTask(task))
        ? "售后详情"
        : type === "pdd_work_order_detail" ? "拼多多工单详情" : "处理证据";
    const url = `/api/tasks/${Number(task.id)}/evidence/${Number(item.id)}`;
    return `<a class="evidence-card" target="_blank" rel="noopener" href="${url}">
      <span class="evidence-preview"><img loading="lazy" src="${url}" alt="${esc(label)}截图" /></span>
      <span class="evidence-meta"><b>${esc(label)}</b><small>${esc(formatTimestamp(item.created_at))}</small></span>
    </a>`;
  }).join("");
  return renderDetailSection("截图证据", `<div class="evidence-grid">${cards}</div>`, `${evidence.length} 张`);
}

const actionTypeLabels = {
  DISCOVER_PDD_READONLY: "发现待处理任务",
  RUN_WORKFLOW_DETAIL_READONLY: "读取拼多多工单详情",
  PDD_DETAIL_WORKER_LIFECYCLE: "工单详情读取",
  AFTERSALE_WORKER_LIFECYCLE: "售后详情读取",
  AFTERSALE_FINAL_ACTION: "执行售后操作",
  RUN_WDT_COMPONENT_WORKFLOW: "旺店通核验",
  INTERCEPT_MESSAGE_DISPATCHED: "发送拦截消息",
  EXECUTE_PDD_REAL_REFUND: "提交拼多多退款",
  RECONCILE_PDD_REAL_REFUND: "核验退款结果",
  AGREE_REFUND: "同意退款并核验",
  DINGTALK_INTERCEPTION_REGISTER: "登记钉钉在线表格",
  QIANNIU_INTERCEPT_TERMINAL: "确认千牛拦截结果",
  STATE_TRANSITION: "任务状态流转",
  STATE_STATUS_UPDATE: "任务状态更新",
  MANUAL_STATE_UPDATE: "人工修改状态",
};

function logBusinessSummary(log) {
  const details = log.details || {};
  if (details.reason) return shortText(details.reason);
  if (details.decision?.reason) return shortText(details.decision.reason);
  if (details.error) return shortText(details.error);
  if (details.result?.error) return shortText(details.result.error);
  if (details.from || details.to) return `${details.from || "-"} → ${details.to || "-"}`;
  if (details.action) return `动作：${actionLabel(details.action)}`;
  if (details.group?.group_name) return `目标群：${details.group.group_name}`;
  if (details.workflow_id) return `流程：${details.workflow_id}`;
  return log.status === "success" ? "已完成并写入审计记录" : log.status === "started" ? "正在执行" : "已记录";
}

function renderTimeline(task) {
  const logs = task.logs || [];
  const finishedTypes = new Set(logs.filter((log) => log.status !== "started").map((log) => log.action_type));
  const businessLogs = logs.filter((log) => {
    if (!actionTypeLabels[log.action_type] && log.status !== "failed") return false;
    return !(log.status === "started" && finishedTypes.has(log.action_type));
  });
  const technicalLogs = logs.filter((log) => !businessLogs.includes(log));
  const timeline = businessLogs.length
    ? `<ol class="task-timeline">${businessLogs.map((log) => {
      const state = log.status === "failed" ? "failed" : log.status === "started" ? "running" : "success";
      return `<li class="timeline-item ${state}">
        <i class="timeline-dot" aria-hidden="true"></i>
        <div class="timeline-content">
          <div><b>${esc(actionTypeLabels[log.action_type] || log.action_type)}</b><time>${esc(formatTimestamp(log.created_at))}</time></div>
          <p>${esc(logBusinessSummary(log))}</p>
        </div>
      </li>`;
    }).join("")}</ol>`
    : '<div class="detail-empty-state"><b>暂无业务处理记录</b><span>任务处理后会形成可读的业务时间线。</span></div>';
  const technical = technicalLogs.length
    ? `<details class="technical-logs"><summary>技术日志（${technicalLogs.length} 条）</summary>
      <ol>${technicalLogs.map((log) => `<li><span class="log-status ${esc(log.status)}">${esc(log.status)}</span><b>${esc(log.action_type)}</b><time>${esc(formatTimestamp(log.created_at))}</time></li>`).join("")}</ol>
    </details>`
    : "";
  return renderDetailSection("处理时间线", `${timeline}${technical}`, `${businessLogs.length} 个业务节点`);
}

function renderTaskSummary(task) {
  const facts = task.facts || {};
  const source = facts.source_evidence || {};
  const subtitle = isQianniuTask(task)
    ? `天猫未收货退款 · ${facts.refund_type || "未收货退款"} · ${source.aftersale_status || "退款待处理"}`
    : isAftersaleTask(task)
      ? `待处理即将逾期 · ${facts.refund_type || "退货退款"} · ${aftersaleRemainingText(facts) || "剩余时间待读取"}`
      : `${taskTitle(task)} · ${facts.work_order_no || "工单号待读取"}`;
  const orderLabel = isQianniuTask(task) ? "天猫订单" : "拼多多订单";
  return `<div class="detail-summary-card ${taskSource(task)}">
    <div class="detail-summary-tags">${taskSourceBadge(task)}<span class="status ${String(task.state || "").toLowerCase()}">${esc(task.display_state || task.state || "待处理")}</span></div>
    <h3>${esc(facts.shop_name || "未知店铺")}${isQianniuTask(task) ? "（天猫）" : ""}</h3>
    <p>${esc(subtitle)}</p>
    <div class="detail-order-number"><span>${orderLabel}</span><b>${esc(facts.order_no || "-")}</b></div>
    <div class="detail-summary-meta"><span>任务 #${Number(task.id)}</span><span>${task.current_sync === false ? "上轮保留" : "当前队列"}</span><span>队列 #${esc(task.queue_rank || "-")}</span></div>
  </div>${sourceStaleAlert(task)}`;
}

function renderAftersaleDetail(task) {
  const facts = task.facts || {};
  const source = facts.source_evidence || {};
  const card = source.aftersale_card || {};
  const decision = facts.aftersale_decision || source.aftersale_detail?.decision || {};
  const shipping = facts.shipping_status || facts.pdd_order_status || card.shipping_status || "尚未读取";
  const aftersaleNo = facts.aftersale_no || card.aftersale_no || "尚未读取";
  const aftersaleStatus = facts.aftersale_status || card.aftersale_status || card.status || "待处理即将逾期";
  const applyTime = facts.aftersale_apply_time || card.aftersale_apply_time || card.apply_time || "尚未读取";
  const description = facts.aftersale_description || card.aftersale_description || card.description || "尚未读取";
  const returnCarrier = facts.return_carrier || card.return_carrier || "尚未读取";
  const returnTrackingNo = facts.return_tracking_no || card.return_tracking_no || "尚未读取";
  const retry = source.aftersale_retry || {};
  const decisionReason = taskSourceStale(task)
    ? "本轮售后扫描失败，保留上轮数据仅供人工查看，禁止进入自动处理"
    : retry.retry_not_before
      ? `平台驳回冷却：${retry.prompt_text || "等待消费者回复或倒计时结束"}；计划 ${formatTimestamp(retry.retry_not_before)} 后重新处理（第 ${Number(retry.attempts || 1)} 次延期）`
      : decision.reason || "自动处理前会重新读取并核验售后详情";
  const action = facts.aftersale_final_action || decision.action || "";
  const overview = renderFactGrid([
    ["售后编号", aftersaleNo],
    ["售后类型", facts.refund_type || card.refund_type || "退货退款"],
    ["处理状态", aftersaleStatus],
    ["申请时间", applyTime],
    ["剩余处理时间", aftersaleRemainingText(facts) || "尚未读取", true],
    ["退款金额", `¥${Number(facts.refund_amount || card.refund_amount || 0).toFixed(2)}`, true],
    ["发货状态", shipping],
    ["退款原因", aftersaleActualReason(facts)],
    ["申请说明", description],
    ["退货快递", `${returnCarrier} / ${returnTrackingNo}`],
    ["列表位置", `第 ${source.page_no || card.page_no || 1} 页 · 第 ${(source.page_index ?? card.page_index ?? 0) + 1} 条`],
  ]);
  const resolution = `<div class="resolution-card ${taskSourceStale(task) ? "stale" : action === "NEED_REVIEW" ? "manual" : action === "EXTEND_RECEIPT" ? "success" : "pending"}">
    <span>当前处置</span><strong>${esc(taskSourceStale(task) ? "禁止自动处理" : aftersaleOutcome(facts))}</strong><p>${esc(decisionReason)}</p>
    <div><span>完整物流轨迹：${Number.isInteger(Number(facts.logistics_trajectory_count)) ? `${Number(facts.logistics_trajectory_count)} 条` : "尚未核验"}</span><span>当前动作：${esc(actionLabel(action) || "尚未决定")}</span><span>真实写入：${facts.aftersale_business_write_performed ? "已执行" : "未执行"}</span>${retry.retry_not_before ? `<span>下次尝试：${esc(formatTimestamp(retry.retry_not_before))}</span>` : ""}</div>
  </div>`;
  return `${renderTaskSummary(task)}
    ${renderDetailSection("售后概况", overview)}
    ${renderDetailSection("自动判断与处置", resolution)}
    ${renderEvidenceGallery(task)}
    ${renderTaskActions(task)}
    ${renderTimeline(task)}`;
}

function renderQianniuDetail(task) {
  const facts = task.facts || {};
  const source = facts.source_evidence || {};
  const decision = qianniuDecisionOf(task);
  const trajectory = source.qianniu_logistics_trajectory || {};
  const route = source.interception_route || {};
  const message = source.interception_message || {};
  const platformFeedback = source.qianniu_platform_feedback || {};
  const outcome = qianniuOutcome(task);
  const actions = (source.available_actions || []).join("、") || "待重新读取";
  const overview = renderFactGrid([
    ["售后编号", facts.work_order_no || source.aftersale_no || "尚未读取"],
    ["售后类型", facts.refund_type || "未收货退款"],
    ["售后状态", source.aftersale_status || "退款待处理"],
    ["发货状态", facts.pdd_order_status || "尚未读取"],
    ["退款金额", `¥${Number(facts.refund_amount || 0).toFixed(2)}`, true],
    ["可用操作", actions],
    ["列表位置", `第 ${source.page_no || 1} 页 · 第 ${(source.page_index ?? 0) + 1} 条 · 扫描位 ${source.shop_scan_order === 11 ? "12（千牛）" : "待读取"}`],
  ]);
  const wdtFacts = renderFactGrid([
    ["快递公司", facts.carrier || "尚未读取"],
    ["物流单号", facts.tracking_no || "尚未读取"],
    ["物流轨迹", Number.isInteger(Number(facts.logistics_trajectory_count))
      ? `${Number(facts.logistics_trajectory_count)} 条`
      : "尚未核验"],
    ["旺店通状态", facts.wdt_order_status || "尚未查询"],
    ["旺店通仓库", facts.wdt_warehouse || "尚未读取"],
    ["物流公司", facts.wdt_logistics_company || "尚未读取"],
    ["货品名称", (facts.wdt_product_names || []).join("；") || "尚未读取"],
    ["旺店通订单", facts.wdt_order_id || "尚未读取"],
  ]);
  const latestLogistics = facts.latest_logistics_text
    ? `<div class="detail-callout"><span>最新物流轨迹</span><p>${esc(facts.latest_logistics_text)}</p></div>`
    : "";
  const interceptionContent = route.group || message.group_name || decision.action
    ? renderFactGrid([
      ["拦截决策", qianniuActionLabel(decision.action) || decision.reason_code || "尚未决策"],
      ["决策依据", decision.reason || "自动处理前会核验旺店通与物流轨迹"],
      ["消息平台", route.group?.account_alias || route.group?.platform || message.platform || "尚未发送"],
      ["目标群", route.group?.group_name || message.group_name || "尚未匹配"],
      ["发送证明", message.sent_echo ? "已取得发送回显" : "尚无发送回显"],
      ["平台最终反馈", platformFeedback.verified
        ? platformFeedback.terminal_feedback || "已核验成功"
        : "尚未取得终态反馈"],
      ["钉钉登记", decision.requires_dingtalk_registration
        ? (task.state === "COMPLETED" ? "已完成" : "待退款后登记")
        : "本单不登记"],
      ["悬浮轨迹读取", trajectory.logistics_trajectory_count !== undefined
        ? `${Number(trajectory.logistics_trajectory_count)} 条`
        : "尚未读取"],
    ])
    : '<div class="detail-empty-state"><b>尚未进入拦截或退款阶段</b><span>完成千牛详情读取和旺店通核验后，会显示拦截路由、目标群和退款决策。</span></div>';
  const resolution = `<div class="resolution-card ${outcome.kind === "manual" ? "manual" : outcome.kind === "done" ? "success" : "pending"}">
    <span>当前处置</span><strong>${esc(outcome.text)}</strong><p>${esc(decision.reason || "等待运行时读取千牛详情、查询旺店通并核验物流轨迹后自动决策")}</p>
    <div><span>平台动作：${esc(qianniuActionLabel(decision.action) || "尚未决定")}</span><span>群拦截前置：${decision.requires_group_interception ? "需要" : "不需要"}</span><span>真实写入：${task.state === "COMPLETED" ? "已执行" : "未执行"}</span></div>
  </div>`;
  return `${renderTaskSummary(task)}
    ${renderDetailSection("千牛售后概况", overview)}
    ${renderDetailSection("物流与旺店通", `${wdtFacts}${latestLogistics}`)}
    ${renderDetailSection("拦截与退款决策", interceptionContent)}
    ${renderDetailSection("自动判断与处置", resolution)}
    ${renderEvidenceGallery(task)}
    ${renderTaskActions(task)}
    ${renderTimeline(task)}`;
}

function renderWorkOrderDetail(task) {
  const facts = task.facts || {};
  const source = facts.source_evidence || {};
  const route = source.interception_route || {};
  const message = source.interception_message || {};
  const productNames = (facts.wdt_product_names || []).join("；") || "尚未读取";
  const orderFacts = renderFactGrid([
    ["工单类型", taskTitle(task)],
    ["工单号", facts.work_order_no || "尚未读取"],
    ["退款金额", `¥${Number(facts.refund_amount || 0).toFixed(2)}`, true],
    ["退款原因", facts.refund_reason || "尚未读取"],
    ["工单截止时间", facts.work_order_deadline || "尚未读取"],
    ["买家说明", facts.buyer_description || "尚未读取"],
  ]);
  const logisticsFacts = renderFactGrid([
    ["快递公司", facts.carrier || "尚未读取"],
    ["物流单号", facts.tracking_no || "尚未读取"],
    ["旺店通状态", facts.wdt_order_status || "尚未查询"],
    ["旺店通订单", facts.wdt_order_id || "尚未读取"],
    ["仓库", facts.wdt_warehouse || "尚未读取"],
    ["物流公司", facts.wdt_logistics_company || "尚未读取"],
    ["货品名称", productNames],
    ["旺店通结论", facts.wdt_eligible ? "符合退款条件" : facts.wdt_result_count ? "不符合或需人工核验" : "尚未核验"],
  ]);
  const latestLogistics = facts.latest_logistics_text
    ? `<div class="detail-callout"><span>最新物流轨迹</span><p>${esc(facts.latest_logistics_text)}</p></div>`
    : "";
  const routeContent = route.group || message.group_name || facts.final_action
    ? renderFactGrid([
      ["路由类型", route.route_type || "尚未匹配"],
      ["消息平台", route.group?.account_alias || route.group?.platform || message.platform || "尚未发送"],
      ["目标群", route.group?.group_name || message.group_name || "尚未匹配"],
      ["拦截消息", route.message || "尚未生成"],
      ["发送证明", message.sent_echo ? "已取得发送回显" : "尚无发送回显"],
      ["最终动作", actionLabel(facts.final_action) || "尚未决定"],
      ["退款结果", facts.refund_agreed ? "商家已同意退款" : task.state === "COMPLETED" ? "任务已完成" : "尚未执行"],
    ])
    : '<div class="detail-empty-state"><b>尚未进入拦截或退款阶段</b><span>完成详情读取和旺店通核验后，会显示路由、发送目标和退款结果。</span></div>';
  return `${renderTaskSummary(task)}
    ${renderDetailSection("订单与退款", orderFacts)}
    ${renderDetailSection("物流与旺店通", `${logisticsFacts}${latestLogistics}`)}
    ${renderDetailSection("拦截与退款结果", routeContent)}
    ${renderEvidenceGallery(task)}
    ${renderTaskActions(task)}
    ${renderTimeline(task)}`;
}

function renderTaskActions(task) {
  const facts = task.facts || {};
  const id = Number(task.id);
  const statusControl = `<label class="detail-status-control"><span>人工调整任务状态</span>${taskStateSelect(task)}</label>`;
  if (isQianniuTask(task)) {
    return renderDetailSection("工作人员操作", `<div class="detail-actions qianniu-actions">
      <div class="detail-action-note"><b>天猫售后由正式队列自动处理</b><span>这里展示详情、物流、旺店通、群拦截与平台反馈，不提供会绕过正式顺序的单步业务按钮。</span></div>
      ${statusControl}
    </div>`, "第 12 家店铺 · 拼多多完成一轮后执行");
  }
  if (isAftersaleTask(task)) {
    return renderDetailSection("工作人员操作", `<div class="detail-actions aftersale-actions">
      <button class="secondary" type="button" onclick="collectAftersaleDetail(${id})" ${taskSourceStale(task) ? 'disabled title="本轮售后扫描失败，仅允许查看详情和人工调整状态"' : ""}>读取售后详情</button>
      ${statusControl}
    </div>`, taskSourceStale(task) ? "上轮数据仅允许人工状态操作" : "仅提供售后相关操作");
  }
  const groupName = `${facts.carrier || "快递"}拦截群`;
  return renderDetailSection("工作人员操作", `<div class="detail-actions">
    <button class="secondary" type="button" onclick="collectDetail(${id})">读取详情快递单号</button>
    <button class="secondary" type="button" onclick="checkWdtComponents(${id})">运行旺店通组件</button>
    ${facts.wdt_eligible ? `<button class="secondary" type="button" onclick="previewRefundComponents(${id})">预检退款组件</button>` : ""}
    <button class="secondary" type="button" onclick="lookupWdt(${id})">旺店通查单</button>
    <button type="button" onclick="evaluateTask(${id})">采集并评估</button>
    <button class="secondary" type="button" onclick="previewMessage(${id}, '${esc(groupName)}')">预览群消息</button>
    ${statusControl}
  </div>`);
}

function openTaskDrawer() {
  const panel = $("#task-detail-panel");
  const backdrop = $("#detail-backdrop");
  backdrop.hidden = false;
  panel.classList.add("is-open");
  panel.setAttribute("aria-hidden", "false");
  panel.scrollTop = 0;
  document.body.classList.add("detail-drawer-open");
}

function closeTaskDrawer() {
  const panel = $("#task-detail-panel");
  const backdrop = $("#detail-backdrop");
  panel.classList.remove("is-open");
  panel.setAttribute("aria-hidden", "true");
  backdrop.hidden = true;
  document.body.classList.remove("detail-drawer-open");
  selectedTaskId = null;
  document.querySelectorAll("#tasks tr[data-task-id]").forEach((row) => row.classList.remove("task-selected"));
}

async function showTask(id) {
  selectedTaskId = Number(id);
  openTaskDrawer();
  $("#detail").className = "detail-loading";
  $("#detail").innerHTML = '<span class="loading-spinner" aria-hidden="true"></span><b>正在读取任务详情…</b>';
  document.querySelectorAll("#tasks tr[data-task-id]").forEach((row) => {
    row.classList.toggle("task-selected", Number(row.dataset.taskId) === selectedTaskId);
  });
  try {
    const detailTask = await api(`/api/tasks/${id}`);
    const listTask = taskIndex.get(Number(id)) || {};
    const task = {
      ...listTask,
      ...detailTask,
      facts: { ...(listTask.facts || {}), ...(detailTask.facts || {}) },
    };
    taskIndex.set(Number(id), task);
    $("#detail").className = "";
    $("#detail").innerHTML = isQianniuTask(task)
      ? renderQianniuDetail(task)
      : isAftersaleTask(task)
        ? renderAftersaleDetail(task)
        : renderWorkOrderDetail(task);
    $("#task-detail-panel").scrollTop = 0;
  } catch (error) {
    $("#detail").className = "detail-error";
    $("#detail").innerHTML = `<b>任务详情读取失败</b><p>${esc(error.message)}</p>`;
    notice(error.message, true);
  }
}

async function collectAftersaleDetail(id) {
  const task = taskIndex.get(Number(id));
  if (taskSourceStale(task)) {
    notice("该售后单是上轮保留数据，本轮扫描失败，已禁止自动读取和处理；请先恢复该店售后扫描。", true);
    return;
  }
  if (!confirm("将打开该售后订单并只读采集详情与截图，完成后自动返回售后列表；不会点击延长收货、退款或提交。继续吗？")) return;
  try {
    notice("正在读取售后详情并生成任务截图，请暂时不要操作鼠标键盘…");
    await api(`/api/tasks/${id}/collect-aftersale-detail-readonly`, { method: "POST" });
    await Promise.all([loadDashboard(), loadTasks()]);
    await showTask(id);
    notice("售后详情和截图证据已更新；未执行任何售后写入");
  } catch (error) { notice(error.message, true); }
}

async function changeTaskStatus(id, select, currentState) {
  const target = select.value;
  const targetLabel = select.options[select.selectedIndex]?.textContent || target;
  if (target === currentState) return;
  if (!confirm(`确定将任务 #${id} 的状态人工修改为“${targetLabel}”吗？此操作会写入审计日志。`)) {
    select.value = currentState;
    return;
  }
  select.disabled = true;
  try {
    await api(`/api/tasks/${id}/status`, {
      method: "PATCH",
      body: JSON.stringify({ state: target, reason: `售后流程控制台人工修改为${targetLabel}` }),
    });
    await Promise.all([loadDashboard(), loadTasks()]);
    if (Number(selectedTaskId) === Number(id)) await showTask(id);
    notice(`任务 #${id} 已人工修改为“${targetLabel}”`);
  } catch (error) {
    select.value = currentState;
    select.disabled = false;
    notice(error.message, true);
  }
}

async function evaluateTask(id) {
  try {
    await api(`/api/tasks/${id}/evaluate`, { method: "POST" });
    await refreshAll();
    await showTask(id);
    notice("采集和规则评估完成");
  } catch (error) { notice(error.message, true); }
}

async function collectDetail(id) {
  if (!confirm("将唯一定位该订单卡片并点击“立即处理”，只读取详情与快递单号；不会点击退款、同意或提交。继续吗？")) return;
  try {
    notice("正在绑定工单详情，请暂时不要操作鼠标键盘…");
    const data = await api(`/api/tasks/${id}/collect-detail-readonly`, { method: "POST" });
    await refreshAll();
    await showTask(id);
    notice(`详情读取完成：${data.tracking_no}`);
  } catch (error) { notice(error.message, true); }
}

async function checkWdtComponents(id) {
  try {
    notice("正在运行旺店通固定组件，请暂时不要操作鼠标键盘…");
    const data = await api(`/api/tasks/${id}/check-wdt-components`, { method: "POST" });
    await showTask(id);
    notice(`旺店通校验通过：${data.order_status}`);
  } catch (error) { notice(error.message, true); }
}

async function previewRefundComponents(id) {
  try {
    notice("正在打开同一工单并预检退款组件；不会选择或提交…");
    const data = await api(`/api/tasks/${id}/preview-refund-components`, { method: "POST" });
    await showTask(id);
    notice(`第一阶段预检通过；仍有 ${data.capture_pending.length} 个第二阶段组件待捕获`);
  } catch (error) { notice(error.message, true); }
}

async function lookupWdt(id) {
  try {
    notice("正在旺店通按完整物流单号查询，请暂时不要操作鼠标键盘…");
    const data = await api(`/api/tasks/${id}/lookup-wdt`, { method: "POST" });
    await showTask(id);
    notice(data.eligible ? `旺店通校验通过：${data.order_status}` : `停止退款：${data.decision_message}`, !data.eligible);
  } catch (error) { notice(error.message, true); }
}

async function previewMessage(id, groupName) {
  try {
    const data = await api(`/api/tasks/${id}/preview-intercept`, {
      method: "POST",
      body: JSON.stringify({ group_name: groupName }),
    });
    notice(`仅预览，未发送：${data.message}`);
    await showTask(id);
  } catch (error) { notice(error.message, true); }
}

async function refreshAll() {
  await loadDashboard();
  await Promise.all([loadTasks(), loadPriorities(), loadRuntime(), loadInterceptionRegisters()]);
  lastRegisterRefreshKey = completedCycleRegisterKey(runtimeStatus);
}

$("#save-priorities").addEventListener("click", async () => {
  try {
    const byKey = Object.fromEntries(priorityConfig.workflows.map((item) => [item.workflow_key, item]));
    byKey.shipped_no_trace.priority = Number($("#priority-shipped").value);
    byKey.in_transit_no_reason.priority = Number($("#priority-transit").value);
    byKey.confirm_earlier_shipping.priority = Number($("#priority-earlier-shipping").value);
    byKey.aftersale_return_refund.priority = Number($("#priority-aftersale-return").value);
    byKey.aftersale_shipped_refund.priority = Number($("#priority-aftersale-shipped").value);
    const data = await api("/api/workflow-priorities", {
      method: "PUT",
      body: JSON.stringify({ definition: { ...priorityConfig, workflows: Object.values(byKey) } }),
    });
    priorityConfig = data.config;
    await loadTasks();
    notice("流程优先级已保存，任务队列已重新排序");
  } catch (error) { notice(error.message, true); }
});

$("#toggle-processed").addEventListener("click", async () => {
  showProcessed = !showProcessed;
  selectedWorkOrderType = "";
  selectedTaskStatus = "";
  $("#task-status-filter").value = "";
  processedPage = 1;
  closeTaskDrawer();
  updateTaskViewButton();
  try {
    await loadTasks();
  } catch (error) {
    notice(error.message, true);
  }
});

$("#export-processed").addEventListener("click", exportProcessedTasks);

async function applyTaskFilters() {
  if (showProcessed) {
    processedPage = 1;
    await loadTasks();
  } else {
    renderTaskWorkspace(loadedTasks);
  }
}

$("#task-source-tabs").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-source]");
  if (!button) return;
  selectedTaskSource = button.dataset.source || "all";
  selectedWorkOrderType = "";
  if (selectedTaskSource !== "aftersale") {
    selectedTaskUrgency = "";
    $("#task-urgency-filter").value = "";
  }
  try { await applyTaskFilters(); } catch (error) { notice(error.message, true); }
});

$("#task-search").addEventListener("input", (event) => {
  taskSearch = event.target.value;
  clearTimeout(taskSearchTimer);
  taskSearchTimer = setTimeout(async () => {
    try { await applyTaskFilters(); } catch (error) { notice(error.message, true); }
  }, 220);
});

$("#task-shop-filter").addEventListener("change", async (event) => {
  selectedShopId = event.target.value;
  try { await applyTaskFilters(); } catch (error) { notice(error.message, true); }
});

$("#task-type-filter").addEventListener("change", async (event) => {
  selectedWorkOrderType = event.target.value;
  try { await applyTaskFilters(); } catch (error) { notice(error.message, true); }
});

$("#task-status-filter").addEventListener("change", (event) => {
  selectedTaskStatus = event.target.value;
  renderTaskWorkspace(loadedTasks);
});

$("#task-urgency-filter").addEventListener("change", (event) => {
  selectedTaskUrgency = event.target.value;
  renderTaskWorkspace(loadedTasks);
});

for (const [selector, setter] of [
  ["#processed-date-from", (value) => { processedDateFrom = value; }],
  ["#processed-date-to", (value) => { processedDateTo = value; }],
]) {
  $(selector).addEventListener("change", async (event) => {
    setter(event.target.value || "");
    try { await applyTaskFilters(); } catch (error) { notice(error.message, true); }
  });
}

$("#processed-date-filter").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-history-days]");
  if (!button) return;
  const value = button.dataset.historyDays;
  if (value === "all") {
    processedDateFrom = "";
    processedDateTo = "";
  } else {
    const today = new Date();
    const localDate = (date) => [
      date.getFullYear(),
      String(date.getMonth() + 1).padStart(2, "0"),
      String(date.getDate()).padStart(2, "0"),
    ].join("-");
    const start = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    if (Number(value) > 0) start.setDate(start.getDate() - Number(value) + 1);
    processedDateFrom = localDate(start);
    processedDateTo = localDate(today);
  }
  $("#processed-date-from").value = processedDateFrom;
  $("#processed-date-to").value = processedDateTo;
  try { await applyTaskFilters(); } catch (error) { notice(error.message, true); }
});

$("#processed-prev").addEventListener("click", async () => {
  if (processedPage <= 1) return;
  processedPage -= 1;
  try { await loadTasks(); } catch (error) { notice(error.message, true); }
});

$("#processed-next").addEventListener("click", async () => {
  if (processedPage >= processedPages) return;
  processedPage += 1;
  try { await loadTasks(); } catch (error) { notice(error.message, true); }
});

$("#detail-close").addEventListener("click", closeTaskDrawer);
$("#detail-backdrop").addEventListener("click", closeTaskDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && $("#task-detail-panel").classList.contains("is-open")) closeTaskDrawer();
});

$("#start-production").addEventListener("click", async () => {
  const transitReady = Boolean(runtimeStatus?.readiness?.workflows?.in_transit_no_reason?.production?.ready);
  const workflowKeys = [
    "shipped_no_trace",
    ...(transitReady ? ["in_transit_no_reason"] : []),
    "confirm_earlier_shipping",
    ...(runtimeStatus?.readiness?.workflows?.aftersale_return_refund?.production?.ready
      ? ["aftersale_return_refund"]
      : []),
    ...(runtimeStatus?.readiness?.workflows?.aftersale_shipped_refund?.production?.ready
      ? ["aftersale_shipped_refund"]
      : []),
  ];
  const transitText = transitReady
    ? "每家店会先处理全部已发货无轨迹工单，再处理在途无理由工单并按已捕获的快递群组件执行拦截。"
    : "在途无理由消息组件尚未全部捕获，本次只处理已发货无轨迹工单。";
  const aftersaleReturnReady = Boolean(runtimeStatus?.readiness?.workflows?.aftersale_return_refund?.production?.ready);
  const aftersaleShippedReady = Boolean(runtimeStatus?.readiness?.workflows?.aftersale_shipped_refund?.production?.ready);
  const aftersaleText = aftersaleReturnReady || aftersaleShippedReady
    ? `同店工单管理完成后，只采集售后工作台“待处理即将逾期”；${aftersaleReturnReady ? "处理退货退款延长收货；" : ""}${aftersaleShippedReady ? "已发货普通退款完整物流≤3条时拦截后同意退款；>3条时先拦截，再按页面互斥按钮执行同意快递退回后退款或同意召回后退款；两者都没有时按平台弹窗驳回，30分钟冷却会自动延期。" : ""}`
    : "售后工作台分支尚未就绪，本次不会提交。";
  if (!confirm(`将启动持续自动循环：每轮同步所有已启用店铺、筛选目标并执行真实处理；${transitText} 每家店最后还会处理“确认更早发货时间”工单。${aftersaleText} 会产生真实写入，运行期间请勿操作鼠标键盘。确定启动吗？`)) return;
  try {
    renderRuntime(await api("/api/runtime/start", { method: "POST", body: JSON.stringify({ mode: "production", workflow_keys: workflowKeys, continuous: true }) }));
    notice("已启动正式全自动循环；需要停止时点击“停止”");
  } catch (error) { notice(error.message, true); }
});

$("#start-current-queue").addEventListener("click", async () => {
  const transitReady = Boolean(runtimeStatus?.readiness?.workflows?.in_transit_no_reason?.production?.ready);
  const workflowKeys = [
    "shipped_no_trace",
    ...(transitReady ? ["in_transit_no_reason"] : []),
    "confirm_earlier_shipping",
    ...(runtimeStatus?.readiness?.workflows?.aftersale_return_refund?.production?.ready
      ? ["aftersale_return_refund"]
      : []),
    ...(runtimeStatus?.readiness?.workflows?.aftersale_shipped_refund?.production?.ready
      ? ["aftersale_shipped_refund"]
      : []),
  ];
  const visibleCount = Number(dashboard?.current_task_count || 0);
  if (!confirm(`将跳过 11 家店铺重新同步，直接单轮处理后台当前完整实时待处理队列（共 ${visibleCount} 条，实际只进入已支持流程，不受页面店铺筛选影响；“上轮保留”任务也会在详情页重新核验是否仍待处理）。本次会真实发送拦截消息、操作售后并退款，完成后不会自动循环。运行期间请勿操作鼠标键盘。确定启动吗？`)) return;
  try {
    renderRuntime(await api("/api/runtime/start", {
      method: "POST",
      body: JSON.stringify({
        mode: "production",
        workflow_keys: workflowKeys,
        continuous: false,
        queue_only: true,
      }),
    }));
    notice("已启动当前实时队列单轮处理；已跳过店铺同步，完成后自动停止");
  } catch (error) { notice(error.message, true); }
});

for (const [selector, action] of [["#runtime-toggle", null], ["#runtime-stop", "stop"]]) {
  $(selector).addEventListener("click", async () => {
    try {
      const runtimeAction = action || $(selector).dataset.action || "pause";
      renderRuntime(await api(`/api/runtime/${runtimeAction}`, { method: "POST" }));
      if (runtimeAction === "stop") {
        await Promise.all([loadDashboard(), loadTasks()]);
        notice("已停止；实时待处理工单与售后单已保留");
      }
    }
    catch (error) { notice(error.message, true); }
  });
}

$("#open-shops").addEventListener("click", async () => {
  if (!confirm("将打开全部已启用店铺工单窗口、圆通客户管家和钉钉在线表格。只打开或复用窗口，不会自动登录、发送消息、退款或写入表格。确定继续吗？")) return;
  try {
    const data = await api("/api/pdd/open-shop-windows", { method: "POST" });
    const summary = data.summary || {};
    const failed = (data.results || []).filter((item) =>
      item.action === "failed" || item.action === "reuse_unverified" || item.action === "disabled" || item.verified === false
    );
    if (failed.length) {
      notice(`已打开/复用 ${summary.opened_or_reused || 0} 个窗口，但有 ${failed.length} 个失败：${failed.map((item) => `${item.kind}：${item.note}`).join("；")}`, true);
    } else {
      notice(`已打开/复用 ${summary.opened_or_reused || 0} 个窗口：全部店铺、圆通客户管家、钉钉在线表格`);
    }
  } catch (error) { notice(error.message, true); }
});

window.showTask = showTask;
window.changeTaskStatus = changeTaskStatus;
window.collectAftersaleDetail = collectAftersaleDetail;
window.appendInterceptionRegister = appendInterceptionRegister;
window.evaluateTask = evaluateTask;
window.previewMessage = previewMessage;
refreshAll().catch((error) => notice(error.message, true));
setInterval(async () => {
  try {
    await loadRuntime();
    const registerRefreshKey = completedCycleRegisterKey(runtimeStatus);
    if (registerRefreshKey && registerRefreshKey !== lastRegisterRefreshKey) {
      lastRegisterRefreshKey = registerRefreshKey;
      await loadInterceptionRegisters();
    }
    if (runtimeStatus && runtimeBusy(runtimeStatus.state)) {
      await loadDashboard();
      if (!document.activeElement?.closest(".tasks-panel")) await loadTasks();
    }
  } catch (_) {}
}, 2000);
