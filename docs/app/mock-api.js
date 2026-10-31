/*
 * Static demo adapter for the real RPA frontend.
 * It only supplies synthetic read-only data to the existing UI. No real
 * business records, credentials or external calls are used.
 */
(function () {
  "use strict";

  const clone = (value) => JSON.parse(JSON.stringify(value));
  const nowIso = () => new Date().toISOString().slice(0, 19);

  const displayStates = {
    DISCOVERED: "待处理",
    COLLECTING_DATA: "读取中",
    WAIT_RULE_MATCH: "规则匹配",
    READY: "可执行",
    NEED_REVIEW: "转人工",
    DATA_CONFLICT: "数据冲突",
    WAIT_APPROVAL: "待审批",
    EXECUTING: "执行中",
    VERIFYING: "核验中",
    RETRY_PENDING: "等待重试",
    COMPLETED: "已处理",
    REJECTED: "已拒绝",
    FAILED: "失败",
    PAUSED: "已暂停",
  };

  const shops = "ABCDEFGHIJK".split("").map((letter, index) => ({
    id: `shop_${String(index + 1).padStart(2, "0")}`,
    name: `示例书店 ${letter}`,
    edge_profile: index === 0 ? "Profile" : `Profile ${index}`,
    enabled: 1,
  }));

  const catalogSpecs = [
    ["EnsurePendingFilter", "勾选待处理筛选", "拼多多", "刷新后检查工单筛选，并确保待处理卡片处于选中状态", true],
    ["RefreshPage", "刷新页面", "浏览器", "刷新已绑定店铺页面，并等待总数与卡片连续两次稳定", false],
    ["ActivateWindow", "激活窗口", "浏览器", "激活指定店铺的 Edge 窗口", false],
    ["AssertShopIdentity", "校验店铺", "安全", "核验 Profile、URL 和页面完整店铺名", false],
    ["GoToPage", "打开页面", "浏览器", "打开白名单内页面", false],
    ["ForEachPage", "遍历分页", "页面", "依次遍历数字分页并恢复原页码", false],
    ["FindWorkOrderCard", "查找工单卡片", "页面", "按订单号和创建时间唯一定位", false],
    ["ClickElement", "点击元素", "页面", "点击唯一且未遮挡的页面元素", true],
    ["ReadText", "读取文本", "页面", "读取元素文本，不修改页面", false],
    ["ReadDetailFields", "读取详情字段", "拼多多", "读取运单号、物流和退款字段", false],
    ["CloseDetailTab", "关闭详情标签", "浏览器", "关闭已绑定的详情页", false],
    ["SaveEvidence", "保存证据", "审计", "保存截图和结构化证据", false],
    ["Condition", "条件判断", "逻辑", "根据规则选择流程分支", false],
    ["ManualApproval", "人工审批", "安全", "暂停并等待人工批准", false],
    ["ActivateDesktopWindow", "激活桌面窗口", "桌面应用", "按进程和窗口标题激活桌面客户端", false],
    ["ClickDesktopElement", "点击桌面元素", "桌面应用", "按捕获的控件特征点击桌面元素", true],
    ["InputDesktopText", "输入桌面文本", "桌面应用", "聚焦捕获的输入框并写入变量文本", true],
    ["ReadDesktopText", "读取桌面文本", "桌面应用", "读取捕获控件的文本或值", false],
    ["ReadDesktopGridRow", "读取桌面表格行", "旺店通", "读取唯一查询结果的订单、平台、状态和物流单号", false],
    ["WaitDesktopState", "等待桌面状态", "桌面应用", "等待加载完成或控件状态变化", false],
    ["EvaluateAllowedSet", "白名单判断", "逻辑", "判断字段是否属于允许值集合", false],
    ["AssertDesktopQueryResult", "校验旺店通结果", "旺店通", "校验唯一结果、平台、原始单号、物流单号和状态白名单", false],
    ["OpenWorkOrderDetail", "打开工单详情", "拼多多", "按订单号唯一定位并打开立即处理详情", false],
    ["WaitBrowserState", "等待页面状态", "拼多多", "等待页面出现指定阶段文本", false],
    ["VerifyRefundSuccess", "验证退款成功", "拼多多", "回读同一订单和工单 ID 的退款成功状态", false],
    ["ReadWdtLogisticsCompany", "读取旺店通物流公司", "旺店通", "查询订单并读取物流公司原始文本", false],
    ["ClassifyFulfillmentRoute", "判断发货路由", "逻辑", "按可配置规则区分平台直发、供应链发货和旺店通代发", false],
    ["QueryJstSupplierByTracking", "供应链查询供应商", "供应链", "按快递单号查询唯一订单并读取供应商", false],
    ["ReadWdtWarehouseLogCompany", "读取选仓公司", "旺店通", "打开订单日志并读取货品选仓后的公司名", false],
    ["ResolveInterceptionGroup", "解析拦截群", "逻辑", "按快递公司或供应商匹配唯一平台、账号和群", false],
    ["SendGroupInterception", "发送拦截消息", "消息平台", "使用真实账号向唯一目标群发送拦截单号", true],
    ["VerifyGroupMessage", "回读群消息", "消息平台", "回读群聊并核验账号、群名、单号和发送时间", false],
    ["ReadLatestPddLogistics", "读取最新物流", "拼多多", "回到工单详情读取最新一条物流轨迹", false],
    ["MatchLocationWhitelist", "匹配退款地点", "逻辑", "按快递公司匹配可配置地点关键词白名单", false],
    ["RejectRefundFixedReason", "固定原因拒绝退款", "拼多多", "选择全局固定的拒绝原因并提交", true],
    ["AgreeRefund", "同意退款", "拼多多", "经证据和地点白名单校验后提交同意退款", true],
  ];

  const catalog = catalogSpecs.map(([component_type, name, category, description, write_capable]) => ({
    component_type,
    name,
    category,
    description,
    write_capable,
  }));

  const savedComponents = [
    {
      component_id: "CMP-MSG-WECOM-SEARCH-INPUT",
      component_type: "InputDesktopText",
      name: "搜索框（输入群名）",
      application: "企业微信",
      locator: { window: { title: "企业微信" }, control_type: "Edit", name: "搜索", relative_point: { x: 0.18, y: 0.09 } },
      config: { value_variable: "group_name" },
      on_failure: "MANUAL_REVIEW",
      enabled: true,
      version: 1,
      write_capable: true,
    },
    {
      component_id: "CMP-MSG-WECOM-SEARCH-RESULT",
      component_type: "ClickDesktopElement",
      name: "搜索结果第一项（点击群）",
      application: "企业微信",
      locator: { window: { title: "企业微信" }, name: "示例供应商 A", relative_point: { x: 0.2, y: 0.18 } },
      config: {},
      on_failure: "MANUAL_REVIEW",
      enabled: true,
      version: 1,
      write_capable: true,
    },
    {
      component_id: "CMP-MSG-WECOM-GROUP-HEADER",
      component_type: "ReadDesktopText",
      name: "群聊标题（回读群名）",
      application: "企业微信",
      locator: { window: { title: "企业微信" }, name: "示例供应商 A", relative_point: { x: 0.45, y: 0.05 } },
      config: { output_variable: "group_header" },
      on_failure: "MANUAL_REVIEW",
      enabled: true,
      version: 1,
      write_capable: false,
    },
    {
      component_id: "CMP-MSG-WECOM-MESSAGE-INPUT",
      component_type: "InputDesktopText",
      name: "消息输入框（输入单号+拦截）",
      application: "企业微信",
      locator: { window: { title: "企业微信" }, control_type: "Edit", relative_point: { x: 0.6, y: 0.9 } },
      config: { value_variable: "message" },
      on_failure: "MANUAL_REVIEW",
      enabled: true,
      version: 1,
      write_capable: true,
    },
    {
      component_id: "CMP-MSG-WECOM-SEND",
      component_type: "ClickDesktopElement",
      name: "发送按钮",
      application: "企业微信",
      locator: { window: { title: "企业微信" }, name: "发送(S)", relative_point: { x: 0.86, y: 0.92 } },
      config: {},
      on_failure: "MANUAL_REVIEW",
      enabled: true,
      version: 1,
      write_capable: true,
    },
  ];

  const workflow = {
    workflow_id: "WF-PDD-DETAIL-READONLY",
    name: "拼多多工单详情只读采集",
    description: "唯一定位工单，点击立即处理后读取快递单号并关闭详情页",
    enabled: false,
    version: 1,
    nodes: [
      { node_id: "activate", component_type: "ActivateWindow", name: "激活店铺窗口" },
      { node_id: "identity", component_type: "AssertShopIdentity", name: "校验店铺身份" },
      { node_id: "find", component_type: "FindWorkOrderCard", name: "按订单号查找工单卡片" },
      { node_id: "click", component_type: "ClickElement", name: "点击立即处理", locator: { text: "立即处理", anchor_text: "{{order_no}}" } },
      { node_id: "read", component_type: "ReadDetailFields", name: "读取快递单号" },
      { node_id: "evidence", component_type: "SaveEvidence", name: "保存详情证据" },
      { node_id: "close", component_type: "CloseDetailTab", name: "关闭详情标签" },
    ],
    edges: [
      { from: "activate", to: "identity" },
      { from: "identity", to: "find" },
      { from: "find", to: "click" },
      { from: "click", to: "read" },
      { from: "read", to: "evidence" },
      { from: "evidence", to: "close" },
    ],
  };

  const workOrderFacts = (index, overrides = {}) => {
    const shop = shops[index - 1];
    return {
      shop_id: shop.id,
      shop_name: shop.name,
      edge_profile: shop.edge_profile,
      work_order_no: `WG-DEMO-000${index}`,
      order_no: `DEMO-2601-000${index}`,
      work_order_title: "已发货无轨迹退款处理",
      refund_type: "仅退款",
      refund_reason: "不想要了",
      refund_amount: 39.9,
      pdd_order_status: "已发货",
      pdd_logistics_status: "NO_INFO",
      carrier: "中通快递",
      tracking_no: `DEMOZT000000000${index}`,
      jst_outbound_status: "已出库",
      wdt_result_count: 1,
      wdt_order_id: `DEMO-WDT-500${index}`,
      wdt_platform: "拼多多",
      wdt_order_status: "已发货",
      wdt_warehouse: "示例自营仓",
      wdt_logistics_company: "中通-拼多多",
      wdt_product_names: ["示例品类 A"],
      wdt_eligible: true,
      carrier_tracking_status: "UNKNOWN",
      latest_logistics_text: "快件已发往【示例城市甲市】",
      work_order_deadline: "2026-11-02T18:00:00",
      buyer_description: "不想要了，麻烦拦截",
      screenshots: [],
      source_evidence: {
        collection_mode: "edge_uia",
        source_system: "pdd_work_order",
        scan_order: index,
        page_no: 1,
        page_index: index - 1,
        sync_started_at: "2026-10-31T10:37:00",
        interception_route: {
          route_type: "DIRECT_CARRIER",
          rule_id: "DIRECT-PDD",
          message: `DEMOZT000000000${index}+拦截`,
          group: {
            carrier: "中通快递",
            platform: "wecom",
            account_alias: "企业微信",
            group_name: "【快递】示例团队&中通对接群",
            message_mode: "group_chat",
          },
        },
        interception_message: {
          platform: "wecom",
          group_name: "【快递】示例团队&中通对接群",
          sent_echo: "",
        },
      },
      ...overrides,
    };
  };

  const tasks = [
    {
      id: 1001,
      business_key: "shop_01:DEMO-2601-0001:WG-DEMO-0001",
      shop_id: "shop_01",
      order_no: "DEMO-2601-0001",
      work_order_no: "WG-DEMO-0001",
      state: "DISCOVERED",
      risk_level: "unknown",
      matched_rule_id: null,
      decision: null,
      version: 1,
      completed_at: null,
      created_at: "2026-10-31T10:28:00",
      updated_at: "2026-10-31T10:38:00",
      task_kind: "work_order",
      task_source_label: "工单管理",
      queue_scope: "",
      current_sync: true,
      live_pending: true,
      source_stale: false,
      source_sync_error: "",
      sync_run_id: "RUN-DEMO-1",
      sync_finished_at: "2026-10-31T10:38:00",
      display_state: "待处理",
      workflow_key: "shipped_no_trace",
      workflow_enabled: true,
      queue_priority: 100,
      queue_rank: 1,
      evidence: [],
      facts: workOrderFacts(1),
    },
    {
      id: 1002,
      business_key: "shop_02:DEMO-2601-0002:WG-DEMO-0002",
      shop_id: "shop_02",
      order_no: "DEMO-2601-0002",
      work_order_no: "WG-DEMO-0002",
      state: "NEED_REVIEW",
      risk_level: "medium",
      matched_rule_id: "R-INTRANSIT-001",
      decision: { matched: true, rule_id: "R-INTRANSIT-001" },
      version: 2,
      completed_at: null,
      created_at: "2026-10-31T10:29:00",
      updated_at: "2026-10-31T10:38:00",
      task_kind: "work_order",
      task_source_label: "工单管理",
      queue_scope: "",
      current_sync: true,
      live_pending: true,
      source_stale: false,
      source_sync_error: "",
      sync_run_id: "RUN-DEMO-1",
      sync_finished_at: "2026-10-31T10:38:00",
      display_state: "转人工",
      workflow_key: "in_transit_no_reason",
      workflow_enabled: true,
      queue_priority: 50,
      queue_rank: 2,
      evidence: [],
      facts: workOrderFacts(2, {
        work_order_title: "在途无理由退款处理",
        refund_amount: 49.8,
        pdd_logistics_status: "IN_TRANSIT",
        latest_logistics_text: "快件已到达示例城市甲市分拨中心，派送中",
        carrier_tracking_status: "IN_TRANSIT",
        source_evidence: {
          collection_mode: "edge_uia",
          source_system: "pdd_work_order",
          scan_order: 2,
          page_no: 1,
          page_index: 1,
          interception_route: {
            route_type: "DIRECT_CARRIER",
            rule_id: "ID-DEMO-TRANSIT",
            message: "DEMOZT0000000002+拦截",
            group: {
              carrier: "中通快递",
              platform: "wecom",
              account_alias: "企业微信",
              group_name: "【快递】示例团队&中通对接群",
              message_mode: "group_chat",
            },
          },
          interception_message: { platform: "wecom", group_name: "【快递】示例团队&中通对接群", sent_echo: "" },
          aftersale_retry: { retry_not_before: "", prompt_text: "", attempts: 0 },
        },
      }),
    },
    {
      id: 1003,
      business_key: "shop_03:DEMO-2601-0003:DEMO-AFTERSALE-0003",
      shop_id: "shop_03",
      order_no: "DEMO-2601-0003",
      work_order_no: "DEMO-AFTERSALE-0003",
      state: "DISCOVERED",
      risk_level: "unknown",
      matched_rule_id: null,
      decision: null,
      version: 1,
      completed_at: null,
      created_at: "2026-10-31T10:30:00",
      updated_at: "2026-10-31T10:38:00",
      task_kind: "aftersale",
      task_source_label: "售后工作台",
      queue_scope: "pending_soon_overdue",
      current_sync: true,
      live_pending: true,
      source_stale: false,
      source_sync_error: "",
      sync_run_id: "RUN-DEMO-1",
      sync_finished_at: "2026-10-31T10:38:00",
      display_state: "待处理",
      workflow_key: "aftersale_return_refund",
      workflow_enabled: true,
      queue_priority: 10,
      queue_rank: 3,
      evidence: [],
      facts: {
        shop_id: "shop_03",
        shop_name: "示例书店 C",
        edge_profile: "Profile 2",
        work_order_no: "DEMO-AFTERSALE-0003",
        work_order_title: "售后工作台：退货退款",
        order_no: "DEMO-2601-0003",
        refund_type: "退货退款",
        refund_reason: "尺码不合适",
        refund_amount: 68.0,
        pdd_order_status: "已发货",
        shipping_status: "已发货",
        aftersale_status: "待处理即将逾期",
        aftersale_remaining_text: "1天2时30分",
        aftersale_remaining_seconds: 95400,
        carrier: "圆通快递",
        tracking_no: "DEMOYT0000000003",
        return_carrier: "圆通快递",
        return_tracking_no: "DEMOYT0000000003",
        logistics_trajectory_count: 4,
        aftersale_decision: { action: "EXTEND_RECEIPT", reason: "退货退款且物流未签收，延长收货" },
        aftersale_business_write_performed: false,
        source_evidence: {
          collection_mode: "edge_uia",
          source_system: "aftersale_workbench",
          queue_scope: "pending_soon_overdue",
          scan_order: 3,
          page_no: 1,
          page_index: 2,
          aftersale_card: {
            status: "待处理即将逾期",
            urgency_text: "1天2时30分",
            refund_reason: "尺码不合适",
            aftersale_no: "DEMO-AFTERSALE-0003",
            aftersale_apply_time: "2026-10-29 09:12",
            description: "买家申请退货退款",
            shipping_status: "已发货",
            return_carrier: "圆通快递",
            return_tracking_no: "DEMOYT0000000003",
            page_no: 1,
            page_index: 2,
          },
          aftersale_retry: { retry_not_before: "", prompt_text: "", attempts: 0 },
          aftersale_detail: { decision: { action: "EXTEND_RECEIPT", reason: "退货退款且物流未签收，延长收货" } },
        },
      },
    },
    {
      id: 1004,
      business_key: "qianniu_01:DEMO-TMALL-0004:DEMO-TM-AFTERSALE-0004",
      shop_id: "qianniu_01",
      order_no: "DEMO-TMALL-0004",
      work_order_no: "DEMO-TM-AFTERSALE-0004",
      state: "NEED_REVIEW",
      risk_level: "medium",
      matched_rule_id: null,
      decision: null,
      version: 1,
      completed_at: null,
      created_at: "2026-10-31T10:31:00",
      updated_at: "2026-10-31T10:38:00",
      task_kind: "qianniu",
      task_source_label: "千牛未收货退款",
      queue_scope: "unreceived_refund_pending",
      current_sync: true,
      live_pending: true,
      source_stale: false,
      source_sync_error: "",
      sync_run_id: "RUN-DEMO-1",
      sync_finished_at: "2026-10-31T10:38:00",
      display_state: "转人工",
      workflow_key: "qianniu_unreceived_refund",
      workflow_enabled: true,
      queue_priority: 0,
      queue_rank: 4,
      evidence: [],
      facts: {
        shop_id: "qianniu_01",
        shop_name: "示例天猫旗舰店",
        edge_profile: "Default",
        work_order_no: "DEMO-TM-AFTERSALE-0004",
        work_order_title: "千牛未收货退款",
        order_no: "DEMO-TMALL-0004",
        refund_type: "未收货退款",
        refund_reason: "不想要了",
        refund_amount: 45.94,
        pdd_order_status: "已发货",
        carrier: "圆通速递",
        tracking_no: "DEMOYT0000000004",
        logistics_trajectory_count: 5,
        latest_logistics_text: "快件已到达【示例城市乙市】",
        wdt_result_count: 1,
        wdt_order_id: "DEMO-WDT-5004",
        wdt_order_status: "已发货",
        wdt_warehouse: "示例自营仓",
        wdt_logistics_company: "圆通-天猫",
        wdt_product_names: ["示例品类 B"],
        source_evidence: {
          collection_mode: "edge_uia",
          source_system: "qianniu_aftersale",
          platform: "tmall",
          queue_scope: "unreceived_refund_pending",
          aftersale_no: "DEMO-TM-AFTERSALE-0004",
          aftersale_status: "退款待处理",
          available_actions: ["同意退款", "拦截快递", "查看详情"],
          automation_eligible: true,
          shop_scan_order: 11,
          page_no: 1,
          page_index: 3,
          qianniu_decision: {
            action: "intercept_express",
            reason: "≤5 条轨迹且均为在途，先拦截快递再同意退款",
            requires_group_interception: true,
            requires_dingtalk_registration: true,
          },
          qianniu_logistics_trajectory: { logistics_trajectory_count: 5 },
          qianniu_platform_feedback: { verified: false, terminal: "", terminal_feedback: "" },
          interception_route: {
            group: { platform: "yto", account_alias: "圆通专属客服", group_name: "示例客户服务群" },
            message: "DEMOYT0000000004+拦截",
          },
          interception_message: { platform: "yto", group_name: "示例客户服务群", sent_echo: "" },
        },
      },
    },
    {
      id: 1005,
      business_key: "shop_04:DEMO-2601-0005:WG-DEMO-0005",
      shop_id: "shop_04",
      order_no: "DEMO-2601-0005",
      work_order_no: "WG-DEMO-0005",
      state: "DISCOVERED",
      risk_level: "unknown",
      matched_rule_id: null,
      decision: null,
      version: 1,
      completed_at: null,
      created_at: "2026-10-31T10:32:00",
      updated_at: "2026-10-31T10:38:00",
      task_kind: "work_order",
      task_source_label: "工单管理",
      queue_scope: "",
      current_sync: true,
      live_pending: true,
      source_stale: false,
      source_sync_error: "",
      sync_run_id: "RUN-DEMO-1",
      sync_finished_at: "2026-10-31T10:38:00",
      display_state: "待处理",
      workflow_key: "confirm_earlier_shipping",
      workflow_enabled: true,
      queue_priority: 40,
      queue_rank: 5,
      evidence: [],
      facts: workOrderFacts(4, {
        work_order_title: "确认更早发货时间",
        refund_amount: 88.0,
        pdd_order_status: "待发货",
        carrier: "",
        tracking_no: "",
        wdt_order_status: "待发货",
        wdt_warehouse: "示例供应链代发总仓",
        wdt_logistics_company: "",
        latest_logistics_text: "",
      }),
    },
    {
      id: 1006,
      business_key: "shop_05:DEMO-2601-0006:WG-DEMO-0006",
      shop_id: "shop_05",
      order_no: "DEMO-2601-0006",
      work_order_no: "WG-DEMO-0006",
      state: "COMPLETED",
      risk_level: "low",
      matched_rule_id: "R-NOTRACE-001",
      decision: { matched: true, rule_id: "R-NOTRACE-001" },
      version: 3,
      completed_at: "2026-10-30T15:00:00",
      created_at: "2026-10-30T14:20:00",
      updated_at: "2026-10-30T15:00:00",
      task_kind: "work_order",
      task_source_label: "工单管理",
      queue_scope: "",
      current_sync: false,
      live_pending: false,
      source_stale: false,
      source_sync_error: "",
      sync_run_id: "RUN-DEMO-0",
      sync_finished_at: "2026-10-30T15:00:00",
      display_state: "已处理",
      workflow_key: "shipped_no_trace",
      workflow_enabled: true,
      queue_priority: 100,
      queue_rank: 6,
      evidence: [],
      facts: workOrderFacts(5, {
        work_order_no: "WG-DEMO-0006",
        order_no: "DEMO-2601-0006",
        refund_amount: 59.0,
        tracking_no: "DEMOZT0000000006",
        refund_agreed: true,
        real_refund_completed: true,
        final_action: "AGREE_REFUND",
        source_evidence: {
          collection_mode: "edge_uia",
          source_system: "pdd_work_order",
          scan_order: 5,
          page_no: 1,
          page_index: 4,
          interception_message: {
            group_name: "【快递】示例团队&中通对接群",
            platform: "wecom",
            sent_echo: "DEMOZT0000000006+拦截 15:00",
          },
          interception_route: {
            route_type: "DIRECT_CARRIER",
            group: { platform: "wecom", account_alias: "企业微信", group_name: "【快递】示例团队&中通对接群" },
          },
        },
      }),
    },
  ];

  const logsByTask = {
    1001: [
      { id: 9001, task_id: 1001, action_key: "DEMO:1001:1", action_type: "DISCOVER_PDD_READONLY", status: "success", details: { reason: "列表扫描命中待处理卡片" }, created_at: "2026-10-31T10:31:00" },
      { id: 9002, task_id: 1001, action_key: "DEMO:1001:2", action_type: "RUN_WDT_COMPONENT_WORKFLOW", status: "success", details: { reason: "唯一结果，平台与单号一致" }, created_at: "2026-10-31T10:33:00" },
      { id: 9003, task_id: 1001, action_key: "DEMO:1001:3", action_type: "INTERCEPT_MESSAGE_DISPATCHED", status: "success", details: { group: { group_name: "【快递】示例团队&中通对接群" }, tracking_no: "DEMOZT0000000001" }, created_at: "2026-10-31T10:34:00" },
    ],
    1002: [
      { id: 9011, task_id: 1002, action_key: "DEMO:1002:1", action_type: "DISCOVER_PDD_READONLY", status: "success", details: { reason: "命中在途无理由候选" }, created_at: "2026-10-31T10:29:00" },
      { id: 9012, task_id: 1002, action_key: "DEMO:1002:2", action_type: "STATE_STATUS_UPDATE", status: "success", details: { from: "WAIT_RULE_MATCH", to: "NEED_REVIEW", reason: "地点/状态需要人工复核" }, created_at: "2026-10-31T10:35:00" },
    ],
    1003: [
      { id: 9021, task_id: 1003, action_key: "DEMO:1003:1", action_type: "DISCOVER_AFTERSALE_READONLY", status: "success", details: { reason: "待处理即将逾期列表采集" }, created_at: "2026-10-31T10:30:00" },
      { id: 9022, task_id: 1003, action_key: "DEMO:1003:2", action_type: "COLLECT_AFTERSALE_DETAIL", status: "success", details: { reason: "详情、物流和可用按钮已读取" }, created_at: "2026-10-31T10:36:00" },
    ],
    1004: [
      { id: 9031, task_id: 1004, action_key: "DEMO:1004:1", action_type: "QIANNIU_SCAN", status: "success", details: { reason: "千牛未收货退款扫描" }, created_at: "2026-10-31T10:37:00" },
      { id: 9032, task_id: 1004, action_key: "DEMO:1004:2", action_type: "STATE_STATUS_UPDATE", status: "success", details: { from: "DISCOVERED", to: "NEED_REVIEW", reason: "等待人工确认拦截群" }, created_at: "2026-10-31T10:38:00" },
    ],
    1006: [
      { id: 9041, task_id: 1006, action_key: "DEMO:1006:1", action_type: "DISCOVER_PDD_READONLY", status: "success", details: { reason: "历史队列扫描命中" }, created_at: "2026-10-30T14:20:00" },
      { id: 9042, task_id: 1006, action_key: "DEMO:1006:2", action_type: "INTERCEPT_MESSAGE_DISPATCHED", status: "success", details: { tracking_no: "DEMOZT0000000006" }, created_at: "2026-10-30T14:58:00" },
      { id: 9043, task_id: 1006, action_key: "DEMO:1006:3", action_type: "EXECUTE_PDD_REAL_REFUND", status: "success", details: { action: "AGREE_REFUND", verified: true }, created_at: "2026-10-30T14:59:00" },
      { id: 9044, task_id: 1006, action_key: "DEMO:1006:4", action_type: "STATE_TRANSITION", status: "success", details: { from: "VERIFYING", to: "COMPLETED", reason: "退款结果已回读" }, created_at: "2026-10-30T15:00:00" },
    ],
  };

  const dashboard = {
    paused: false,
    task_count: 6,
    states: { DISCOVERED: 3, NEED_REVIEW: 2, COMPLETED: 1 },
    shops,
    rules: { total: 4, enabled: 2 },
    sources: { live: 5 },
    targets: {},
    current_task_count: 5,
    current_work_order_count: 3,
    current_aftersale_count: 1,
    current_qianniu_count: 1,
    history_task_count: 1,
    current_targets: {},
    shop_current_counts: { shop_01: 1, shop_02: 1, shop_03: 1, shop_04: 1, qianniu_01: 1 },
    sync: { run_id: "RUN-DEMO-1", finished_at: "2026-10-31T10:38:00" },
    components: 5,
    workflows: 1,
    execution_mode: "observe",
    live_actions_enabled: false,
    pdd_readonly: {},
    qianniu_shop: {
      id: "qianniu_01",
      name: "示例天猫旗舰店",
      edge_profile: "Default",
      platform: "tmall",
      enabled: true,
      workflow_enabled: false,
      production_enabled: false,
      scan_position: 12,
    },
  };

  const readyWorkflow = () => ({ dry_run: { ready: true, reasons: [] }, production: { ready: true, reasons: [] } });
  const runtimeStatus = {
    run_id: "RUN-DEMO-1",
    state: "STOPPED",
    mode: "production",
    phase: "静态演示 · 未执行真实动作",
    current_task: null,
    queue: [],
    results: [
      { task_id: 1006, shop_name: "示例书店 E", order_no: "DEMO-2601-0006", status: "success", error: "", steps: [{ step: "读取工单详情" }, { step: "旺店通核验" }, { step: "同意退款" }, { step: "结果回读" }] },
      { task_id: 1005, shop_name: "示例书店 D", order_no: "DEMO-2601-0005", status: "skipped", error: "等待确认更早发货时间", steps: [] },
      { task_id: 1003, shop_name: "示例书店 C", order_no: "DEMO-2601-0003", status: "success", error: "", steps: [{ step: "读取售后详情" }, { step: "延长收货" }] },
    ],
    counts: { total: 5, completed: 3, failed: 0, skipped: 2 },
    started_at: "2026-10-31T10:30:00",
    finished_at: "2026-10-31T10:42:00",
    last_error: "",
    continuous: false,
    queue_only: false,
    queue_snapshot_run_id: "RUN-DEMO-1",
    queue_task_ids: [1001, 1002, 1005, 1003, 1004],
    cycle: 1,
    poll_interval_seconds: 60,
    next_sync_at: "",
    last_sync: {},
    workflow_keys: [
      "shipped_no_trace",
      "in_transit_no_reason",
      "confirm_earlier_shipping",
      "aftersale_return_refund",
      "aftersale_shipped_refund",
    ],
    recovery_pending: false,
    recovery_previous_state: "",
    recovery_attempts: 0,
    recovery_of: "",
    recovery_active: false,
    readiness: {
      dry_run: { ready: true, reasons: [] },
      production: { ready: true, reasons: [] },
      supported_workflows: [
        "shipped_no_trace",
        "in_transit_no_reason",
        "confirm_earlier_shipping",
        "aftersale_return_refund",
        "aftersale_shipped_refund",
        "qianniu_unreceived_refund",
      ],
      pending_workflows: [],
      workflows: {
        shipped_no_trace: readyWorkflow(),
        in_transit_no_reason: readyWorkflow(),
        confirm_earlier_shipping: readyWorkflow(),
        aftersale_return_refund: readyWorkflow(),
        aftersale_shipped_refund: readyWorkflow(),
        qianniu_unreceived_refund: readyWorkflow(),
      },
    },
  };

  const registers = [
    {
      id: 501,
      task_id: 1006,
      record_date: "2026-10-30",
      carrier: "中通快递",
      tracking_no: "DEMOZT0000000006",
      order_no: "DEMO-2601-0006",
      refund_reason: "拦截后退款",
      amount: 59.0,
      warehouse: "示例自营仓",
      group_name: "【快递】示例团队&中通对接群",
      dingtalk_target_key: "junfu",
      created_at: "2026-10-30T15:00:00",
      updated_at: "2026-10-30T15:00:00",
      shop_id: "shop_05",
      shop_name: "示例书店 E",
      platform: "pdd",
      platform_label: "拼多多",
      dingtalk_sync_status: "pending",
      dingtalk_remote_row: null,
      dingtalk_duplicate_tracking: 0,
      dingtalk_last_error: null,
      dingtalk_synced_at: null,
    },
    {
      id: 502,
      task_id: 1006,
      record_date: "2026-10-29",
      carrier: "圆通速递",
      tracking_no: "DEMOYT0000000007",
      order_no: "DEMO-2601-0007",
      refund_reason: "拦截后退款",
      amount: 42.0,
      warehouse: "示例供应链代发总仓",
      group_name: "示例供应商 C",
      dingtalk_target_key: "siku_distribution",
      created_at: "2026-10-29T16:20:00",
      updated_at: "2026-10-29T16:20:00",
      shop_id: "shop_03",
      shop_name: "示例书店 C",
      platform: "pdd",
      platform_label: "拼多多",
      dingtalk_sync_status: "succeeded",
      dingtalk_remote_row: 108,
      dingtalk_duplicate_tracking: 0,
      dingtalk_last_error: null,
      dingtalk_synced_at: "2026-10-29T16:22:00",
    },
  ];

  const priorities = {
    version: 5,
    workflows: [
      { workflow_key: "shipped_no_trace", title: "已发货无轨迹退款处理", priority: 100, enabled: true },
      { workflow_key: "in_transit_no_reason", title: "在途无理由退款处理", priority: 50, enabled: true },
      { workflow_key: "confirm_earlier_shipping", title: "确认更早发货时间", priority: 40, enabled: true },
      { workflow_key: "aftersale_return_refund", title: "售后工作台：退货退款", priority: 10, enabled: true },
      { workflow_key: "aftersale_shipped_refund", title: "售后工作台：已发货-退款", priority: 9, enabled: true },
    ],
  };

  const routing = {
    schema_version: 13,
    version: 13,
    message_template: "{tracking_no}+拦截",
    fixed_reject: {
      option_text: "消费者撤销售后申请",
      explanation_template: "已联系拦截，当前物流状态不满足退款条件",
    },
    jst_product_warehouse: "示例供应链代发总仓",
    supported_warehouses: ["示例自营仓", "示例供应链代发总仓"],
    supported_platforms: ["qq", "wechat", "wecom", "yto"],
    latest_logistics_phrase: "快件已发往",
    product_groups: [
      { keyword: "示例品类 A", warehouse: "示例供应链代发总仓", platform: "wecom", account_alias: "企业微信", group_name: "示例供应商 A", message_mode: "group_chat", enabled: true },
      { keyword: "示例品类 B", warehouse: "示例供应链代发总仓", platform: "wecom", account_alias: "企业微信", group_name: "示例供应商 B", message_mode: "group_chat", enabled: true },
      { keyword: "示例品类 C", warehouse: "示例供应链代发总仓", platform: "wecom", account_alias: "企业微信", group_name: "示例供应商 C", message_mode: "group_chat", enabled: true },
      { keyword: "示例品类 E", warehouse: "示例供应链代发总仓", platform: "wecom", account_alias: "企业微信", group_name: "【分销】示例供应商 E 沟通群", message_mode: "group_chat", enabled: true },
      { keyword: "示例品类 F", warehouse: "示例供应链代发总仓", platform: "qq", account_alias: "QQ", group_name: "示例供应商 F 分销群", message_mode: "group_chat", enabled: true },
      { keyword: "示例品类 G", warehouse: "示例供应链代发总仓", platform: "wecom", account_alias: "企业微信", group_name: "【分销】示例供应商 G 对接群", message_mode: "group_chat", enabled: false },
    ],
    carrier_groups: [
      { carrier: "圆通快递", aliases: ["圆通", "圆通速递"], platform: "yto", account_alias: "圆通专属客服", group_name: "示例客户服务群", message_mode: "service_chat", enabled: true },
      { carrier: "中通快递", aliases: ["中通", "中通速递"], platform: "wecom", account_alias: "企业微信", group_name: "【快递】示例团队&中通对接群", message_mode: "group_chat", enabled: true },
      { carrier: "顺丰快递", aliases: ["顺丰", "顺丰速运"], platform: "wecom", account_alias: "企业微信", group_name: "示例顺丰服务群", message_mode: "group_chat", enabled: true },
      { carrier: "韵达快递", aliases: ["韵达", "韵达速递"], platform: "wechat", account_alias: "微信", group_name: "示例韵达沟通群", message_mode: "group_chat", enabled: true },
    ],
    supplier_groups: [],
    location_whitelists: [
      { carrier: "*", aliases: ["圆通", "中通", "顺丰", "韵达", "圆通快递", "中通快递", "顺丰快递", "韵达快递"], keywords: ["示例城市甲市", "示例城市乙市"], enabled: true },
    ],
    classification_rules: [
      { rule_id: "DIRECT-PDD", route_type: "DIRECT_CARRIER", match_mode: "regex", pattern: "[-－—一]拼多多\\d*$", priority: 300, enabled: true },
      { rule_id: "DIRECT-TMALL", route_type: "DIRECT_CARRIER", match_mode: "regex", pattern: "[-－—一]天猫\\d*$", priority: 290, enabled: true },
      { rule_id: "JST-FOUR-WAREHOUSE", route_type: "JST_SUPPLIER", match_mode: "contains", pattern: "示例供应链发货", priority: 200, enabled: true },
      { rule_id: "WDT-DROPSHIP", route_type: "WDT_DROPSHIP", match_mode: "regex", pattern: "代发$", priority: 100, enabled: true },
    ],
  };

  const messageConfig = {
    version: 5,
    enabled: false,
    platforms: {
      wecom: {
        enabled: true, application: "企业微信",
        roles: {
          search_input: "CMP-MSG-WECOM-SEARCH-INPUT",
          search_result: "CMP-MSG-WECOM-SEARCH-RESULT",
          group_header: "CMP-MSG-WECOM-GROUP-HEADER",
          message_input: "CMP-MSG-WECOM-MESSAGE-INPUT",
          send_button: "CMP-MSG-WECOM-SEND",
        },
        after_search_wait_seconds: 0.6, after_open_wait_seconds: 0.6, after_send_wait_seconds: 1.5,
      },
      wechat: {
        enabled: true, application: "微信",
        roles: {
          search_input: "CMP-MSG-WECHAT-SEARCH-INPUT",
          group_header: "CMP-MSG-WECHAT-GROUP-HEADER",
          message_input: "CMP-MSG-WECHAT-MESSAGE-INPUT",
          send_button: "CMP-MSG-WECHAT-SEND",
        },
      },
      qq: {
        enabled: true, application: "QQ",
        roles: {
          search_input: "CMP-MSG-QQ-SEARCH-INPUT",
          search_result: "CMP-MSG-QQ-SEARCH-RESULT",
          group_header: "CMP-MSG-QQ-GROUP-HEADER",
          message_input: "CMP-MSG-QQ-MESSAGE-INPUT",
          send_button: "CMP-MSG-QQ-SEND",
        },
      },
      yto: {
        enabled: true, application: "圆通客户管家", transport: "playwright",
        url: "https://example.com/carrier-console",
        expected_group_name: "示例客户服务群", roles: {},
      },
    },
  };

  const readiness = {
    ready: true,
    required_platforms: ["qq", "wechat", "wecom", "yto"],
    platforms: {
      wecom: { ready: true, platform: "wecom", application: "企业微信", required_roles: ["search_input", "search_result", "group_header", "message_input", "send_button"], reasons: [] },
      wechat: { ready: true, platform: "wechat", application: "微信", required_roles: ["search_input", "group_header", "message_input", "send_button"], reasons: [] },
      qq: { ready: true, platform: "qq", application: "QQ", required_roles: ["search_input", "search_result", "group_header", "message_input", "send_button"], reasons: [] },
      yto: { ready: true, platform: "yto", application: "圆通客户管家", required_roles: [], reasons: [] },
    },
    reasons: [],
  };

  const state = {
    tasks,
    logsByTask,
    runtimeStatus,
    registers,
    priorities,
    routing,
    messageConfig,
    components: savedComponents,
    workflow: workflow,
  };

  let componentSequence = 20;

  function taskById(id) {
    return state.tasks.find((task) => Number(task.id) === Number(id));
  }

  function pendingTasks() {
    return state.tasks
      .filter((task) => task.live_pending && !["COMPLETED", "REJECTED"].includes(task.state))
      .sort((left, right) => Number(left.queue_rank || 999) - Number(right.queue_rank || 999));
  }

  function processedTasks() {
    return state.tasks.filter((task) => ["COMPLETED", "REJECTED"].includes(task.state));
  }

  function displayFor(taskState) {
    return displayStates[taskState] || taskState;
  }

  function updateRuntimeState(nextState, phase, extras = {}) {
    state.runtimeStatus = {
      ...state.runtimeStatus,
      ...extras,
      state: nextState,
      phase,
      finished_at: ["COMPLETED", "STOPPED", "FAILED"].includes(nextState) ? nowIso() : state.runtimeStatus.finished_at,
    };
    return state.runtimeStatus;
  }

  function updateTaskStatus(id, body) {
    const task = taskById(id);
    if (!task) return { detail: "任务不存在" };
    const target = body.state || "DISCOVERED";
    task.state = target;
    task.display_state = displayFor(target);
    task.updated_at = nowIso();
    task.completed_at = target === "COMPLETED" ? nowIso() : null;
    task.live_pending = !["COMPLETED", "REJECTED"].includes(target);
    return { ok: true, task: clone(task) };
  }

  function previewRouting(inputs) {
    const input = inputs || {};
    const warehouse = String(input.wdt_warehouse || "").trim();
    const tracking = String(input.tracking_no || "DEMO0000000000000");
    const carrier = String(input.carrier || input.wdt_logistics_company || "中通快递");
    const names = Array.isArray(input.wdt_product_names) ? input.wdt_product_names.map(String) : [];
    const fixedReject = state.routing.fixed_reject || {};
    if (warehouse.includes("示例供应链")) {
      const match = state.routing.product_groups.find((item) => names.some((name) => name.includes(item.keyword)));
      if (!match) {
        return {
          ok: false,
          manual_review: true,
          reason_code: "WDT_PRODUCT_NAME_UNMAPPED",
          reason: `示例供应链代发总仓货品名称未匹配拦截群：${JSON.stringify(names)}`,
          wdt_warehouse: warehouse,
          wdt_product_names: names,
        };
      }
      const group = clone(match);
      return {
        ok: true,
        manual_review: false,
        wdt_warehouse: warehouse,
        wdt_warehouse_raw: warehouse,
        wdt_warehouse_read_source: "exact_cell",
        route_type: "WDT_PRODUCT_GROUP",
        rule_id: "WDT-PRODUCT-KEYWORD",
        wdt_product_names: names,
        matched_product_keyword: match.keyword,
        tracking_no: tracking,
        carrier,
        group,
        message: `${tracking}+拦截`,
        evidence_source: "WDT_GOODS_LIST",
        matched_location: "",
        location_phrase: "",
        destination_text: "",
        final_action: "REJECT_REFUND",
        force_reject_after_interception: true,
        fixed_reject: fixedReject,
        dispatch: {
          ok: true,
          duplicate: false,
          duplicate_check_performed: true,
          action_key: `INTERCEPT_DISPATCHED:${tracking}`,
          group: group,
          tracking_no: tracking,
          message: `${tracking}+拦截`,
          previous: null,
        },
      };
    }
    const group = clone(state.routing.carrier_groups.find((item) => carrier.includes(item.carrier.replace("快递", ""))) || state.routing.carrier_groups[1]);
    return {
      ok: true,
      manual_review: false,
      wdt_warehouse: warehouse,
      wdt_warehouse_raw: warehouse,
      wdt_warehouse_read_source: "exact_cell",
      route_type: "DIRECT_CARRIER",
      rule_id: "DIRECT-PDD",
      logistics_company: carrier,
      tracking_no: tracking,
      carrier,
      group,
      message: `${tracking}+拦截`,
      evidence_source: "PDD_LATEST_LOGISTICS",
      matched_location: "示例城市甲市",
      location_phrase: "快件已发往",
      destination_text: "示例城市甲市",
      final_action: "AGREE_REFUND",
      force_reject_after_interception: false,
      fixed_reject: fixedReject,
      dispatch: {
        ok: true,
        duplicate: false,
        duplicate_check_performed: true,
        action_key: `INTERCEPT_DISPATCHED:${tracking}`,
        group,
        tracking_no: tracking,
        message: `${tracking}+拦截`,
        previous: null,
      },
    };
  }

  function saveRouting(body) {
    if (Number(body.expected_version) !== Number(state.routing.version)) {
      return { __status: 409, detail: `路由配置已从版本 ${state.routing.version} 更新，请刷新后重试。` };
    }
    const definition = clone(body.definition || {});
    definition.version = Number(state.routing.version) + 1;
    definition.schema_version = state.routing.schema_version;
    state.routing = definition;
    return { ok: true, config: clone(state.routing) };
  }

  function saveWorkflow(body) {
    const definition = clone(body.definition || {});
    definition.workflow_id = definition.workflow_id || `WF-DEMO-${Date.now().toString().slice(-6)}`;
    definition.version = Number(state.workflow.version || 1) + 1;
    definition.enabled = false;
    state.workflow = definition;
    return { ok: true, workflow: clone(state.workflow) };
  }

  function saveComponent(body) {
    const component = {
      component_id: body.component_id || `CMP-DEMO-${String(++componentSequence).padStart(4, "0")}`,
      component_type: body.component_type || "ClickElement",
      name: body.name || "演示组件",
      locator: body.capture?.selector ? { selector: body.capture.selector, text: body.capture.text || "" } : {
        window: body.capture?.window || {},
        control_type: body.capture?.element?.control_type || "Custom",
        name: body.capture?.element?.name || "",
        relative_point: body.capture?.relative_point || {},
      },
      config: body.config || {},
      on_failure: "MANUAL_REVIEW",
      enabled: true,
      version: 1,
      write_capable: true,
    };
    state.components.push(component);
    return { ok: true, component: clone(component) };
  }

  function processedHistory(url) {
    const source = String(url.searchParams.get("source") || "all");
    const shop = String(url.searchParams.get("shop_id") || "");
    const search = String(url.searchParams.get("search") || "").toLowerCase();
    const taskType = String(url.searchParams.get("task_type") || "");
    const page = Math.max(1, Number(url.searchParams.get("page") || 1));
    const pageSize = Math.max(1, Number(url.searchParams.get("page_size") || 100));
    let items = processedTasks();
    if (source !== "all") {
      items = items.filter((task) => (task.task_kind || "work_order") === source);
    }
    if (shop) items = items.filter((task) => task.shop_id === shop);
    if (taskType) items = items.filter((task) => task.facts?.work_order_title === taskType);
    if (search) {
      items = items.filter((task) => [task.order_no, task.work_order_no, task.facts?.shop_name, task.facts?.work_order_title].join(" ").toLowerCase().includes(search));
    }
    const total = items.length;
    const start = (page - 1) * pageSize;
    return { items: clone(items.slice(start, start + pageSize)), total, page, pages: Math.max(1, Math.ceil(total / pageSize)) };
  }

  function downloadProcessedCsv() {
    const items = processedTasks();
    const header = "店铺,订单号,工单号,任务类型,状态,金额";
    const rows = items.map((task) => [
      task.facts?.shop_name || "",
      task.order_no || "",
      task.work_order_no || "",
      task.facts?.work_order_title || "",
      task.display_state || task.state || "",
      Number(task.facts?.refund_amount || 0).toFixed(2),
    ].map((value) => `"${String(value).replaceAll('"', '""')}"`).join(","));
    const blob = new Blob([`\uFEFF${[header, ...rows].join("\n")}`], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "demo-processed-tasks.csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    const notice = document.querySelector("#notice");
    if (notice) notice.textContent = "演示导出已生成：demo-processed-tasks.csv";
  }

  function jsonResponse(data, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  }

  function asRequest(input) {
    if (input instanceof Request) return input;
    return new Request(typeof input === "string" ? new URL(input, window.location.href) : input);
  }

  const realFetch = window.fetch.bind(window);
  window.fetch = async function (input, init = {}) {
    const request = asRequest(input);
    const url = new URL(request.url, window.location.href);
    const apiIndex = url.pathname.indexOf("/api/");
    const path = apiIndex >= 0 ? url.pathname.slice(apiIndex) : url.pathname;
    const method = String(init.method || request.method || "GET").toUpperCase();
    let body = {};
    try {
      const raw = init.body !== undefined ? init.body : null;
      body = raw ? JSON.parse(String(raw)) : {};
    } catch (_) {
      body = {};
    }

    const ok = (data) => jsonResponse(data);
    const fail = (status, detail) => jsonResponse({ detail }, status);
    let match;

    if (method === "GET" && path === "/api/dashboard") return ok(clone(state.runtimeStatus ? dashboard : dashboard));
    if (method === "GET" && path === "/api/tasks") return ok(clone(pendingTasks()));
    if (method === "GET" && path === "/api/tasks/processed-history") return ok(processedHistory(url));
    if (method === "GET" && (match = path.match(/^\/api\/tasks\/(\d+)$/))) {
      const task = taskById(match[1]);
      if (!task) return fail(404, "任务不存在");
      return ok(clone({ ...task, logs: state.logsByTask[task.id] || [], evidence: [] }));
    }
    if (method === "PATCH" && (match = path.match(/^\/api\/tasks\/(\d+)\/status$/))) {
      return ok(updateTaskStatus(match[1], body));
    }
    if (method === "POST" && (match = path.match(/^\/api\/tasks\/(\d+)\/evaluate$/))) return ok({ ok: true, task_id: Number(match[1]), decision: { matched: true, rule_id: "R-DEMO-001" } });
    if (method === "POST" && (match = path.match(/^\/api\/tasks\/(\d+)\/collect-detail-readonly$/))) {
      const task = taskById(match[1]);
      return ok({ ok: true, tracking_no: task?.facts?.tracking_no || "DEMOZT0000000001", facts: task?.facts || {}, trace: [] });
    }
    if (method === "POST" && (match = path.match(/^\/api\/tasks\/(\d+)\/check-wdt-components$/))) {
      const task = taskById(match[1]);
      return ok({ ok: true, order_status: task?.facts?.wdt_order_status || "已发货", result: { unique: true } });
    }
    if (method === "POST" && (match = path.match(/^\/api\/tasks\/(\d+)\/preview-refund-components$/))) return ok({ ok: true, capture_pending: ["agree_refund", "submit_refund"] });
    if (method === "POST" && (match = path.match(/^\/api\/tasks\/(\d+)\/lookup-wdt$/))) return ok({ ok: true, eligible: true, order_status: "已发货", decision_message: "演示：唯一结果，符合后续处理条件" });
    if (method === "POST" && (match = path.match(/^\/api\/tasks\/(\d+)\/preview-intercept$/))) {
      const task = taskById(match[1]);
      return ok({ ok: true, message: `${task?.facts?.tracking_no || "DEMO0000000000000"}+拦截`, group_name: body.group_name || "示例客户服务群", sent_echo: "" });
    }
    if (method === "POST" && path.endsWith("/collect-aftersale-detail-readonly")) return ok({ ok: true });
    if (method === "GET" && path === "/api/interception-registers") return ok(clone(state.registers));
    if (method === "POST" && (match = path.match(/^\/api\/interception-registers\/(\d+)\/dingtalk\/sync$/))) {
      const record = state.registers.find((item) => Number(item.id) === Number(match[1]));
      if (record) {
        record.dingtalk_sync_status = "succeeded";
        record.dingtalk_remote_row = 109;
        record.dingtalk_synced_at = nowIso();
      }
      return ok({ ok: true, already_synced: false, remote_row: 109 });
    }
    if (method === "GET" && path === "/api/workflow-priorities") return ok(clone(state.priorities));
    if (method === "PUT" && path === "/api/workflow-priorities") {
      state.priorities = { ...clone(body.definition || state.priorities), version: Number(state.priorities.version) + 1 };
      return ok({ ok: true, config: clone(state.priorities) });
    }
    if (method === "GET" && path === "/api/runtime/status") return ok(clone(state.runtimeStatus));
    if (method === "POST" && path === "/api/runtime/start") {
      state.runtimeStatus = updateRuntimeState("COMPLETED", "静态演示完成 · 未执行真实动作", {
        continuous: Boolean(body.continuous),
        queue_only: Boolean(body.queue_only),
        current_task: null,
        counts: { total: 5, completed: 4, failed: 0, skipped: 1 },
        results: [
          { task_id: 1001, shop_name: "示例书店 A", order_no: "DEMO-2601-0001", status: "success", error: "", steps: [{ step: "读取工单详情" }, { step: "旺店通核验" }, { step: "拦截消息发送" }, { step: "结果回读" }] },
          { task_id: 1002, shop_name: "示例书店 B", order_no: "DEMO-2601-0002", status: "success", error: "", steps: [{ step: "在途事实合并" }, { step: "路由命中" }, { step: "转人工复核" }] },
          { task_id: 1003, shop_name: "示例书店 C", order_no: "DEMO-2601-0003", status: "success", error: "", steps: [{ step: "售后详情读取" }, { step: "延长收货" }] },
          { task_id: 1004, shop_name: "示例天猫旗舰店", order_no: "DEMO-TMALL-0004", status: "skipped", error: "等待人工确认拦截群", steps: [] },
        ],
      });
      return ok(clone(state.runtimeStatus));
    }
    if (method === "POST" && (match = path.match(/^\/api\/runtime\/(pause|resume|stop)$/))) {
      const action = match[1];
      if (action === "pause") state.runtimeStatus = updateRuntimeState("PAUSED", "静态演示已暂停");
      if (action === "resume") state.runtimeStatus = updateRuntimeState("RUNNING", "静态演示运行中");
      if (action === "stop") state.runtimeStatus = updateRuntimeState("STOPPED", "静态演示已停止");
      return ok(clone(state.runtimeStatus));
    }
    if (method === "POST" && path === "/api/pdd/open-shop-windows") {
      return ok({ ok: true, summary: { opened_or_reused: 6 }, results: [] });
    }

    if (method === "GET" && path === "/api/components/catalog") return ok(clone(catalog));
    if (method === "GET" && path === "/api/components") return ok(clone(state.components));
    if (method === "GET" && path === "/api/workflows") return ok(clone([state.workflow]));
    if (method === "POST" && path === "/api/workflows") return ok(saveWorkflow(body));
    if (method === "POST" && path === "/api/components") return ok({ ok: true, component: saveComponent(body.definition || body).component });
    if (method === "POST" && (path === "/api/desktop-capture/start" || path === "/api/capture/start")) return ok({ status: "capturing", delay_seconds: 3 });
    if (method === "GET" && path === "/api/desktop-capture/status") {
      return ok({ status: "captured", result: { application: "企业微信", window: { title: "企业微信", class_name: "WeChatMainWndForPC" }, element: { name: "搜索", control_type: "Edit", class_name: "Edit" }, relative_point: { x: 0.18, y: 0.09 }, ancestry: [], template_path: "" } });
    }
    if (method === "GET" && /^\/api\/capture\/[^/]+$/.test(path)) {
      return ok({ status: "captured", result: { text: "立即处理", selector: "text=立即处理", window_title: "示例书店 A - 商家后台", element: { name: "立即处理", control_type: "Hyperlink" } } });
    }
    if (method === "POST" && /^\/api\/(desktop-)?capture\/(cancel|[^/]+\/cancel)$/.test(path)) return ok({ status: "cancelled" });
    if (method === "POST" && /^\/api\/(desktop-)?capture\/component$/.test(path)) return ok(saveComponent(body));

    if (method === "GET" && path === "/api/in-transit-routing/config") return ok(clone(state.routing));
    if (method === "PUT" && path === "/api/in-transit-routing/config") {
      const result = saveRouting(body);
      return result.__status ? fail(result.__status, result.detail) : ok(result);
    }
    if (method === "POST" && path === "/api/in-transit-routing/preview") return ok(previewRouting(body.inputs || {}));
    if (method === "GET" && path === "/api/message-dispatch/config") return ok(clone(state.messageConfig));
    if (method === "PUT" && path === "/api/message-dispatch/config") {
      state.messageConfig = { ...state.messageConfig, ...(clone(body.definition || {})), version: Number(state.messageConfig.version) + 1 };
      return ok({ ok: true, config: clone(state.messageConfig) });
    }
    if (method === "GET" && path === "/api/message-dispatch/readiness") return ok(clone(readiness));
    if (method === "POST" && path === "/api/message-dispatch/preflight") return ok({ ok: true, result: { group_header: body.group_name || "示例供应商 A" } });

    console.warn("[static-demo] Unhandled API request:", method, path);
    return fail(404, `静态演示未实现 ${method} ${path}`);
  };

  document.addEventListener("click", (event) => {
    const exportButton = event.target.closest("#export-processed");
    if (!exportButton) return;
    event.preventDefault();
    event.stopPropagation();
    downloadProcessedCsv();
  }, true);

  document.addEventListener("submit", (event) => {
    const form = event.target.closest('form[action="/logout"]');
    if (!form) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const button = form.querySelector("button");
    if (button) {
      button.textContent = "静态演示 · 已退出";
      button.disabled = true;
    }
  }, true);
})();
