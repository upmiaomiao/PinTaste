import { profileButton } from "./profile.mjs";
import { portalAsset } from "./ui-config.mjs";
import {
  escapeHTML as esc,
  normaliseList,
  statusLabel,
  formatDate,
} from "./lib.mjs";
const NAV = [
  ["overview", "运行概览", "database"],
  ["models", "模型配置", "cpu"],
  ["data", "多星数据", "layers"],
  ["sources", "图源与地点", "search"],
  ["search", "联网搜索", "globe"],
  ["policy", "任务策略", "shield"],
  ["appearance", "界面设置", "settings"],
  ["users", "用户管理", "users"],
];
const NAV_GROUPS = [
  ["运行与资源", ["overview", "data", "sources"]],
  ["智能分析", ["models", "search", "policy"]],
  ["系统设置", ["appearance", "users"]],
];
function adminNavigation(tab, icon) {
  return NAV_GROUPS.map(
    ([label, ids]) =>
      `<section class="admin-nav-group"><h2>${label}</h2>${ids
        .map((id) => {
          const [, title, glyph] = NAV.find((item) => item[0] === id);
          return `<button class="${tab === id ? "active" : ""}" data-action="admin-tab" data-id="${id}" ${tab === id ? 'aria-current="page"' : ""}>${icon(glyph)}<span>${title}</span></button>`;
        })
        .join("")}</section>`,
  ).join("");
}
const ROLES = [
  ["main", "主模型", "任务规划、工具调用与结果组织，不作高分判读"],
  ["selector", "选星模型", "需要模型选星时评估图源适用性"],
  ["recognizer", "见微遥感识别", "固废场景识别与证据标注"],
  [
    "science_analyst",
    "Gemini 视觉复核与科学影像",
    "DMX Gemini 接口；复核高分建筑等误报，同时解读多光谱与 SAR 证据",
  ],
  ["reasoner", "文本一致性复核（可选）", "仅核对文字结论与证据是否一致，不看图；默认关闭"],
];
function modelLabel(model = {}) {
  if (model.display_name) return model.display_name;
  try {
    if (
      model.model === "deepseek-flash" &&
      new URL(model.base_url).hostname === "api.deepseek.com"
    )
      return "DeepSeek V4.1 Flash 1017";
  } catch {}
  return model.model || "尚未指定模型";
}
export function modelInputLimits(model = {}) {
  try {
    if (
      model.model === "deepseek-flash" &&
      new URL(model.base_url).hostname === "api.deepseek.com"
    )
      return { minTokens: 1, maxTokens: 393216, maxTimeout: 3600 };
  } catch {}
  return { minTokens: 128, maxTokens: 32768, maxTimeout: 600 };
}
export function handleAdminModelInput(event) {
  const target = event.target,
    match = /^models\.([a-z_]+)\.(model|base_url)$/.exec(target?.name || "");
  if (!match || !ROLES.some(([role]) => role === match[1])) return;
  const form = target.form || target.closest?.("form");
  if (form?.dataset.section !== "models") return;
  const prefix = `models.${match[1]}.`,
    control = (key) => form.querySelector(`[name="${prefix}${key}"]`),
    limits = modelInputLimits({
      model: control("model")?.value,
      base_url: control("base_url")?.value,
    });
  const tokens = control("max_tokens"),
    timeout = control("timeout");
  if (tokens) {
    tokens.min = String(limits.minTokens);
    tokens.max = String(limits.maxTokens);
  }
  if (timeout) timeout.max = String(limits.maxTimeout);
}
function field(name, label, value = "", type = "text", hint = "", attrs = "") {
  return `<label class="field">${label}<input name="${name}" type="${type}" value="${esc(value ?? "")}" ${type === "password" ? 'autocomplete="new-password" placeholder="留空保持已有密钥"' : 'autocomplete="off"'} ${attrs}>${hint ? `<small>${hint}</small>` : ""}</label>`;
}
function toggle(name, label, value, hint = "") {
  return `<label class="switch-row"><span><strong>${label}</strong>${hint ? `<small>${hint}</small>` : ""}</span><input name="${name}" type="checkbox" ${value ? "checked" : ""}></label>`;
}
function secret(name, configured) {
  return field(
    name,
    "API 密钥",
    "",
    "password",
    configured ? "已配置 · 输入新密钥以替换" : "尚未配置 · 仅保存在服务端",
  );
}
function saveBar(icon, busy = false) {
  return `<footer class="admin-action-bar"><span>保存后用于后续任务</span><button class="primary" ${busy ? "disabled" : ""}>${icon("save")}保存配置</button></footer>`;
}
function dataHealthSection(state, icon) {
  const health = state.adminDataHealth,
    configured =
      !!state.config?.data?.base_url || !!state.config?.unified_data?.enabled;
  const labels = {
    checking: "正在检查",
    connected: "本次连接正常",
    ready: "本次连接正常",
    ok: "本次连接正常",
    unavailable: "本次连接失败",
    authentication_required: "需要核对凭据",
    not_configured: "尚未配置",
    disabled: "未启用",
    partial: "部分服务未连接",
  };
  const services = health?.services || {};
  return `<section class="admin-health-strip"><div>${icon("database")}<strong>多星数据服务</strong><span class="pill">${configured ? "地址已配置" : "尚未配置"}</span><span>${labels[health?.status] || "尚未检查连接"}</span></div><button class="secondary" data-action="admin-data-health" ${health?.status === "checking" ? "disabled" : ""}>${icon("refresh")}检查连接</button>${
    Object.keys(services).length
      ? `<div class="data-service-health">${[
          ["science", "多光谱与 SAR 数据服务"],
          ["unified", "高清与时序 PNG 服务"],
        ]
          .map(([key, title]) => {
            const service = services[key];
            return service
              ? `<section><strong>${title}</strong><span>${esc(labels[service.status] || "状态待核实")}</span>${service.message ? `<p>${esc(service.message)}</p>` : ""}${key === "unified" && Number.isInteger(service.source_count) ? `<small>接口返回 ${service.source_count} 项目录记录；覆盖和取图能力逐次核验。</small>` : ""}</section>`
              : "";
          })
          .join("")}</div>`
      : ""
  }${health?.message ? `<p>${esc(health.message)}${health.checked_at ? ` · ${esc(formatDate(health.checked_at, true))}` : ""}</p>` : ""}</section>`;
}

export function renderAdmin(state, icon) {
  const tab = state.adminTab || "overview",
    config = state.config;
  return `<div class="admin-shell"><header class="admin-topbar"><button class="brand admin-brand" data-action="admin-back"><img src="${portalAsset("assets/logo.png")}" alt="无隅"><strong>无隅见微</strong></button><div class="admin-topbar-right"><button class="admin-top-return" data-action="admin-back">${icon("back")}返回问答</button><span class="admin-role">${icon("shield")}管理工作台</span><div class="account">${profileButton(state.user, icon, true)}</div></div></header><div class="admin-layout"><aside class="admin-sidebar"><span class="admin-nav-label">工作空间</span><button class="admin-back" data-action="admin-back">${icon("chat")}<span>返回问答</span>${icon("back")}</button><nav class="admin-nav" aria-label="管理员导航">${adminNavigation(tab, icon)}</nav><div class="admin-nav-bottom">${icon("shield")}管理员工作空间</div></aside><main class="admin-content"><header class="admin-heading"><div><p>系统管理</p><h1>${NAV.find((x) => x[0] === tab)?.[1] || "管理工作台"}</h1></div><button class="secondary" data-action="admin-refresh">${icon("refresh")}刷新${tab === "overview" ? "概览" : "配置"}</button></header>${state.error ? `<div class="error-box" role="alert">${esc(state.error)}</div>` : ""}${!config ? '<div class="empty-state"><span class="spin"></span>正在读取服务端配置…</div>' : tab === "overview" ? overview(state, icon) : tab === "models" ? models(config, icon) : tab === "data" ? dataConfig(config, icon) : tab === "sources" ? sourceSettings(state, icon) : tab === "search" ? searchConfig(config, icon) : tab === "policy" ? policyConfig(config, icon) : tab === "appearance" ? appearanceConfig(config, icon) : usersView(state, icon)}</main></div></div>`;
}
function overview(state, icon) {
  const snapshot = state.adminOverview;
  if (!snapshot)
    return `<section class="panel admin-overview-empty"><h2>读取运行概览</h2><p>正在获取全部用户的任务统计，请稍候或刷新概览。</p><button class="secondary" data-action="admin-refresh">${icon("refresh")}刷新概览</button></section>`;
  const counts = snapshot.counts,
    models = snapshot.models || [],
    recent = snapshot.recent_tasks || [];
  return `<div class="admin-overview-context"><p class="admin-description">全部用户的门户运行情况</p><span>更新于 ${esc(formatDate(snapshot.updated_at, true))}</span></div><div class="admin-stat-grid">${[
    [
      "注册用户",
      counts.registered_users,
      "users",
      `${counts.active_users ?? "—"} 个账号可登录`,
    ],
    [
      "正在分析",
      counts.running,
      "clock",
      `${counts.planning ?? "—"} 项正在准备任务大纲`,
    ],
    ["等待执行", counts.queued, "file", "已进入分析队列"],
    [
      "累计完成",
      counts.completed,
      "check",
      `累计提交 ${counts.total_tasks ?? "—"} 项任务`,
    ],
  ]
    .map(
      ([title, value, i, detail]) =>
        `<section class="admin-stat"><span class="stat-label">${icon(i)}${title}</span><strong>${esc(value ?? "—")}</strong><small>${esc(detail)}</small></section>`,
    )
    .join(
      "",
    )}</div>${snapshot.execution ? `<section class="admin-health-strip"><div>${icon("shield")}<strong>执行容量</strong><span class="pill">单实例</span>${snapshot.execution.draining ? '<span class="pill">维护中 · 不接收新任务</span>' : ""}</div>${capacityLines(snapshot.execution)}</section>` : ""}${dataHealthSection(state, icon)}<section class="panel"><div class="panel-heading"><h2>已接入模型</h2><button class="text-button" data-action="admin-tab" data-id="models">管理模型 ${icon("chevron")}</button></div><div class="config-card-grid">${ROLES.map(
    ([id, title, description]) => {
      const model = {
          ...state.config?.models?.[id],
          ...(models.find((item) => item.role === id) || {}),
        },
        configured = model.configured === true;
      return `<button class="config-card" data-action="admin-tab" data-id="models"><div class="admin-model-heading"><span class="role-icon">${icon("cpu")}</span><span class="pill">${configured ? "配置完整" : "待配置"}</span></div><strong>${title}</strong><span class="admin-model-name">${esc(modelLabel(model))}</span><p>${description}</p><small>${(model.input_modalities || []).includes("image") ? "文本与图像" : "文本"} · ${model.max_tokens ? `${Number(model.max_tokens).toLocaleString("zh-CN")} 输出预算` : "输出预算待配置"}</small></button>`;
    },
  ).join(
    "",
  )}</div><p class="admin-panel-note">模型卡显示服务端保存的配置；运行结果以实际任务为准。</p></section><section class="panel"><div class="panel-heading"><h2>最近任务</h2><button class="secondary" data-action="admin-refresh">${icon("refresh")}刷新</button></div><p class="admin-description">显示全部用户最近提交的任务及真实执行状态。任务资料、影像和报告仍由所属用户访问。</p><div class="table-wrap"><table><thead><tr><th>用户</th><th>任务</th><th>状态</th><th>提交时间</th></tr></thead><tbody>${recent.map((task) => `<tr><td><strong>${esc(task.user_display_name || task.username)}</strong></td><td class="admin-task-title">${esc(task.objective)}</td><td><span class="pill">${esc(statusLabel(task.status))}</span></td><td class="admin-time-cell">${esc(formatDate(task.created_at, true))}</td></tr>`).join("") || '<tr><td colspan="4" class="admin-table-empty">暂无任务记录</td></tr>'}</tbody></table></div>${counts.failed || counts.interrupted ? `<p class="admin-panel-note">历史执行失败 ${counts.failed} 项，服务中断 ${counts.interrupted} 项。</p>` : ""}</section>`;
}
function models(c, icon) {
  return `<p class="admin-description">使用 OpenAI 兼容接口连接不同角色模型。模型名称与输入能力应与实际接口一致。</p><form id="config-form" data-section="models">${ROLES.map(
    ([id, title, desc]) => {
      const m =
          c.models?.[id] ||
          (id === "science_analyst"
            ? { model: "gemini-3.8-flash", input_modalities: ["text", "image"] }
            : {}),
        limits = modelInputLimits(m);
      return `<section class="panel"><div class="role-heading"><span class="role-icon">${icon("cpu")}</span><div><h2>${title}</h2><p>${desc}</p></div><span class="pill ${m.api_key_configured ? "green" : ""}">${m.api_key_configured ? "已配置" : "待配置"}</span></div><div class="field-grid">${field(`models.${id}.model`, "接口模型名称", m.model, "text", modelLabel(m) !== m.model ? esc(modelLabel(m)) : "")}${field(`models.${id}.base_url`, "API Base URL", m.base_url, "url")}${secret(`models.${id}.api_key`, m.api_key_configured)}${field(`models.${id}.timeout`, "请求超时（秒）", m.timeout, "number", "", `min="5" max="${limits.maxTimeout}"`)}${field(`models.${id}.max_tokens`, "最大输出 tokens", m.max_tokens, "number", "", `min="${limits.minTokens}" max="${limits.maxTokens}"`)}<label class="field">输入能力<select name="models.${id}.input_modalities"><option value="text" ${(m.input_modalities || []).includes("image") ? "" : "selected"}>文本</option><option value="text,image" ${(m.input_modalities || []).includes("image") ? "selected" : ""}>文本与图像</option></select></label><label class="field">思考参数<select name="models.${id}.enable_thinking"><option value="null" ${m.enable_thinking == null ? "selected" : ""}>不发送，使用接口默认值</option><option value="true" ${m.enable_thinking === true ? "selected" : ""}>开启</option><option value="false" ${m.enable_thinking === false ? "selected" : ""}>关闭</option></select><small>仅在接口支持该参数时设置。</small></label></div>${toggle(`models.${id}.trust_env`, "使用系统代理环境", m.trust_env, "开启后遵循系统 HTTP(S) 代理环境变量")}</section>`;
    },
  ).join("")}${saveBar(icon)}</form>`;
}
function dataConfig(c, icon) {
  const d = c.data || {},
    u = c.unified_data || {};
  return `<p class="admin-description">分别连接科学数据服务和统一 PNG 影像服务。任务按实际能力选择来源，保留版本、拍摄日期与空间核验记录。</p><form id="config-form" data-section="data"><section class="panel"><div class="panel-heading"><h2>多光谱与 SAR 数据服务</h2><span class="pill ${d.api_key_configured ? "green" : ""}">${d.api_key_configured ? "已配置凭据" : "无需凭据或待配置"}</span></div><div class="field-grid">${field("data.base_url", "数据 API 地址", d.base_url, "url")}<label class="field">连接方式<select name="data.mode">${[
    ["auto", "自动选择"],
    ["worker", "桌面 Worker"],
    ["gateway", "API Gateway"],
  ]
    .map(
      ([v, t]) =>
        `<option value="${v}" ${d.mode === v ? "selected" : ""}>${t}</option>`,
    )
    .join(
      "",
    )}</select></label>${secret("data.api_key", d.api_key_configured)}</div><div class="notice-box">${icon("layers")}图源是否可用由任务实际请求验证；桌面图源需要对应客户端已登录且 Worker 在线。</div></section><section class="panel"><div class="panel-heading"><h2>高清与时序 PNG 影像服务</h2><span class="pill">${u.api_key_configured ? "已配置凭据" : "尚未配置凭据"}</span></div>${toggle("unified_data.enabled", "启用统一影像 API", u.enabled, "连接 2026 高清组、世纪空间、吉林一号、四维地球、天地图及历史时序；图源权限和目标覆盖逐次核验")}<div class="field-grid">${field("unified_data.base_url", "统一影像 API 地址", u.base_url || "", "url")}${secret("unified_data.api_key", u.api_key_configured)}${field("unified_data.timeout_seconds", "取图超时（秒）", u.timeout_seconds ?? 120, "number", "", 'min="30" max="120"')}${field("unified_data.max_concurrency", "最多并发取图数", u.max_concurrency ?? 2, "number", "", 'min="1" max="2"')}</div><p class="admin-panel-note">此服务由后端使用 X-API-Key 调用。年度版次不代替实际拍摄日期；多期影像通过空间对应和时相检查后才用于变化判断。</p></section>${saveBar(icon)}</form>`;
}
function searchConfig(c, icon) {
  const m = c.metaso || {},
    w = c.web_search || {},
    x = c.wechat || {};
  return `<p class="admin-description">网页使用 DeepSeek，学术文献使用秘塔，并按当前问题补充微信公众号资料。回答底部保留出处、公众号与发布时间，已读内容可在任务中复查。</p><form id="config-form" data-section="search"><section class="panel"><div class="panel-heading"><h2>DeepSeek 网页搜索</h2><span class="pill ${w.api_key_configured ? "green" : ""}">${w.api_key_configured ? "已配置" : "待配置"}</span></div><div class="field-grid">${field("web_search.base_url", "搜索 API 地址", w.base_url || "https://www.dmxapi.cn/v1", "url")}${secret("web_search.api_key", w.api_key_configured)}</div></section><section class="panel"><div class="panel-heading"><h2>秘塔学术搜索</h2><span class="pill ${m.api_key_configured ? "green" : ""}">${m.api_key_configured ? "已配置" : "待配置"}</span></div><div class="field-grid">${field("metaso.base_url", "学术搜索 API 地址", m.base_url, "url")}${secret("metaso.api_key", m.api_key_configured)}</div><p class="admin-description">涉及论文、文献、研究进展或数据集的问题会检索学术来源。</p></section><section class="panel"><div class="panel-heading"><h2>微信公众号</h2><button type="button" class="secondary" data-action="admin-search-health">${icon("refresh")}检查连接</button></div>${toggle("wechat.enabled", "启用公众号搜索", x.enabled, "补充公开文章，并立即读取、保存重点正文")}<div class="field-grid">${field("wechat.mcp_url", "MCP 完整地址", x.mcp_url || "", "url")}${secret("wechat.api_key", x.api_key_configured)}${field("wechat.timeout", "总超时（秒）", x.timeout || 180, "number", "", 'min="5" max="180"')}${field("wechat.max_results", "每关键词最多文章", x.max_results || 5, "number", "", 'min="1" max="50"')}${field("wechat.max_pages", "最多索引页", x.max_pages || 3, "number", "", 'min="1" max="3"')}${field("wechat.read_articles", "每任务读取重点文章", x.read_articles ?? 2, "number", "", 'min="0" max="5"')}</div><p class="admin-description">默认检索近两年；文章索引与正文时间冲突时以正文为准。正文暂未取得或仅阅读片段时会在来源中标注。网关鉴权密钥可选，无需填写模型密钥。</p></section>${saveBar(icon)}</form>`;
}
function capacityLines(execution) {
  const lanes = execution.lanes || {};
  const line = (title, lane, fallbackCapacity) => lane
    ? `<p><strong>${title}</strong>：同时处理 ${esc(lane.capacity)} 个，正在处理 ${esc(lane.running)} 个，排队 ${esc(lane.waiting)} 个；排队等待约 ${esc(lane.wait_seconds_p50)} 秒（多数）/ ${esc(lane.wait_seconds_p95)} 秒（较慢的）。</p>`
    : `<p><strong>${title}</strong>：同时处理 ${esc(fallbackCapacity)} 个。</p>`;
  return line("影像分析", lanes.analysis, execution.concurrency) + line("追问与资料检索", lanes.interactive, execution.qa_concurrency)
    + `<p>全系统待处理上限 ${esc(execution.max_pending_tasks)} 项 · 单个用户同时 ${esc(execution.max_user_interactive)} 项普通任务、${esc(execution.max_user_batch)} 项批量任务 · 同时处理 ${esc(execution.max_parallel_uploads)} 个上传。</p>`;
}
function policyConfig(c, icon) {
  const p = c.task_policy || {};
  return `<p class="admin-description">设置任务的默认检索、专业复核与资源预算。影像不足或证据冲突时，分析会明确说明限制。</p><form id="config-form" data-section="task_policy"><section class="panel"><div class="panel-heading"><h2>任务默认行为</h2></div>${toggle("task_policy.search_policy", "默认启用政策检索", p.search_policy, "收集任务相关的政策与公开资料")}${toggle("task_policy.consult_shensi", "默认启用文本一致性复核", p.consult_shensi, "仅核对文字结论与已有证据是否一致；需先配置该模型")}${toggle("task_policy.adaptive_review", "启用自适应追加复核", p.adaptive_review, "根据证据不足或冲突情况追加取图与复核")}<div class="field-grid"><label class="field">选星方式<select name="task_policy.selector_mode"><option value="auto" ${p.selector_mode !== "model" ? "selected" : ""}>自动规则选择</option><option value="model" ${p.selector_mode === "model" ? "selected" : ""}>模型专业选星</option></select></label>${field("task_policy.max_policy_queries", "每任务最多搜索次数", p.max_policy_queries, "number", "", 'min="1" max="8"')}${field("task_policy.max_assets", "每任务最多影像数", p.max_assets, "number", "", 'min="1" max="8"')}${field("task_policy.concurrency", "影像分析同时进行数", p.concurrency, "number", "", 'min="1" max="8"')}${field("task_policy.qa_concurrency", "追问与资料检索同时进行数", p.qa_concurrency ?? 8, "number", "", 'min="1" max="32"')}${field("task_policy.recognition_concurrency", "单个任务内同时判读影像数", p.recognition_concurrency ?? 3, "number", "", 'min="1" max="8"')}<label class="field">见微原生输出格式<select name="task_policy.rs_output_format"><option value="xml" ${p.rs_output_format === "xml" ? "selected" : ""}>原生 XML</option><option value="json" ${p.rs_output_format === "json" ? "selected" : ""}>JSON</option></select><small>应与当前部署模型的训练输出格式一致。</small></label></div></section><section class="panel"><div class="panel-heading"><h2>多人使用的容量与保护</h2></div><p class="admin-description">这些数值保存后立即生效，不需要等没有任务时再改，也不会打断正在运行的任务。建议先保持默认，上线后看"运行概览"里的排队等待再调整：影像分析排队等待持续偏长，再把"影像分析同时进行数"调大，同时确认图源服务和模型没有限流。</p><div class="field-grid">${field("task_policy.max_pending_tasks", "全系统待处理任务上限", p.max_pending_tasks ?? 400, "number", "", 'min="10" max="2000"')}${field("task_policy.max_job_groups", "同时挂起的任务上限", p.max_job_groups ?? 200, "number", "", 'min="4" max="1000"')}${field("task_policy.max_user_interactive", "单个用户同时进行的普通任务", p.max_user_interactive ?? 3, "number", "", 'min="1" max="10"')}${field("task_policy.max_user_batch", "单个用户同时进行的批量任务", p.max_user_batch ?? 50, "number", "", 'min="1" max="200"')}${field("task_policy.optical_review_timeout", "同图独立复核等待上限（秒）", p.optical_review_timeout ?? 90, "number", "", 'min="10" max="300"')}${field("task_policy.cross_review_timeout", "跨图复核等待上限（秒）", p.cross_review_timeout ?? 45, "number", "", 'min="10" max="300"')}</div></section>${saveBar(icon)}</form>`;
}
function usersView(state, icon) {
  const shared = state.auth?.mode === "wuyu";
  return `<p class="admin-description">${shared ? "账号来自无隅 AI 平台；本页仅管理见微系统的访问权限、字体和月度额度，不修改平台账号资料。" : "管理员创建账号并分配权限。普通用户仅可访问自己的任务、上传资料与报告。"}</p><section class="panel"><div class="users-toolbar"><h2>用户与访问权限 <span class="pill">${state.users.length}</span></h2>${shared ? "" : `<button class="primary" data-action="user-add">${icon("plus")}添加用户</button>`}</div><div class="table-wrap"><table><thead><tr><th>用户</th><th>用户名</th><th>角色</th><th>状态</th><th>见微设置</th></tr></thead><tbody>${state.users.map((u) => `<tr><td><strong>${esc(u.display_name || u.username)}</strong>${u.id === state.user.id ? "<small>当前账号</small>" : ""}</td><td>${esc(u.username)}</td><td>${u.role === "admin" ? "管理员" : "业务用户"}</td><td><span class="pill ${u.active ? "green" : ""}">${u.active ? "正常" : "已停用"}</span></td><td><button class="text-button" data-action="user-settings" data-id="${esc(u.id)}">字体与额度</button> <button class="text-button" data-action="user-edit" data-id="${esc(u.id)}">${shared ? "见微权限" : "编辑"}</button>${u.id !== state.user.id ? ` <button class="text-button" data-action="user-toggle" data-id="${esc(u.id)}">${u.active ? "停用见微" : "恢复见微"}</button>` : ""}</td></tr>`).join("") || '<tr><td colspan="5">暂无账号</td></tr>'}</tbody></table></div><p class="admin-local-note">额度按北京时间自然月计量；留空表示不限额。配额的扣减与拦截由见微后端执行。</p></section>`;
}
function appearanceConfig(c, icon) {
  return `<p class="admin-description">这些设置仅作用于无隅见微遥感工作台，不更改无隅 AI 平台。</p><form id="config-form" data-section="appearance"><section class="panel"><h2>默认字体</h2><div class="field-grid"><label class="field">默认正文字号<select name="appearance.default_font_size">${[12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24].map((size) => `<option value="${size}" ${Number(c.appearance?.default_font_size || 16) === size ? "selected" : ""}>${size} px</option>`).join("")}</select><small>适用于未单独设置字号的用户；个人字号在“用户管理 → 字体与额度”中设置。</small></label></div></section>${saveBar(icon)}</form>`;
}
export function userSettingsModal(user, settings = {}, icon = () => "") {
  return `<header class="modal-header"><h2>${esc(user.display_name || user.username)} · 见微设置</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon("close")}</button></header><form id="user-settings-form" data-id="${esc(user.id)}"><div class="modal-body"><p>仅设置这个用户在见微系统中的界面和资源额度。</p><label class="field">正文字号<select name="font_size"><option value="" ${settings.font_size_override == null ? "selected" : ""}>使用系统默认</option>${[12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24].map((size) => `<option value="${size}" ${Number(settings.font_size_override) === size ? "selected" : ""}>${size} px</option>`).join("")}</select></label><div class="quota-fields">${field("imagery_quota", "每月卫星影像额度", settings.imagery_quota ?? "", "number", "留空不限额；0 表示本月不允许新增取图", 'min="0" step="1"')}${field("qa_quota", "每月问答额度", settings.qa_quota ?? "", "number", "留空不限额；0 表示本月不允许新增问答", 'min="0" step="1"')}</div><p class="quota-usage">本月已用：影像 ${esc(settings.usage?.imagery ?? 0)} 次 · 问答 ${esc(settings.usage?.qa ?? 0)} 次。按北京时间自然月统计。</p></div><footer class="modal-footer"><button type="button" class="secondary" data-action="close-modal">取消</button><button class="primary" type="submit">${icon("save")}保存见微设置</button></footer></form>`;
}

function userModal(u, icon, shared = false) {
  if (shared && u)
    return `<header class="modal-header"><h2>见微访问权限</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon("close")}</button></header><form id="user-form" data-id="${esc(u.id)}" data-shared="true"><div class="modal-body"><p>${esc(u.display_name || u.username)} 的权限仅作用于见微系统。</p><label class="field">见微角色<select name="role"><option value="user" ${u.role !== "admin" ? "selected" : ""}>业务用户</option><option value="admin" ${u.role === "admin" ? "selected" : ""}>见微管理员</option></select></label><p class="admin-local-note">平台账号、密码和无隅 AI 管理权限保持原有设置。</p></div><footer class="modal-footer"><button type="button" class="secondary" data-action="close-modal">取消</button><button class="primary" type="submit">保存见微权限</button></footer></form>`;
  return `<header class="modal-header"><h2>${u ? "编辑用户" : "添加用户"}</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon("close")}</button></header><form id="user-form" data-id="${esc(u?.id || "")}"><div class="modal-body"><div class="field-grid">${u ? `<label class="field">用户名<input value="${esc(u.username)}" disabled></label>` : field("username", "用户名", "", "text", "至少 3 个字符", 'required minlength="3" maxlength="80"')}${field("display_name", "显示名称", u?.display_name || "")}${field("password", u ? "重置密码（留空保持不变）" : "初始密码", "", "password", "至少 10 个字符", `${u ? "" : "required"} minlength="10" maxlength="128"`)}<label class="field">用户角色<select name="role"><option value="user" ${u?.role !== "admin" ? "selected" : ""}>监管业务用户</option><option value="admin" ${u?.role === "admin" ? "selected" : ""}>系统管理员</option></select></label></div><p class="evidence-note">请将初始密码交给对应用户；工作台不会展示已保存的密码。</p></div><footer class="modal-footer"><button type="button" class="secondary" data-action="close-modal">取消</button><button class="primary">${icon("save")}${u ? "保存修改" : "创建用户"}</button></footer></form>`;
}
export async function handleAdminAction(action, button, state, ctx) {
  const { api, render, toast, showModal, icon } = ctx;
  if (state.user?.role !== "admin") return;
  if (action === "admin-load-sources") {
    state.adminSourceChoices = (await api("/source-choices")).sources || [];
    render();
  } else if (action === "admin-search-health") {
    button.disabled = true;
    try {
      const result = await api("/admin/search-health");
      const status = result.wechat?.status;
      toast(
        status === "ready"
          ? "公众号 MCP 连接正常，工具已就绪"
          : status === "cooldown"
            ? "公众号服务正在冷却，请稍后使用"
            : "公众号暂未连通，请检查保存的地址与开关",
        status !== "ready",
      );
    } finally {
      button.disabled = false;
    }
  } else if (action === "admin-back") {
    state.route = "home";
    state.active = null;
    state.error = "";
    if (!state.draft.search_touched)
      state.draft.search_policy =
        state.config?.task_policy?.search_policy ?? true;
    render();
  } else if (action === "admin-tab") {
    state.adminTab = button.dataset.id;
    state.error = "";
    render();
    if (state.adminTab === "overview") {
      try {
        state.adminOverview = await api("/admin/overview");
        render();
      } catch (error) {
        state.error = error.message;
        render();
      }
    }
  } else if (action === "admin-data-health") {
    state.adminDataHealth = { status: "checking" };
    render();
    try {
      state.adminDataHealth = await api("/admin/data-health");
      render();
    } catch (error) {
      state.adminDataHealth = { status: "unavailable", message: error.message };
      render();
    }
  } else if (action === "admin-refresh") {
    const [config, users, overview] = await Promise.all([
      api("/config"),
      api("/users"),
      api("/admin/overview"),
    ]);
    state.config = config;
    state.users = normaliseList(users);
    state.adminOverview = overview;
    state.error = "";
    render();
    toast("服务端数据已刷新");
  } else if (action === "user-settings") {
    const u = state.users.find((u) => u.id === button.dataset.id);
    if (u) {
      const settings = await api(
        "/users/" + encodeURIComponent(u.id) + "/settings",
      );
      showModal(userSettingsModal(u, settings, icon));
    }
  } else if (action === "user-add") showModal(userModal(null, icon));
  else if (action === "user-edit") {
    const u = state.users.find((u) => u.id === button.dataset.id);
    if (u) showModal(userModal(u, icon, state.auth?.mode === "wuyu"));
  } else if (action === "user-toggle") {
    const u = state.users.find((u) => u.id === button.dataset.id);
    if (!u) return;
    await api("/users/" + encodeURIComponent(u.id), {
      method: "PATCH",
      body: { active: !u.active },
    });
    state.users = normaliseList(await api("/users"));
    render();
    toast(u.active ? "账号已停用" : "账号已恢复");
  }
}
export async function handleAdminSubmit(event, state, ctx) {
  const { api, render, toast, closeModal } = ctx;
  if (state.user?.role !== "admin") return;
  const form = event.target,
    button = form.querySelector('button[type="submit"],button.primary');
  if (button) button.disabled = true;
  try {
    if (form.id === "user-settings-form") {
      const fields = new FormData(form),
        payload = {};
      for (const key of ["font_size", "imagery_quota", "qa_quota"]) {
        const raw = String(fields.get(key) || "").trim();
        payload[key] = raw === "" ? null : Number(raw);
      }
      const id = form.dataset.id;
      const updated = await api(
        "/users/" + encodeURIComponent(id) + "/settings",
        { method: "PATCH", body: payload },
      );
      if (id === state.user.id) {
        state.preferences = updated;
        document.documentElement.style.setProperty(
          "--portal-font-size",
          (updated.font_size ||
            state.config?.appearance?.default_font_size ||
            16) + "px",
        );
      }
      closeModal();
      render();
      toast("见微字体与额度已保存");
    } else if (form.id === "source-settings-form") {
      const all = state.adminSourceChoices || [],
        enabled = new Set(new FormData(form).getAll("enabled_source"));
      state.config = await api("/config", {
        method: "PUT",
        body: {
          sources: {
            disabled_sources: all
              .filter((s) => !enabled.has(s.source))
              .map((s) => s.source),
            source_order: all.map((s) => s.source),
          },
        },
      });
      render();
      toast("图源策略已保存");
    } else if (form.id === "config-form") {
      const section = form.dataset.section,
        payload = {};
      const controls = [...form.querySelectorAll("input[name],select[name]")];
      for (const el of controls) {
        const keys = el.name.split(".");
        let value =
          el.type === "checkbox"
            ? el.checked
            : el.type === "number"
              ? Number(el.value)
              : el.value;
        if (keys.at(-1) === "api_key" && !el.value) continue;
        if (keys.at(-1) === "input_modalities") value = el.value.split(",");
        if (el.name === "appearance.default_font_size") value = Number(value);
        if (keys.at(-1) === "enable_thinking")
          value = el.value === "null" ? null : el.value === "true";
        if (el.type !== "checkbox" && !el.value && keys.at(-1) !== "api_key")
          continue;
        let obj = payload;
        for (const key of keys.slice(0, -1)) obj = obj[key] ??= {};
        obj[keys.at(-1)] = value;
      }
      state.config = await api("/config", { method: "PUT", body: payload });
      state.adminDataHealth = null;
      state.error = "";
      render();
      toast("配置已保存，将用于后续任务。");
    } else if (form.id === "user-form") {
      const data = new FormData(form),
        id = form.dataset.id,
        payload =
          form.dataset.shared === "true"
            ? { role: data.get("role") }
            : {
                display_name: String(data.get("display_name") || ""),
                role: data.get("role"),
              };
      if (!id) payload.username = data.get("username");
      if (data.get("password")) payload.password = data.get("password");
      await api("/users" + (id ? "/" + encodeURIComponent(id) : ""), {
        method: id ? "PATCH" : "POST",
        body: payload,
      });
      state.users = normaliseList(await api("/users"));
      closeModal();
      render();
      toast(id ? "用户已更新" : "用户已创建");
    }
  } catch (e) {
    toast(e.message, true);
    if (button) button.disabled = false;
  }
}

function sourceSettings(state, icon) {
  const g = state.config.geography || {},
    rows = state.adminSourceChoices || [];
  return `<section class="panel"><h2>地名检索</h2><p>使用高德 Web 服务 Key 在后端查询候选地点，选中地点后保留原坐标与转换记录。</p><form id="config-form"><label class="field"><span>启用地名检索</span><input type="checkbox" name="geography.enabled" ${g.enabled ? "checked" : ""}></label>${secret("geography.api_key", g.api_key_configured)}${saveBar(icon)}</form></section><section class="panel"><div class="panel-heading"><h2>业务可选图源</h2><button class="secondary" data-action="admin-load-sources">读取真实目录</button></div><p>停用后不进入自动规划或用户选项。远端未配置的来源不会因为勾选而获得权限。</p><form id="source-settings-form"><div class="source-admin-table">${rows.map((r) => `<label class="source-choice"><input name="enabled_source" type="checkbox" value="${esc(r.source)}" ${r.enabled ? "checked" : ""}><span>${esc(r.label)} · ${esc(r.group_label)}<small>${r.available ? "远端已配置，覆盖需逐次核验" : "远端未配置"}</small></span></label>`).join("") || "<p>点击读取目录查看来源及实际配置。</p>"}</div>${rows.length ? saveBar(icon) : ""}</form></section>`;
}
