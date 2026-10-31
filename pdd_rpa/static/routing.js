const $ = (selector) => document.querySelector(selector);

const PLATFORM_META = {
  wecom: { label: "企业微信", accountAlias: "企业微信", messageMode: "group_chat" },
  wechat: { label: "微信", accountAlias: "微信", messageMode: "group_chat" },
  qq: { label: "QQ", accountAlias: "QQ", messageMode: "group_chat" },
  yto: { label: "圆通客户管家", accountAlias: "圆通专属客服", messageMode: "service_chat" },
};

let current = null;
let messageConfig = null;
let editingRouteIndex = null;
let routeBusy = false;

async function api(pathname, options = {}) {
  const response = await fetch(pathname, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (response.status === 401) {
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
    throw new Error("控制台登录已失效");
  }
  const raw = await response.text();
  let data = {};
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch {
    data = { detail: raw || `请求失败（HTTP ${response.status}）` };
  }
  if (!response.ok) {
    const error = new Error(data.detail || JSON.stringify(data));
    error.status = response.status;
    throw error;
  }
  return data;
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function normalizeWarehouse(value) {
  return String(value || "").replace(/\s+/g, "");
}

function platformMeta(platform) {
  const normalized = String(platform || "").trim().toLowerCase();
  return PLATFORM_META[normalized] || {
    label: normalized ? `${normalized}（当前配置）` : "未配置",
    accountAlias: normalized,
    messageMode: "group_chat",
  };
}

function setNotice(target, message = "", state = "") {
  target.textContent = message;
  target.className = `inline-notice${state ? ` ${state}` : ""}`;
}

function jsonFrom(selector, label) {
  try {
    const value = JSON.parse($(selector).value || "[]");
    if (!Array.isArray(value)) throw new Error("必须是 JSON 数组");
    return value;
  } catch (error) {
    throw new Error(`${label}不是有效配置：${error.message}`);
  }
}

function collectAdvanced() {
  if (!current) throw new Error("路由配置尚未加载，不能保存");
  return {
    ...clone(current),
    message_template: $("#message-template").value,
    fixed_reject: {
      ...(current.fixed_reject || {}),
      option_text: $("#reject-option").value,
      explanation_template: $("#reject-explanation").value,
    },
    carrier_groups: jsonFrom("#carrier-groups", "快递公司群"),
    supplier_groups: jsonFrom("#supplier-groups", "供应商群"),
    location_whitelists: jsonFrom("#location-whitelists", "退款地点白名单"),
    classification_rules: jsonFrom("#classification-rules", "物流分类规则"),
  };
}

function routeWarehouse(route) {
  return normalizeWarehouse(route?.warehouse || current?.jst_product_warehouse || "");
}

function knownWarehouses() {
  const values = [
    ...(current?.supported_warehouses || []),
    ...(current?.product_groups || []).map((route) => routeWarehouse(route)),
  ];
  return [...new Set(values.map(normalizeWarehouse).filter(Boolean))];
}

function populateWarehouseOptions() {
  const datalist = $("#warehouse-options");
  datalist.replaceChildren();
  for (const warehouse of knownWarehouses()) {
    const option = document.createElement("option");
    option.value = warehouse;
    datalist.append(option);
  }
}

function addCell(row, text, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = text;
  row.append(cell);
  return cell;
}

function ensurePlatformOption(platform) {
  const select = $("#route-platform");
  const normalized = String(platform || "").trim().toLowerCase();
  if (!normalized || [...select.options].some((option) => option.value === normalized)) return;
  const option = document.createElement("option");
  option.value = normalized;
  option.textContent = `${normalized}（仅保留当前配置）`;
  option.dataset.legacy = "true";
  select.append(option);
}

function renderRoutes() {
  const routes = Array.isArray(current?.product_groups) ? current.product_groups : [];
  $("#route-count").textContent = `共 ${routes.length} 条 · 货品名称按包含匹配`;
  const body = $("#route-list");
  body.replaceChildren();

  if (!routes.length) {
    const row = document.createElement("tr");
    addCell(row, "尚未配置货品路由，请使用上方四项表单新增。", "empty").colSpan = 6;
    body.append(row);
    return;
  }

  routes.forEach((route, index) => {
    const row = document.createElement("tr");
    const keywordCell = addCell(row, "");
    const keyword = document.createElement("b");
    keyword.textContent = String(route.keyword || "");
    keywordCell.append(keyword);
    addCell(row, routeWarehouse(route));

    const platformCell = addCell(row, "");
    const platform = String(route.platform || "").trim().toLowerCase();
    const platformBadge = document.createElement("span");
    platformBadge.className = `platform-badge platform-${platform.replace(/[^a-z0-9_-]/g, "")}`;
    platformBadge.textContent = platformMeta(platform).label;
    platformCell.append(platformBadge);

    addCell(row, String(route.group_name || ""), "group-name-cell");
    const statusCell = addCell(row, "");
    const status = document.createElement("span");
    status.className = `status ${route.enabled === false ? "disabled" : "enabled"}`;
    status.textContent = route.enabled === false ? "已停用" : "已启用";
    statusCell.append(status);

    const actionCell = addCell(row, "", "route-actions-cell");
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "secondary compact-button";
    edit.textContent = "修改";
    edit.addEventListener("click", () => beginRouteEdit(index));
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "secondary compact-button";
    toggle.textContent = route.enabled === false ? "启用" : "停用";
    toggle.setAttribute(
      "aria-label",
      `${toggle.textContent}货品路由：${String(route.keyword || "未命名")}`,
    );
    toggle.addEventListener("click", () => toggleRoute(index));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger compact-button";
    remove.textContent = "删除";
    remove.addEventListener("click", () => deleteRoute(index));
    actions.append(edit, toggle, remove);
    actionCell.append(actions);
    body.append(row);
  });
}

function showConfig(config) {
  current = clone(config);
  $("#version").textContent = `配置版本 ${config.version}`;
  $("#message-template").value = config.message_template || "";
  $("#reject-explanation").value = config.fixed_reject?.explanation_template || "";

  const rejectSelect = $("#reject-option");
  const selectedReject = config.fixed_reject?.option_text || "";
  rejectSelect.replaceChildren(new Option("请选择捕获后的固定选项", ""));
  if (selectedReject) rejectSelect.append(new Option(selectedReject, selectedReject, true, true));

  const editors = [
    ["#carrier-groups", "carrier_groups"],
    ["#supplier-groups", "supplier_groups"],
    ["#location-whitelists", "location_whitelists"],
    ["#classification-rules", "classification_rules"],
  ];
  for (const [selector, key] of editors) {
    $(selector).value = JSON.stringify(config[key] || [], null, 2);
  }

  populateWarehouseOptions();
  renderRoutes();
  $("#save").disabled = false;
  updateRouteSummary();
}

function routeFormValues() {
  return {
    keyword: $("#route-keyword").value.trim(),
    warehouse: normalizeWarehouse($("#route-warehouse").value),
    platform: $("#route-platform").value.trim().toLowerCase(),
    groupName: $("#route-group-name").value.trim(),
  };
}

function updateRouteSummary() {
  const summary = $("#route-summary");
  const saveButton = $("#save-route");
  const values = routeFormValues();
  const missing = [];
  if (!values.keyword) missing.push("货品名称");
  if (!values.warehouse) missing.push("仓库");
  if (!values.platform) missing.push("拦截群平台");
  if (!values.groupName) missing.push("拦截群名称");

  if (missing.length) {
    summary.textContent = `还需填写：${missing.join("、")}。`;
    summary.classList.remove("is-ready", "is-warning");
  } else {
    const isNewWarehouse = !knownWarehouses().includes(values.warehouse);
    summary.textContent = `仓库“${values.warehouse}”中，货品名称包含“${values.keyword}” → ${platformMeta(values.platform).label}“${values.groupName}”。${isNewWarehouse ? " 这是新仓库，保存后会自动加入白名单。" : ""}`;
    summary.classList.toggle("is-warning", isNewWarehouse);
    summary.classList.toggle("is-ready", !isNewWarehouse);
  }
  saveButton.disabled = Boolean(missing.length || !current || routeBusy);
}

function resetRouteForm({ keepNotice = false } = {}) {
  $("#route-form").reset();
  editingRouteIndex = null;
  $("#route-builder-title").textContent = "新增货品拦截路由";
  $("#save-route").textContent = "保存并自动配置";
  $("#cancel-route").hidden = true;
  for (const option of [...$("#route-platform").options]) {
    if (option.dataset.legacy === "true") option.remove();
  }
  if (!keepNotice) setNotice($("#route-notice"));
  updateRouteSummary();
}

function beginRouteEdit(index) {
  if (!current || !current.product_groups?.[index]) return;
  const route = current.product_groups[index];
  editingRouteIndex = index;
  $("#route-keyword").value = route.keyword || "";
  $("#route-warehouse").value = routeWarehouse(route);
  ensurePlatformOption(route.platform);
  $("#route-platform").value = String(route.platform || "").trim().toLowerCase();
  $("#route-group-name").value = route.group_name || "";
  $("#route-builder-title").textContent = "修改货品拦截路由";
  $("#save-route").textContent = "保存修改";
  $("#cancel-route").hidden = false;
  setNotice($("#route-notice"), "正在修改现有路由；未点击保存前不会变更配置。");
  updateRouteSummary();
  $(".route-builder-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function routeMapping(values, original = null) {
  const meta = platformMeta(values.platform);
  const samePlatform = String(original?.platform || "").trim().toLowerCase() === values.platform;
  return {
    ...(original || {}),
    keyword: values.keyword,
    warehouse: values.warehouse,
    platform: values.platform,
    account_alias: samePlatform && original?.account_alias ? original.account_alias : meta.accountAlias,
    group_name: values.groupName,
    message_mode: samePlatform && original?.message_mode ? original.message_mode : meta.messageMode,
    enabled: original?.enabled === false ? false : true,
  };
}

async function persistRouteDraft(draft, successMessage, { resetForm = true } = {}) {
  if (routeBusy) return;
  routeBusy = true;
  updateRouteSummary();
  setNotice($("#route-notice"), "正在校验并保存…");
  try {
    const data = await api("/api/in-transit-routing/config", {
      method: "PUT",
      body: JSON.stringify({ definition: draft, expected_version: current.version }),
    });
    showConfig(data.config);
    if (resetForm) resetRouteForm({ keepNotice: true });
    setNotice($("#route-notice"), successMessage, "ok");
  } catch (error) {
    const prefix = error.status === 409 ? "配置已被其他页面更新，请刷新后重试：" : "";
    setNotice($("#route-notice"), `${prefix}${error.message}`, "error");
  } finally {
    routeBusy = false;
    updateRouteSummary();
  }
}

async function saveRoute(event) {
  event.preventDefault();
  if (!current) {
    setNotice($("#route-notice"), "路由配置尚未加载，不能保存。", "error");
    return;
  }
  const values = routeFormValues();
  if (!values.keyword || !values.warehouse || !values.platform || !values.groupName) {
    updateRouteSummary();
    setNotice($("#route-notice"), "请完整填写货品名称、仓库、平台和群名。", "error");
    return;
  }
  if (!PLATFORM_META[values.platform] && editingRouteIndex === null) {
    setNotice($("#route-notice"), "该平台没有可用的发送组件，不能新增。", "error");
    return;
  }
  const isNewWarehouse = !knownWarehouses().includes(values.warehouse);
  if (
    isNewWarehouse
    && !window.confirm(
      `将新增在途仓库白名单：\n\n${values.warehouse}\n\n请确认仓库名称和文字完全正确。`,
    )
  ) {
    setNotice($("#route-notice"), "已取消保存；仓库白名单没有变更。");
    return;
  }

  const draft = clone(current);
  const routes = Array.isArray(draft.product_groups) ? draft.product_groups : [];
  const duplicateIndex = routes.findIndex((route, index) => (
    index !== editingRouteIndex
    && String(route.keyword || "").trim() === values.keyword
    && routeWarehouse(route) === values.warehouse
  ));

  let targetIndex = editingRouteIndex;
  let successMessage = editingRouteIndex === null ? "路由已新增并自动配置完成。" : "路由修改已保存。";
  if (editingRouteIndex !== null && duplicateIndex >= 0) {
    setNotice($("#route-notice"), "同一仓库已经存在完全相同的货品关键词，请直接修改那条规则。", "error");
    return;
  }
  if (targetIndex === null && duplicateIndex >= 0) {
    targetIndex = duplicateIndex;
    successMessage = "检测到同仓库同关键词，已更新原路由，没有生成重复规则。";
  }

  const original = targetIndex === null ? null : routes[targetIndex];
  const mapping = routeMapping(values, original);
  if (targetIndex === null) routes.push(mapping);
  else routes[targetIndex] = mapping;
  draft.product_groups = routes;
  draft.supported_warehouses = [...new Set([
    ...(draft.supported_warehouses || []).map(normalizeWarehouse),
    values.warehouse,
  ].filter(Boolean))];
  draft.supported_platforms = [...new Set([
    ...(draft.supported_platforms || []),
    values.platform,
  ])];
  await persistRouteDraft(draft, successMessage);
}

async function toggleRoute(index) {
  if (!current || !current.product_groups?.[index] || routeBusy) return;
  const draft = clone(current);
  const route = draft.product_groups[index];
  const nextEnabled = route.enabled === false;
  route.enabled = nextEnabled;
  await persistRouteDraft(
    draft,
    `路由已${nextEnabled ? "启用" : "停用"}；正式流程会使用最新状态。`,
    { resetForm: false },
  );
}

async function deleteRoute(index) {
  if (!current || !current.product_groups?.[index] || routeBusy) return;
  const route = current.product_groups[index];
  const warehouse = routeWarehouse(route);
  const label = `${warehouse} / ${route.keyword || "未命名"} / ${route.group_name || "未命名群"}`;
  const hasAnotherWarehouseRoute = current.product_groups.some((item, itemIndex) => (
    itemIndex !== index && routeWarehouse(item) === warehouse
  ));
  const whitelistNotice = hasAnotherWarehouseRoute
    ? "该仓库还有其他货品路由，仓库白名单会保留。"
    : `这是仓库“${warehouse}”的最后一条货品路由，删除时会同步移出仓库白名单。`;
  if (!window.confirm(`确定删除这条路由吗？\n\n${label}\n\n${whitelistNotice}\n删除后会立即保存配置，但不会启动正式流程。`)) return;

  const draft = clone(current);
  draft.product_groups.splice(index, 1);
  if (!hasAnotherWarehouseRoute) {
    draft.supported_warehouses = (draft.supported_warehouses || [])
      .map(normalizeWarehouse)
      .filter((item) => item && item !== warehouse);
  }
  routeBusy = true;
  updateRouteSummary();
  try {
    const data = await api("/api/in-transit-routing/config", {
      method: "PUT",
      body: JSON.stringify({ definition: draft, expected_version: current.version }),
    });
    showConfig(data.config);
    if (editingRouteIndex === index) resetRouteForm();
    setNotice($("#route-notice"), "路由已删除。", "ok");
  } catch (error) {
    setNotice($("#route-notice"), error.message, "error");
  } finally {
    routeBusy = false;
    updateRouteSummary();
  }
}

function previewInputs() {
  return {
    wdt_warehouse: $("#p-warehouse").value,
    wdt_product_names: $("#p-products").value
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean),
    wdt_logistics_company: $("#p-wdt").value,
    carrier: $("#p-carrier").value,
    tracking_no: $("#p-tracking").value,
    latest_logistics_text: $("#p-logistics").value,
    jst_supplier: $("#p-jst").value,
    wdt_log_company: $("#p-wdt-company").value,
  };
}

function renderMessageConfig(config, readiness) {
  messageConfig = clone(config);
  $("#message-version").textContent = `配置版本 ${config.version}`;
  $("#message-enabled").checked = Boolean(config.enabled);
  $("#save-message").disabled = false;
  const container = $("#message-readiness");
  container.replaceChildren();
  const platforms = readiness.platforms || {};
  for (const [platform, item] of Object.entries(platforms)) {
    const card = document.createElement("div");
    card.className = item.ready ? "ready" : "pending";
    const title = document.createElement("b");
    title.textContent = `${platformMeta(platform).label} · ${item.ready ? "已就绪" : "待配置"}`;
    const reason = document.createElement("span");
    reason.textContent = item.ready ? "组件、群名和消息回读均可校验" : (item.reasons || []).join("；");
    card.append(title, reason);
    container.append(card);
  }
  if (!container.childElementCount) container.textContent = "尚未配置消息平台";
}

async function loadMessageConfig() {
  try {
    const [config, readiness] = await Promise.all([
      api("/api/message-dispatch/config"),
      api("/api/message-dispatch/readiness"),
    ]);
    renderMessageConfig(config, readiness);
  } catch (error) {
    $("#message-readiness").textContent = `消息组件状态读取失败：${error.message}`;
    $("#message-readiness").classList.add("error");
  }
}

async function loadRoutingConfig() {
  $("#save-route").disabled = true;
  $("#save").disabled = true;
  try {
    showConfig(await api("/api/in-transit-routing/config"));
    resetRouteForm();
  } catch (error) {
    current = null;
    $("#version").textContent = "读取失败";
    $("#route-count").textContent = "配置不可用";
    const body = $("#route-list");
    body.replaceChildren();
    const row = document.createElement("tr");
    addCell(row, `读取失败：${error.message}。请刷新页面后重试。`, "empty error").colSpan = 6;
    body.append(row);
    setNotice($("#route-notice"), "配置未完整加载，所有写操作已禁用。", "error");
    updateRouteSummary();
  }
}

$("#route-form").addEventListener("submit", saveRoute);
$("#cancel-route").addEventListener("click", () => resetRouteForm());
for (const selector of ["#route-keyword", "#route-warehouse", "#route-platform", "#route-group-name"]) {
  $(selector).addEventListener("input", updateRouteSummary);
  $(selector).addEventListener("change", updateRouteSummary);
}

$("#save").addEventListener("click", async () => {
  try {
    const data = await api("/api/in-transit-routing/config", {
      method: "PUT",
      body: JSON.stringify({ definition: collectAdvanced(), expected_version: current.version }),
    });
    showConfig(data.config);
    setNotice($("#notice"), "高级配置已校验并保存。", "ok");
  } catch (error) {
    setNotice($("#notice"), error.message, "error");
  }
});

$("#preview").addEventListener("click", async () => {
  const notice = $("#preview-notice");
  try {
    setNotice(notice, "正在计算路由…");
    const data = await api("/api/in-transit-routing/preview", {
      method: "POST",
      body: JSON.stringify({ inputs: previewInputs() }),
    });
    $("#result").textContent = JSON.stringify(data, null, 2);
    setNotice(notice, data.ok ? "预演完成，未发送消息。" : "预演完成：该输入会进入人工复核。", data.ok ? "ok" : "error");
  } catch (error) {
    $("#result").textContent = error.message;
    setNotice(notice, "预演失败。", "error");
  }
});

$("#save-message").addEventListener("click", async () => {
  try {
    const latest = await api("/api/message-dispatch/config");
    const data = await api("/api/message-dispatch/config", {
      method: "PUT",
      body: JSON.stringify({ definition: { ...latest, enabled: $("#message-enabled").checked } }),
    });
    setNotice($("#message-notice"), "发送开关已保存。", "ok");
    const readiness = await api("/api/message-dispatch/readiness");
    renderMessageConfig(data.config, readiness);
  } catch (error) {
    setNotice($("#message-notice"), error.message, "error");
  }
});

$("#message-preflight").addEventListener("click", async () => {
  const platform = $("#message-preflight-platform").value;
  const groupName = $("#message-preflight-group").value.trim();
  const notice = $("#message-preflight-notice");
  if (!groupName) {
    setNotice(notice, "请填写群或专属客服会话名称。", "error");
    return;
  }
  try {
    setNotice(notice, "正在打开并回读群标题；不会输入或发送消息…");
    const data = await api("/api/message-dispatch/preflight", {
      method: "POST",
      body: JSON.stringify({ platform, group_name: groupName }),
    });
    setNotice(notice, `只读预检通过：${data.result.group_header || groupName}`, "ok");
  } catch (error) {
    setNotice(notice, error.message, "error");
  }
});

Promise.allSettled([loadRoutingConfig(), loadMessageConfig()]);
