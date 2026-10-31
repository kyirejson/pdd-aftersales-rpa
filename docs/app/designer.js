const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));

let catalog = [];
let nodes = [];
let selectedId = null;
let captureResult = null;
let workflowId = "";
const desktopApplications = {
  "desktop-wdt": { title: "旺店通ERP旗舰版", label: "旺店通ERP旗舰版", platform: "" },
  "desktop-wecom": { title: "企业微信", label: "企业微信", platform: "wecom" },
  "desktop-wechat": { title: "微信", label: "微信", platform: "wechat" },
};
const messageRoles = {
  wecom: [
    ["search_input", "CMP-MSG-WECOM-SEARCH-INPUT", "搜索框（输入群名）", "InputDesktopText", { value_variable: "group_name" }],
    ["search_result", "CMP-MSG-WECOM-SEARCH-RESULT", "搜索结果第一项（点击群）", "ClickDesktopElement", {}],
    ["group_header", "CMP-MSG-WECOM-GROUP-HEADER", "群聊标题（回读群名；鼠标放在标题文字中间）", "ReadDesktopText", { output_variable: "group_header" }],
    ["message_input", "CMP-MSG-WECOM-MESSAGE-INPUT", "消息输入框（输入单号+拦截）", "InputDesktopText", { value_variable: "message" }],
    ["send_button", "CMP-MSG-WECOM-SEND", "发送按钮", "ClickDesktopElement", {}],
  ],
  wechat: [
    ["search_input", "CMP-MSG-WECHAT-SEARCH-INPUT", "搜索框（输入群名）", "InputDesktopText", { value_variable: "group_name" }],
    ["group_header", "CMP-MSG-WECHAT-GROUP-HEADER", "群聊标题（回读群名；鼠标放在标题文字中间）", "ReadDesktopText", { output_variable: "group_header" }],
    ["message_input", "CMP-MSG-WECHAT-MESSAGE-INPUT", "消息输入框（输入单号+拦截）", "InputDesktopText", { value_variable: "message" }],
    ["send_button", "CMP-MSG-WECHAT-SEND", "发送按钮", "ClickDesktopElement", {}],
  ],
};
const desktopTarget = () => desktopApplications[$("#capture-application").value] || null;
const isDesktopCapture = () => Boolean(desktopTarget());
const isMessageCapture = () => Boolean(desktopTarget()?.platform);
const selectedMessageRole = () => {
  const platform = desktopTarget()?.platform;
  return (messageRoles[platform] || []).find((item) => item[0] === $("#capture-role").value) || null;
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
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `请求失败 ${response.status}`);
  }
  return response.json();
}

function setCaptureState(status, title, message) {
  const element = $("#capture-state");
  element.className = `capture-state ${status}`;
  element.innerHTML = `<b>${escapeHtml(title)}</b><span>${escapeHtml(message)}</span>`;
}

function renderCatalog() {
  const groups = {};
  catalog.forEach((item) => (groups[item.category] ||= []).push(item));
  $("#catalog").innerHTML = Object.entries(groups).map(([group, items]) => `
    <div class="catalog-group">${escapeHtml(group)}</div>
    ${items.map((item) => `<div class="catalog-item ${item.write_capable ? "write" : ""}" draggable="true" data-type="${item.component_type}" data-component-id="${item.component_id || ""}">
      <b>${escapeHtml(item.name)}</b><span>${escapeHtml(item.description)}</span>
    </div>`).join("")}
  `).join("");
  document.querySelectorAll(".catalog-item").forEach((element) => {
    element.ondragstart = (event) => event.dataTransfer.setData("text/component-type", element.dataset.type);
    element.onclick = () => {
      const saved = catalog.find((item) => item.component_id && item.component_id === element.dataset.componentId);
      addNode(element.dataset.type, saved || null);
    };
  });
}

function addNode(componentType, component = null) {
  const spec = catalog.find((item) => item.component_type === componentType);
  const nodeId = `node_${Date.now()}_${Math.random().toString(16).slice(2, 7)}`;
  nodes.push({
    node_id: nodeId,
    component_id: component?.component_id || "",
    component_type: componentType,
    name: component?.name || spec?.name || componentType,
    locator: component?.locator || {},
    config: component?.config || {},
    on_failure: component?.on_failure || "MANUAL_REVIEW",
  });
  selectedId = nodeId;
  renderCanvas();
  renderProperties();
}

function renderCanvas() {
  const canvas = $("#canvas");
  if (!nodes.length) {
    canvas.innerHTML = '<div class="canvas-empty">从左侧拖入组件，或点击组件添加</div>';
    return;
  }
  canvas.innerHTML = nodes.map((node, index) => `<div class="flow-node ${node.node_id === selectedId ? "selected" : ""}" data-id="${node.node_id}">
    <span class="node-type">${String(index + 1).padStart(2, "0")} · ${node.component_type}</span>
    <b>${escapeHtml(node.name)}</b><button class="remove-node" title="删除">×</button>
  </div>`).join("");
  document.querySelectorAll(".flow-node").forEach((element) => {
    element.onclick = (event) => {
      if (event.target.closest(".remove-node")) {
        nodes = nodes.filter((node) => node.node_id !== element.dataset.id);
        if (selectedId === element.dataset.id) selectedId = null;
      } else selectedId = element.dataset.id;
      renderCanvas();
      renderProperties();
    };
  });
}

function renderProperties() {
  const node = nodes.find((item) => item.node_id === selectedId);
  const box = $("#properties");
  if (!node) {
    box.className = "properties empty-property";
    box.textContent = "选择一个流程节点";
    return;
  }
  box.className = "properties";
  box.innerHTML = `
    <label>节点名称<input id="prop-name" value="${escapeHtml(node.name)}"></label>
    <label>组件类型<input value="${escapeHtml(node.component_type)}" disabled></label>
    <label>失败处理<select id="prop-failure"><option value="MANUAL_REVIEW">转人工复核</option><option value="STOP">停止流程</option><option value="SKIP">跳过</option></select></label>
    <label>定位器（JSON）<textarea id="prop-locator" rows="7">${escapeHtml(JSON.stringify(node.locator || {}, null, 2))}</textarea></label>
    <label>参数（JSON）<textarea id="prop-config" rows="5">${escapeHtml(JSON.stringify(node.config || {}, null, 2))}</textarea></label>
    <button id="apply-property">应用</button>`;
  $("#prop-failure").value = node.on_failure || "MANUAL_REVIEW";
  $("#apply-property").onclick = () => {
    try {
      node.name = $("#prop-name").value.trim() || node.name;
      node.locator = JSON.parse($("#prop-locator").value || "{}");
      node.config = JSON.parse($("#prop-config").value || "{}");
      node.on_failure = $("#prop-failure").value;
      renderCanvas();
      renderProperties();
      $("#save-state").textContent = "属性已应用，尚未保存";
    } catch (error) { alert(`JSON 格式错误：${error.message}`); }
  };
}

async function startCapture() {
  const shopId = $("#shop").value;
  const target = desktopTarget();
  try {
    setCaptureState("capturing", "正在启动…", isDesktopCapture() ? `准备捕获${target.label}桌面控件` : "正在校验对应店铺 Edge 窗口");
    if (isDesktopCapture()) {
      await api("/api/desktop-capture/start", { method: "POST", body: JSON.stringify({ application: target.title, delay_seconds: 3 }) });
    } else {
      await api("/api/capture/start", { method: "POST", body: JSON.stringify({ shop_id: shopId }) });
    }
    $("#start-capture").disabled = true;
    $("#read-capture").disabled = false;
    $("#cancel-capture").disabled = false;
    setCaptureState("capturing", isDesktopCapture() ? "倒计时捕获" : "等待点击", isDesktopCapture() ? `3秒内把鼠标移到${target.label}目标控件，不需要点击` : "到对应 Edge 点击元素；原点击会被拦截，然后回来点“读取结果”");
  } catch (error) { setCaptureState("error", "启动失败", error.message); }
}

function resetCaptureButtons() {
  $("#start-capture").disabled = false;
  $("#read-capture").disabled = true;
  $("#cancel-capture").disabled = true;
}

async function readCapture() {
  try {
    const data = isDesktopCapture() ? await api("/api/desktop-capture/status") : await api(`/api/capture/${$("#shop").value}`);
    if (data.status !== "captured") {
      setCaptureState("capturing", "尚未捕获", isDesktopCapture() ? "倒计时尚未结束，请稍后读取" : "请先在对应 Edge 页面点击目标元素");
      return;
    }
    captureResult = data.result;
    resetCaptureButtons();
    $("#capture-result").classList.remove("hidden");
    const capturedText = isDesktopCapture() ? captureResult.element?.name : captureResult.text;
    const capturedLocator = isDesktopCapture() ? `${captureResult.element?.control_type || "Custom"} · ${captureResult.window?.title || ""}` : captureResult.selector;
    $("#captured-text").textContent = capturedText || "（无文本）";
    $("#captured-selector").textContent = capturedLocator || "（无选择器）";
    const role = selectedMessageRole();
    $("#capture-name").value = role ? role[2] : (capturedText ? `操作 ${capturedText.slice(0, 24)}` : "旺店通桌面控件");
    setCaptureState("captured", "捕获成功", isDesktopCapture() ? "已记录控件层级、相对位置和图像模板" : "原页面点击已拦截，可保存为组件");
  } catch (error) { resetCaptureButtons(); setCaptureState("error", "读取失败", error.message); }
}

async function cancelCapture() {
  const path = isDesktopCapture() ? "/api/desktop-capture/cancel" : `/api/capture/${$("#shop").value}/cancel`;
  await api(path, { method: "POST", body: "{}" }).catch(() => null);
  resetCaptureButtons();
  setCaptureState("idle", "已取消", "未创建组件");
}

async function saveCapture() {
  if (!captureResult) return;
  try {
    const path = isDesktopCapture() ? "/api/desktop-capture/component" : "/api/capture/component";
    const role = selectedMessageRole();
    const data = await api(path, { method: "POST", body: JSON.stringify({
      name: $("#capture-name").value.trim(), component_type: $("#capture-type").value, capture: captureResult,
      component_id: role?.[1] || "",
      config: role?.[4] || {},
    }) });
    catalog.push({ ...data.component, category: "已捕获", description: data.component.locator.text || data.component.locator.selector });
    renderCatalog();
    addNode(data.component.component_type, data.component);
    $("#capture-result").classList.add("hidden");
    setCaptureState("captured", "组件已保存", `${data.component.name} 已加入流程`);
  } catch (error) { setCaptureState("error", "保存失败", error.message); }
}

async function saveWorkflow() {
  if (!nodes.length) return alert("请先添加至少一个组件");
  const definition = {
    workflow_id: workflowId,
    name: $("#workflow-name").value.trim(),
    description: "拼多多售后工单组件化流程",
    nodes,
    edges: nodes.slice(0, -1).map((node, index) => ({ from: node.node_id, to: nodes[index + 1].node_id })),
    enabled: false,
  };
  try {
    const data = await api("/api/workflows", { method: "POST", body: JSON.stringify({ definition }) });
    workflowId = data.workflow.workflow_id;
    $("#save-state").textContent = `已保存 ${workflowId} · 默认未启用`;
  } catch (error) { $("#save-state").textContent = `保存失败：${error.message}`; }
}

function updateCaptureMode() {
  const target = desktopTarget();
  const desktop = Boolean(target);
  const message = Boolean(target?.platform);
  $("#shop").disabled = desktop;
  const roleWrap = $("#capture-role-wrap");
  roleWrap.classList.toggle("hidden", !message);
  if (message) {
    const previousRole = $("#capture-role").value;
    const choices = messageRoles[target.platform] || [];
    $("#capture-role").innerHTML = choices.map((item) => `<option value="${item[0]}">${item[2]}</option>`).join("");
    if (choices.some((item) => item[0] === previousRole)) $("#capture-role").value = previousRole;
    const role = selectedMessageRole();
    $("#capture-type").innerHTML = `<option value="${role[3]}">${role[3]}</option>`;
    $("#capture-type").disabled = true;
  } else {
    $("#capture-type").disabled = false;
    $("#capture-type").innerHTML = desktop
      ? '<option value="ClickDesktopElement">点击桌面元素</option><option value="InputDesktopText">输入桌面文本</option><option value="ReadDesktopText">读取桌面文本</option>'
      : '<option value="ClickElement">点击元素</option><option value="ReadText">读取文本</option>';
  }
  const hint = message
    ? `选择角色后开始；3秒内把鼠标停在${target.label}对应控件上，不需要点击`
    : desktop
      ? "开始后将鼠标停在旺店通目标控件上"
      : "开始后到 Edge 点击元素，再回来读取结果";
  setCaptureState("idle", "未开始", hint);
}

async function init() {
  const [builtins, saved, workflows] = await Promise.all([
    api("/api/components/catalog"), api("/api/components"), api("/api/workflows"),
  ]);
  catalog = [...builtins, ...saved.map((item) => ({
    ...item, category: "已捕获", description: item.locator?.text || item.locator?.selector || "自定义页面元素",
  }))];
  if (workflows.length) {
    const workflow = workflows[0];
    workflowId = workflow.workflow_id;
    nodes = workflow.nodes || [];
    $("#workflow-name").value = workflow.name;
    $("#save-state").textContent = `已加载 ${workflowId} · 默认未启用`;
  }
  renderCatalog();
  renderCanvas();
  renderProperties();
  const canvas = $("#canvas");
  canvas.ondragover = (event) => { event.preventDefault(); canvas.classList.add("drag-over"); };
  canvas.ondragleave = () => canvas.classList.remove("drag-over");
  canvas.ondrop = (event) => { event.preventDefault(); canvas.classList.remove("drag-over"); addNode(event.dataTransfer.getData("text/component-type")); };
  $("#start-capture").onclick = startCapture;
  $("#read-capture").onclick = readCapture;
  $("#cancel-capture").onclick = cancelCapture;
  $("#save-capture").onclick = saveCapture;
  $("#save-workflow").onclick = saveWorkflow;
  $("#capture-application").onchange = updateCaptureMode;
  $("#capture-role").onchange = updateCaptureMode;
  updateCaptureMode();
}

init().catch((error) => setCaptureState("error", "初始化失败", error.message));
