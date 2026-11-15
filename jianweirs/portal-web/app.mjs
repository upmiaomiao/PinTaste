import { createVoiceController, appendTranscript } from "./voice.mjs";
import { applyAppearance, shouldSend } from "./appearance.mjs";
import { profileButton, createProfileController } from "./profile.mjs";
import { SubmissionKeys } from "./submission-keys.mjs";
import {reviewText,readingText,renderReadableAnswer,renderCoreReport} from "./answer-layout.mjs";
import {renderConclusionLimits} from "./conclusion-limits.mjs";
import {conversationId,mergeTurns,upsertConversation} from "./conversation-state.mjs";
const submissionKeys = new SubmissionKeys();
import {
  normaliseRecommendations,
  TASK_TYPES,
  STAGES,
  STEP_LABELS,
  escapeHTML as esc,
  safeURL,
  readableText,
  formatDate,
  fileSize,
  parseCenter,
  normaliseList,
  taskTitle,
  isRunning,
  stageState,
  statusLabel,
  textParagraphs,
  taskPayload,
  selectWorkbookPoint,
  businessText,
  sourceLabel,
  candidateImageReferences,
  renderMarkdown,
  evidencePanelWidth,
  imageZoom,
  imageFit,
  scientificStatus,
  scientificWindows,
  activityProgress,
  sourceObservationLedger,
  evidenceSelection,
  EVIDENCE_GROUP_LABELS,
  evidenceTime,
  workflowStageTitle,
  screeningVerdictVisible,
  renderScienceAvailability,
} from "./lib.mjs";
import {
  renderAdminLazy,
  handleAdminActionLazy as handleAdminAction,
  handleAdminSubmitLazy as handleAdminSubmit,
  handleAdminModelInputLazy as handleAdminModelInput,
} from "./admin-loader.mjs?v=20261027-v14";
import { renderSearchSources, sourceCitations } from "./search-sources.mjs";
import {
  questionCard,
  nextQuestions,
  createWorkspaceController,
  renderDialogueTranscript,
} from "./workspace.mjs";
import {
  workEntries,
  renderWorkEntry,
  localMaterialLog,
  renderProcessingBlock,
} from "./worklog.mjs";
import {
  createComplaintController,
  renderScanCoverage,
} from "./complaints.mjs";
import {
  taskResults,
  renderTaskResults,
  evidenceGallery,
  consumerConclusion,
  modelBadge,
  thinkingBlock,
} from "./task-results.mjs";
import {
  workbookBatchModal,
  workbookBatchPayload,
  applyWorkbookProposal,
} from "./workbook.mjs";
import { portalBase, portalAsset, fontSize } from "./ui-config.mjs";
import { modelChip, modelPicker, modelPayload, selectedModel } from "./answer-models.mjs";

const paths = {
  minus: "M5 12h14",
  fit: "M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5M8 8h8v8H8",
  down: "M12 4v16m-6-6 6 6 6-6",
  plus: "M12 5v14M5 12h14",
  arrow: "M12 19V5m-6 6 6-6 6 6",
  chevron: "m9 5 7 7-7 7",
  close: "m6 6 12 12M6 18 18 6",
  menu: "M4 6h16M4 12h16M4 18h16",
  search: "m21 21-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
  chat: "M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2V11.5A8.5 8.5 0 0 1 10.5 3H13a8 8 0 0 1 8 8.5Z",
  pin: "M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0ZM15 10a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
  file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8ZM14 2v6h6M8 13h8M8 17h5",
  attach: "m21 11-9 9a6 6 0 0 1-9-9L13 2a4 4 0 0 1 6 6L9 18a2 2 0 0 1-3-3l9-9",
  globe:
    "M21 12A9 9 0 1 1 3 12a9 9 0 0 1 18 0ZM3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18",
  shield: "m12 3 9 4v6c0 5-9 9-9 9s-9-4-9-9V7Zm-4 9 3 3 5-6",
  logout: "M9 21H4V3h5M14 8l5 4-5 4M8 12h11",
  scan: "M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5M8 10l3-3 5 3v6H8Z",
  layers: "m12 3 10 6-10 6L2 9Zm-9 12 9 6 9-6M3 12l9 6 9-6",
  route: "M6 19a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm12-8a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM9 16h5a4 4 0 0 0 0-8h-1",
  frame: "M4 4h16v16H4ZM4 9h16M9 4v16",
  compare: "M12 3v18M3 6h6v12H3Zm12 0h6v12h-6",
  check: "m5 12 4 4L19 6",
  repeat: "M20 7H6l4-4M4 17h14l-4 4M20 7v6M4 17v-6",
  leaf: "M21 3C10 2 3 7 3 13a7 7 0 0 0 7 7c6 0 11-7 11-17ZM3 21 16 8",
  network: "M9 3h6v5H9Zm-7 13h6v5H2Zm14 0h6v5h-6M12 8v4M5 16v-4h14v4",
  clock: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM12 7v5l3 2",
  image: "M3 3h18v18H3Zm0 13 6-6 5 5 3-3 4 4M17 7h.01",
  expand: "M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5",
  download: "M12 3v12m-5-5 5 5 5-5M3 17v4h18v-4",
  cpu: "M7 7h10v10H7ZM3 9h4m10 0h4M3 15h4m10 0h4M9 3v4m6-4v4M9 17v4m6-4v4",
  spark: "M9 18h6m-5 3h4M12 3a6 6 0 0 0-4 10.5c.7.7 1 1.5 1 2.5h6c0-1 .3-1.8 1-2.5A6 6 0 0 0 12 3Z",
  users:
    "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM22 21v-2a4 4 0 0 0-3-4m-3-12a4 4 0 0 1 0 8",
  settings:
    "M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8ZM12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2",
  database:
    "M21 5c0 2-4 3-9 3S3 7 3 5s4-3 9-3 9 1 9 3ZM3 5v14c0 4 18 4 18 0V5M3 12c0 4 18 4 18 0",
  back: "M20 12H4m6-6-6 6 6 6",
  save: "M19 21H5a2 2 0 0 1-2-2V5l2-2h13l3 3v13a2 2 0 0 1-2 2ZM7 3v6h10V3M7 21v-8h10v8",
  refresh: "M20 7a9 9 0 1 0 1 7M20 3v5h-5",
  alert: "m12 3 10 18H2ZM12 9v4m0 4h.01",
};
export const icon = (name, extra = "") =>
  `<svg viewBox="0 0 24 24" aria-hidden="true" ${extra}><path d="${paths[name] || paths.file}"/></svg>`;
const root = document.querySelector("#app"),
  modalRoot = document.querySelector("#modal-root");
let taskDefaults = { search_policy: true };
let savedSidebarCollapsed = false;
try {
  savedSidebarCollapsed =
    localStorage.getItem("wuyu-sidebar-collapsed") === "true";
} catch {}
const state = {
  user: null,
  csrf: null,
  bootstrap: false,
  auth: null,
  preferences: {},
  usage: null,
  scanCache: {},
  route: "home",
  tasks: [],
  historyHasMore: false,
  historyOffset: 0,
  historyLoading: false,
  uploads: [],
  active: null,
  historyFilter: "",
  sidebar: false,
  sidebarCollapsed: savedSidebarCollapsed,
  busy: false,
  uploading: false,
  error: "",
  adminTab: "overview",
  config: null,
  users: [],
  adminOverview: null,
  ancestors: [],
  conversation: null,
  conversationTurns: [],
  conversationHasMore: false,
  conversationBefore: null,
  conversationLoading: false,
  evidenceTaskId: null,
  evidenceOpen: true,
  evidenceWidth: 460,
  evidenceTab: "key",
  evidenceComparison: 0,
  detailState: {},
  draft: newDraft(),
  guide: null,
  materialEvents: [],
  dialogueTurns: [],
  dialoguePending: false,
  clientEventsByTask: {},
};
const profileTools = createProfileController(state, {
  api,
  icon,
  showModal,
  render,
  updateComposer,
});
const voiceTools = createVoiceController(state, {
  notice: toast,
  onChange: () => updateComposer(),
  onText: (text) => {
    const result = appendTranscript(state.draft.objective, text);
    state.draft.objective = result.text;
    updateComposer();
    if (result.truncated) toast("语音文字超出输入上限，请分段录入。", true);
  },
});
applyAppearance();
function resetPrivateView() {
  workspaceTools.reset();
  voiceTools.close();
  state.guide = null;
  state.dialogueTurns = [];
  state.dialoguePending = false;
  state.materialEvents = [];
  state.clientEventsByTask = {};
  state.scanCache = {};
  state.tasks = [];
  state.uploads = [];
  state.ancestors = [];
  state.conversation = null;
  state.conversationTurns = [];
  state.conversationHasMore = false;
  state.conversationBefore = null;
  state.conversationLoading = false;
  state.evidenceTaskId = null;
  state.active = null;
  state.historyHasMore = false;
  state.historyOffset = 0;
  state.draft = newDraft();
  state.answerModels = null;
  state.busy = false;
}
function newDraft(previous = {}) {
  return {
    objective: "",
    center: "",
    task_types: [],
    search_policy: taskDefaults.search_policy ?? true,
    search_touched: false,
    // The model choice is a composer preference and survives a new chat.
    answer_model: previous.answer_model || "jianwei",
    deep_thinking: previous.deep_thinking ?? true,
    files: [],
    workbook_record: null,
  };
}
let pollTimer = null,
  loadGeneration = 0;
export async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (
    options.body &&
    !(options.body instanceof FormData) &&
    !(options.body instanceof Blob)
  ) {
    headers.set("Content-Type", "application/json");
    options.body = JSON.stringify(options.body);
  }
  if (!["GET", "HEAD"].includes(options.method || "GET") && state.csrf)
    headers.set("X-CSRF-Token", state.csrf);
  const submission = submissionKeys.begin(
    state.user?.id,
    path,
    options.method || "GET",
    options.body,
  );
  if (submission) headers.set("Idempotency-Key", submission.key);
  const response = await fetch(portalBase() + "/portal/api" + path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  submissionKeys.finish(submission, response);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message =
      typeof data.detail === "string"
        ? data.detail
        : typeof data.detail?.message === "string"
          ? data.detail.message
          : typeof data.error === "string"
          ? data.error
          : typeof data.message === "string"
            ? data.message
            : response.status === 401
              ? "登录已过期，请重新登录。"
              : `请求未完成（${response.status}）`;
    if (response.status === 401 && state.user) {
      submissionKeys.clear();
      resetPrivateView();
      state.user = null;
      state.csrf = null;
      state.scanCache = {};
      state.preferences = {};
      applyAppearance();
      clearTimeout(pollTimer);
      closeModal();
      render();
    }
    const error = new Error(
      [429, 503].includes(response.status) && !/稍后|重试/.test(message)
        ? message + "（系统较忙，请稍后再试）"
        : message,
    );
    error.status = response.status;
    error.retryAfter = Number(response.headers.get("Retry-After")) || null;
    throw error;
  }
  if (path === "/config" && data.task_policy)
    taskDefaults = { search_policy: data.task_policy.search_policy };
  if (
    path === "/config" &&
    data.appearance &&
    state.preferences.font_size == null
  )
    document.documentElement.style.setProperty(
      "--portal-font-size",
      fontSize(data.appearance.default_font_size) + "px",
    );
  return data;
}
function brand(small = false) {
  return `<div class="brand"><img src="${portalAsset("assets/logo.png")}" alt="无隅"><div><strong>无隅见微</strong>${small ? "" : "<small>遥感 Harness 系统</small>"}</div></div>`;
}

function toast(message, error = false) {
  const node = document.createElement("div");
  node.className = "toast" + (error ? " error" : "");
  node.textContent = message;
  document.querySelector("#toast-region").append(node);
  setTimeout(() => node.remove(), 4500);
}
function errorBox() {
  return state.error
    ? `<div class="error-box" role="alert">${icon("alert")}<span>${esc(state.error)}</span></div>`
    : "";
}
function authView() {
  if (state.auth?.mode === "wuyu") return platformAuthView();
  return `<div class="auth-page"><header class="auth-header">${brand()}<span class="pill">固废非法倾倒监管</span></header><main class="auth-layout"><section class="auth-art"><div class="auth-art-copy"><span class="eyebrow">WUYU · REMOTE SENSING</span><h1>多星观测<br>让监管有据可依。</h1><p>从疑点发现到持续跟踪，连接多源遥感影像、专业识别与监管资料。</p></div><div class="orbit"><span class="orbit-point"></span><span class="orbit-point"></span><img class="brand-hero" src="${portalAsset("assets/logo.png")}" alt=""></div><div class="auth-features">${[
    ["layers", "多源证据", "影像选择与来源可追溯"],
    ["scan", "专业研判", "见微识别，按需追加复核"],
    ["file", "完整交付", "标注影像与专业报告"],
  ]
    .map(
      ([i, t, d]) =>
        `<div class="auth-feature">${icon(i)}<div><strong>${t}</strong><small>${d}</small></div></div>`,
    )
    .join(
      "",
    )}</div></section><section class="auth-right"><div class="auth-box"><span class="eyebrow">${state.bootstrap ? "首次启用" : "欢迎回来"}</span><h2>${state.bootstrap ? "创建管理员账号" : "登录无隅见微"}</h2><p class="auth-subtitle">${state.bootstrap ? "设置工作台的首位管理员，随后可在管理端创建业务用户。" : "使用管理员为你开通的账号，开始遥感监管任务。"}</p>${errorBox()}<form id="auth-form" class="auth-form">${state.bootstrap ? '<label class="field">姓名或显示名称<input name="display_name" autocomplete="name" maxlength="100" placeholder="请输入显示名称"></label>' : ""}<label class="field">用户名<input name="username" required autocomplete="username" minlength="3" maxlength="80" placeholder="请输入用户名" autofocus></label><label class="field">密码<input type="password" name="password" required autocomplete="${state.bootstrap ? "new-password" : "current-password"}" minlength="${state.bootstrap ? "10" : "1"}" maxlength="128" placeholder="${state.bootstrap ? "至少 10 个字符" : "请输入密码"}"></label>${state.bootstrap ? '<label class="field">确认密码<input type="password" name="confirm" required autocomplete="new-password" placeholder="再次输入密码"></label>' : ""}<button class="primary" ${state.busy ? "disabled" : ""}>${state.busy ? '<span class="spin"></span>' : icon(state.bootstrap ? "shield" : "arrow")}${state.busy ? "正在连接…" : state.bootstrap ? "创建账号并进入工作台" : "登录工作台"}</button></form><p class="auth-footnote">${state.bootstrap ? "管理员可统一管理模型、数据、搜索及用户权限。" : "账号由系统管理员管理。若无法登录，请联系管理员。"}</p></div></section></main><footer class="auth-footer">无隅见微遥感Harness系统 · 多星协同监管 <span>以证据支持判断，以复核明确疑点</span></footer></div>`;
}
function platformAuthView() {
  const login = safeURL(state.auth?.login_url),
    register = safeURL(state.auth?.register_url);
  return `<div class="auth-page platform-auth"><header class="auth-header">${brand()}<span class="pill">固废遥感工作台</span></header><main class="auth-layout"><section class="auth-right"><div class="auth-box"><span class="eyebrow">WUYU · JIANWEI</span><h2>登录后，开始遥感核查</h2><p class="auth-subtitle">使用无隅 AI 平台账号进入见微工作台。首次使用请前往平台注册，登录后会返回这里。</p>${errorBox()}<div class="platform-auth-actions">${login ? `<a class="primary" href="${esc(login)}">${icon("arrow")}前往无隅 AI 登录</a>` : '<p role="status">统一登录入口正在连接，请稍后刷新。</p>'}${register ? `<a class="secondary" href="${esc(register)}">注册无隅 AI 账号</a>` : ""}</div><p class="auth-footnote">账号与无隅 AI 平台通用；见微任务、图源、问答额度和界面设置独立管理。</p></div></section></main></div>`;
}
function history() {
  const tasks = state.tasks.filter((t) =>
    taskTitle(t).toLowerCase().includes(state.historyFilter.toLowerCase()),
  );
  const items = tasks.length
    ? tasks
        .map(
          (t) =>
            `<button class="history-item ${state.active && conversationId(state.active) === conversationId(t) ? "active" : ""}" data-action="task" data-id="${esc(t.latest_task_id||t.id)}">${icon(isRunning(t) ? "clock" : "chat")}<div><strong>${esc(taskTitle(t))}</strong><small>${formatDate(t.created_at || t.created)} · ${t.turn_count?`${Number(t.turn_count)} 轮 · `:""}${statusLabel(t.status)}</small></div></button>`,
        )
        .join("")
    : `<div class="sidebar-empty">${icon("chat")}<span>${state.historyFilter ? "已加载会话中没有匹配结果" : "暂无历史会话"}</span></div>`;
  return (
    items +
    (state.historyHasMore
      ? `<button class="history-more secondary" data-action="history-more" ${state.historyLoading ? "disabled" : ""}>${state.historyLoading ? "正在加载…" : "加载更早的会话"}</button>`
      : "")
  );
}
function shell() {
  const t = state.active,
    shownEvidence = evidenceTask(),
    images = shownEvidence?.images || [],
    contextual = (shownEvidence?.workflow_mode || shownEvidence?.task?.workflow_mode) === "contextual_qa",
    hasEvidence = !!t && (!contextual || images.length > 0 || shownEvidence.scientific_evidence?.length > 0 || shownEvidence.task?.document_ids?.length > 0),
    evidenceVisible = hasEvidence && state.evidenceOpen,
    home = state.route === "home";
  return `<div class="app-shell ${state.sidebarCollapsed ? "sidebar-collapsed" : ""}"><aside class="sidebar ${state.sidebar ? "open" : ""}">${brand(true)}<button class="icon-button sidebar-collapse" data-action="sidebar-collapse" aria-label="${state.sidebarCollapsed ? "展开左侧工作区" : "收起左侧工作区"}" title="${state.sidebarCollapsed ? "展开左侧工作区" : "收起左侧工作区"}">${icon("frame")}</button><button class="icon-button sidebar-close" data-action="menu" aria-label="收起侧栏">${icon("close")}</button><nav class="sidebar-main"><button class="sidebar-link new-task" data-action="new" title="新会话" aria-label="新会话">${icon("plus")}新会话</button></nav><div class="history-heading">历史会话 <button class="icon-button" data-action="library" title="资料库" aria-label="资料库">${icon("file")}</button></div><label class="history-search">${icon("search")}<input id="history-search" type="search" value="${esc(state.historyFilter)}" placeholder="搜索历史会话" aria-label="搜索历史会话"></label><div class="history-list" id="history-list">${history()}</div><div class="sidebar-bottom"><button class="sidebar-link" data-action="library" title="全部资料" aria-label="全部资料">${icon("file")}全部资料</button>${state.user.role === "admin" ? `<button class="sidebar-link" data-action="admin" title="系统管理" aria-label="系统管理">${icon("settings")}系统管理</button>` : ""}<div class="account">${profileButton(state.user, icon)}</div></div></aside><section class="workspace"><header class="topbar"><button class="icon-button mobile-menu" data-action="menu" aria-label="展开侧栏">${icon("menu")}</button><div class="product-heading"><strong>无隅见微遥感Harness系统</strong></div><div class="topbar-right">${t ? `<button class="progress-pill" data-action="latest"><span class="status-dot ${isRunning(t) ? "live" : ""}"></span>${isRunning(t) ? "正在工作" : statusLabel(t.status)}</button><button class="secondary evidence-toggle" data-action="evidence-toggle" aria-expanded="${evidenceVisible}">${icon("image")}${evidenceVisible ? "收起证据" : "影像与资料"}${images.length ? ` <span>${images.length}</span>` : ""}</button>` : ""}</div></header>${t ? `<div class="task-heading">${esc(state.conversation?.title || t.conversation_title || taskTitle(t))}</div>` : ""}<div class="conversation-layout ${evidenceVisible ? "with-evidence" : ""}" style="--evidence-width:${state.evidenceWidth}px"><section class="chat-main ${home ? "home-workspace" : ""}"><div class="chat-scroll main-scroll" id="chat-scroll"><div class="chat-thread">${state.route === "task" ? taskView() : homeView()}</div></div><div class="composer-dock"><button class="scroll-latest icon-button" data-action="latest" aria-label="回到最新内容" title="回到最新内容">${icon("down")}</button>${errorBox()}<div id="question-dock">${questionCard(state.guide)}</div><div id="composer-area">${composer(state.route === "task")}</div>${home && !state.guide && !state.dialogueTurns.length ? starterCards() : ""}</div></section>${evidenceVisible ? `<div class="evidence-resizer" role="separator" aria-label="调整证据栏宽度" aria-orientation="vertical" tabindex="0" aria-valuemin="280" aria-valuemax="800" aria-valuenow="${state.evidenceWidth}"></div><aside class="evidence-sidebar"><header class="evidence-header"><strong>影像与资料</strong><button class="icon-button" data-action="evidence-toggle" aria-label="收起证据栏" title="收起证据栏">${icon("close")}</button></header><div class="evidence-scroll" id="evidence-scroll">${evidenceView()}</div></aside>` : ""}</div></section></div><input type="file" id="file-input" multiple hidden accept=".zip,.png,.jpg,.jpeg,.webp,.tif,.tiff,.pdf,.docx,.xlsx,.csv,.txt,.md,.json">`;
}

function composer(followup = false) {
  return `<form id="task-form" class="composer">${state.draft.files.length ? `<div class="file-chips">${state.draft.files.map((f, i) => `<span class="file-chip">${icon(f.kind === "image" || f.asset_id ? "image" : "file")}<span>${esc(f.filename || f.name || "已上传资料")}</span>${/\.zip$/i.test(f.filename || f.name || "") ? `<button type="button" class="text-button" data-action="archive-open" data-id="${esc(f.id)}">选择图片${f.assets?.length ? " · " + f.assets.length : ""}</button>` : ""}${/\.xlsx$/i.test(f.filename || f.name || "") ? `<button type="button" class="text-button" data-action="workbook" data-id="${esc(f.id)}">选择点位</button>` : ""}<button type="button" class="icon-button" data-action="remove-file" data-index="${i}" aria-label="移除附件">${icon("close")}</button></span>`).join("")}</div>` : ""}${state.uploading ? '<div class="uploading-label"><span class="spin"></span>正在接收并检查材料…</div>' : ""}${voiceTools.panel()}<textarea id="objective" name="objective" rows="2" maxlength="12000" required aria-label="输入问题" ${state.guide?.question ? "disabled" : ""} placeholder="${state.guide?.question ? "请先在上方的问题卡片里回答，再继续输入" : followup ? "补充证据、修改要求，或继续追问…" : "想核查哪个地点？可以上传影像、压缩包或疑似点位表…"}">${esc(state.draft.objective)}</textarea><input type="hidden" id="center" name="center" value="${esc(state.draft.center)}"><div class="composer-meta"><div class="composer-tools"><button type="button" class="composer-chip" data-action="location-open">${icon("pin")}地点${state.draft.center ? " · 已选" : ""}</button><button type="button" class="composer-chip" data-action="sources-open">${icon("layers")}图源${state.draft.selected_sources?.length ? " · " + state.draft.selected_sources.length : " · 按需"}</button><button type="button" class="composer-chip ${state.draft.search_policy ? "selected" : ""}" data-action="search-toggle" aria-pressed="${state.draft.search_policy}">${icon("globe")}联网检索${state.draft.search_policy ? icon("check") : ""}</button>${modelChip(state.answerModels, state.draft, icon)}</div><div class="composer-send-group">${voiceTools.button()}<button type="button" class="composer-attachment icon-button" data-action="upload" title="添加影像或资料" aria-label="上传材料" ${state.uploading ? "disabled" : ""}>${icon("attach")}</button>${isRunning(state.active) ? `<button type="button" class="stop-button" data-action="stop-task" aria-label="停止当前任务"><span></span>停止</button>` : ""}<button type="submit" class="composer-send" title="${isRunning(state.active) ? "发送新要求（会先询问是否停止当前核查）" : state.preferences.send_key === "ctrl_enter" ? "发送（Ctrl / ⌘ + Enter）" : "发送（Enter）"}" aria-label="${isRunning(state.active) ? "发送新要求" : "发送问题"}" ${state.busy || state.uploading || state.dialoguePending || state.guide?.pending || state.guide?.question || !state.draft.objective.trim() ? "disabled" : ""}>${state.busy ? '<span class="spin"></span>' : icon("arrow")}</button></div></div></form>`;
}

function homeView() {
  if (state.dialogueTurns.length)
    return `${renderDialogueTranscript(state.dialogueTurns)}${state.dialoguePending ? '<p class="dialogue-pending" role="status">我正在整理你的要求…</p>' : ""}${localMaterialLog(state.materialEvents, icon)}`;
  return `<section class="empty-chat"><div class="welcome-title"><img src="${portalAsset("assets/logo.png")}" alt=""><h1>欢迎使用 <em>无隅见微</em>，<br class="mobile-break">今天想核查什么？</h1></div><p>从一个地名、一幅影像或一张点位表开始。</p></section>${localMaterialLog(state.materialEvents, icon)}`;
}
function starterCards() {
  return `<section class="starter-cards" aria-label="系统支持的固废遥感任务">${[
    [
      "scan",
      "点位核查",
      "发现疑似点位、甄别固废类型、判读堆存范围",
      "请帮我核查一个区域的疑似固废堆存，识别可见类型和范围，并排查建筑、裸地等误识别。",
    ],
    [
      "compare",
      "举报核实",
      "从举报线索确认范围，逐步取证并核实",
      "我有一条固废堆存举报线索，请先帮我整理线索，通过对话确认地点、范围和时间，再进行多星核查并形成举报核实报告。",
    ],
    [
      "leaf",
      "环境风险",
      "评估已确认堆存的风险等级、影响路径与管理要求",
      "这处固废堆存已经现场确认。请结合我提供的资料和影像，评估环境风险等级、污染迁移路径与成因，进行初评和全面复核；如我启用联网，再对照国家和地方管理要求。",
    ],
    [
      "file",
      "时序变化",
      "查找最早出现和最近仍存在的时段，追踪清理变化",
      "我想查看这个点位从过去到最近的变化，判断堆体最早何时出现、最近何时仍存在，以及是否清理或反复倾倒。请按实际拍摄日期和同点证据给出时序报告。",
    ],
  ]
    .map(
      ([i, t, d, p], n) =>
        `<button class="starter-card starter-${n}" data-action="starter" data-prompt="${esc(p)}"><span class="starter-icon">${icon(i)}</span><strong>${t}</strong><small>${d}</small></button>`,
    )
    .join(
      "",
    )}</section><p class="capability-note">支持地名、坐标、自有影像和批量点位表。每类任务分别形成结论与报告，也可继续追问已有证据。</p>`;
}

const business = (value,task=state.active) => reviewText(businessText(value, task?.images || []));
function findingReferences(candidate, pkg,task=state.active) {
  const refs = candidateImageReferences(
    candidate,
    pkg.recognition,
    task?.images || [],
  );
  return refs.length
    ? `<p class="evidence-reference">来源影像：${refs.map((ref) => `<a href="${ref.anchor}" data-action="evidence-focus" data-index="${ref.number - 1}">${ref.label}</a>`).join("、")}</p>`
    : "";
}
function taskPackage(t = state.active || {}) {
  return t.package || t.result || t.report || t;
}
function stageDetails(index, pkg) {
  const outlines =
    pkg.planner_outline ||
    pkg.plan?.task_outline ||
    (Array.isArray(pkg.task_outline) ? pkg.task_outline : []);
  if (index === 0) {
    if (outlines.length)
      return `<ol class="outline-steps">${outlines.map((v) => `<li><strong>${esc(readableText(v.title || ""))}</strong><p>${esc(business(v.summary || v))}</p></li>`).join("")}</ol>`;
    if (pkg.task_outline) {
      const v = pkg.task_outline;
      return [v.objective, ...(v.questions || [])]
        .filter(Boolean)
        .slice(0, 6)
        .map((v) => `<p>${esc(readableText(v))}</p>`)
        .join("");
    }
  }
  if (index === 1) {
    const ranking = pkg.selection?.ranking || [],
      phase = outlines.find((v) => v.id === "source_selection");
    return `${phase ? `<p>${esc(business(phase.summary))}</p>` : ""}`;
  }
  if (index === 2) {
    const a = pkg.adaptive_review;
    if (a?.triggered)
      return `<p>${esc(a.trigger_reason || "已触发追加复核")}${a.requests?.length ? `，涉及 ${a.requests.length} 项取证请求` : ""}。</p>${(a.requests || []).map((r) => `<p><strong>${esc(sourceLabel(r.source))}</strong> ${r.status === "completed" ? "已获取复核证据" : "本次未获得可用复核证据"}</p>`).join("")}${a.local_crops?.length ? `<p>已生成 ${a.local_crops.length} 张局部证据图用于复核。</p>` : ""}${a.main_visual_review?.status === "completed" ? `<p><strong>视觉交叉复核</strong> ${esc(business(a.main_visual_review.assessment || ""))}</p>` : ""}`;
  }
  return "";
}
function evidenceTask() {
  return state.conversationTurns.find(turn=>turn.id===state.evidenceTaskId) || state.active || {};
}
function coreArtifacts(task) {
  const original=(task.artifacts||[]).filter(file=>!['report.html','report.docx','report.md'].includes(file.name));
  return task.core_report?.artifacts ? [...original,...task.core_report.artifacts] : task.artifacts||[];
}
function executionRecords(t, pkg, latest=false) {
  const rendered=renderProcessingBlock(
    t,
    icon,
    state.clientEventsByTask[t.id] || [],
    progressText(t),
  );
  return latest?rendered:rendered.replace(/(<details\b[^>]*?)\sopen(>)/,'$1$2');
}

function baselineSummary(pkg,task=state.active) {
  const business=value=>reviewText(businessText(value,task?.images||[]));
  const baseline = pkg.selection?.baseline,
    decisions = pkg.adaptive_review?.coverage_decisions || [];
  if (!baseline && !decisions.length) return "";
  return `<section class="trace-detail"><h3>影像获取与覆盖</h3>${baseline ? `<p>已取得 ${Array.isArray(baseline.acquired_source_groups) ? baseline.acquired_source_groups.length : Number(baseline.acquired_source_groups) || 0} 组来源影像${baseline.target_source_groups ? `，目标 ${Number(baseline.target_source_groups)} 组` : ""}。</p>${(baseline.shortfalls || []).map((x) => `<p>${esc(business(x))}</p>`).join("")}` : ""}${decisions.map((row) => `<p><strong>${esc(sourceLabel(row.source))}</strong>：${esc(business(row.reason || row.issue))}${row.outcome ? `；${esc(business(row.outcome))}` : `；${{ completed: "已取得补充影像", planned: "待执行", not_attempted: "本次未追加", unavailable: "未取得补充影像", skipped_budget: "本次未追加", disabled_by_user: "未启用追加", skipped_duplicate: "已有相同影像，未重复获取" }[row.status] || "处理状态待确认"}`}${row.result_asset_ids?.length ? `（${row.result_asset_ids.map((id) => business(id)).join("、")}）` : ""}</p>`).join("")}</section>`;
}
function taskView() {
  if (!state.active)
    return '<div class="empty-state"><span class="spin"></span>我来读取这次的材料…</div>';
  const turns=mergeTurns(state.conversationTurns,[state.active]);
  return `${state.conversationHasMore?`<button class="conversation-more secondary" data-action="conversation-more" ${state.conversationLoading?'disabled':''}>${state.conversationLoading?'正在读取…':'加载更早的对话'}</button>`:''}${turns.map(turn=>renderConversationTurn(turn,turn.id===state.active.id)).join('')}`;
}
function renderConversationTurn(t,latest=false) {
  const business=value=>reviewText(businessText(value,t.images||[]));
  const pkg = taskPackage(t),
    summary = business(t.summary || pkg.narrative || ""),
    fusion = t.fusion || pkg.fusion,
    findings = t.findings || pkg.recognition?.candidates || [],
    recommendations = normaliseRecommendations(
      t.narrative?.recommendations || pkg.narrative?.recommendations || [],
    ),
    focused = renderTaskResults(t, icon),
    narrative = t.narrative || pkg.narrative || {},
    presentation = t.consumer_conclusion?.presentation || t.task_results?.consumer_conclusion?.presentation || pkg.consumer_conclusion?.presentation || t.task_results?.presentation || pkg.task_results?.presentation,
    specialized = !!presentation?.enabled,
    contextual = (presentation?.workflow_mode || t.workflow_mode || t.task?.workflow_mode) === "contextual_qa";
  const blocks = {
    scan: renderScanCoverage(t.scan_id ? state.scanCache?.[t.scan_id] : null, t.scan_id, icon),
    process: executionRecords(t, pkg, latest),
    error: t.error ? `<div class="error-box" role="alert">${icon("alert")}<span>${esc(business(t.error))}</span></div>` : "",
    fusion: !focused && fusion && !summary ? renderFusion(fusion, t) : "",
    analysis: summary && !specialized
      ? (focused
        ? `<details class="supporting-details narrative-context" data-ui-state="narrative-${esc(t.id)}"><summary>完整分析与证据说明${icon("chevron")}</summary><div class="answer">${renderReadableAnswer(sourceCitations(summary, t.policy || pkg.policy, t.id),{id:t.id})}</div></details>`
        : `${modelBadge(narrative.answer_model) ? `<div class="answer-model-line">${modelBadge(narrative.answer_model)}</div>` : ""}${thinkingBlock(narrative.reasoning, t.id, icon)}<section class="answer">${renderReadableAnswer(sourceCitations(summary, t.policy || pkg.policy, t.id),{id:t.id})}</section>`)
      : isRunning(t) ? `<div class="answer-pending" role="status"><span class="loading-dot"></span>${t.queue_position ? `前面还有 ${Math.max(0, t.queue_position - 1)} 个任务在排队，轮到后会自动开始…` : "我正在核对证据…"}</div>` : "",
    recommendations: !specialized && recommendations.length
      ? `<section class="turn-layer-block"><h3>核查建议</h3><div class="markdown-body">${renderMarkdown(recommendations.map((x) => "- " + business(x)).join("\n"))}</div></section>` : "",
    findings: !specialized && findings.length
      ? `<section class="turn-layer-block"><h3>影像识别线索 <span>${findings.length}</span></h3>${findings.map((f, i) => `<section class="finding-card"><h4>${esc(f.label || f.title || `疑点 ${i + 1}`)}</h4>${findingReferences(f, pkg,t)}<div class="markdown-body">${renderMarkdown(business(f.description || f.evidence || f.rationale || f.reason || (f.evidence_indices || []).map((index) => pkg.recognition?.observations?.[index]?.statement).filter(Boolean)))}</div>${f.alternative_explanations?.length ? `<p>需排除：${esc(business(f.alternative_explanations))}</p>` : ""}${f.verification?.length ? `<p>${esc(business(f.verification))}</p>` : ""}${typeof f.confidence === "number" ? `<details class="model-score"><summary>识别参考详情</summary><p>模型自报参考分 ${Number(f.confidence).toFixed(2)}，未经校准，不代表倾倒或违法概率。</p></details>` : ""}</section>`).join("")}</section>` : "",
    next: latest && !isRunning(t) && !state.guide
      ? renderNextSteps(nextQuestions(t)) : "",
    trace: !contextual && !isRunning(t) ? `<section class="turn-layer-block"><h3>数据获取与选择</h3>${baselineSummary(pkg,t)}${renderSourceDetails(pkg,t)}</section>` : "",
    ledger: contextual || specialized ? "" : evidenceGallery(t, [], icon),
    sources: renderTurnSources(t, pkg),
    report: (t.core_report || (t.artifacts || []).some((x) => ["report.docx", "report.html"].includes(x.name))) && !isRunning(t)
      ? renderReportCards(t, latest) : "",
  };
  // 三层：① 结论（始终展开）② 证据与依据（一个折叠）③ 处理过程（一个折叠）。
  const evidenceLayer = [blocks.recommendations, blocks.findings, blocks.ledger, blocks.sources, contextual ? "" : renderConclusionLimits(t, icon)].filter(Boolean).join("");
  const processLayer = [blocks.process, blocks.trace].filter(Boolean).join("");
  const layer = (name, title, content, open = false) => content
    ? `<details class="supporting-details turn-layer turn-layer-${name}" data-ui-state="layer-${name}-${esc(t.id)}"${open ? " open" : ""}><summary>${title}${icon("chevron")}</summary>${content}</details>` : "";
  return `<article class="conversation-turn ${latest?"latest-turn":"historical-turn"}" data-task-id="${esc(t.id)}"><div class="user-message">${renderMarkdown(t.objective || t.task?.objective || t.request?.objective || taskTitle(t))}</div><div class="assistant-label"><img src="${portalAsset("assets/logo.png")}" alt=""><strong>无隅见微 · 遥感专家</strong></div>${layer("process", "处理过程", processLayer, isRunning(t))}${blocks.error}${blocks.scan}${focused}${blocks.fusion}${blocks.analysis}${blocks.next}${layer("evidence", "证据与依据", evidenceLayer)}${blocks.report}<div class="answer-time">${formatDate(t.updated_at || t.created_at, true)}</div></article>`;
}
function renderNextSteps(questions) {
  if (!questions.length) return "";
  const clean = (q) => String(q).replace(/^继续核查[：:]\s*/, "").trim();
  const split = (q) => { const text = clean(q), cut = text.search(/[，,：:；;。]/); return cut > 4 && cut <= 28 ? [text.slice(0, cut), text.slice(cut + 1).trim()] : [text, ""]; };
  return `<section class="next-steps" aria-label="下一步建议与计划"><h3>${icon("route")}下一步建议与计划<span>点选即可继续追问</span></h3><ol class="next-steps-list">${questions.map((q, i) => { const [title, detail] = split(q); return `<li><button type="button" class="next-step" data-action="next-question" data-index="${i}"><span class="next-step-no">${String(i + 1).padStart(2, "0")}</span><span class="next-step-text"><strong>${esc(title)}</strong>${detail ? `<small>${esc(detail)}</small>` : ""}</span><span class="next-step-go" aria-hidden="true">${icon("chevron")}</span></button></li>`; }).join("")}</ol></section>`;
}
function renderReportCards(t, latest) {
  const files = coreArtifacts(t).filter((x) => ["report.docx", "report.html"].includes(x.name) && safeURL(x.url));
  const kinds = { "report.docx": ["word", "W", "Word 报告", "可下载、可编辑，含图文证据"], "report.html": ["html", "网", "在线报告", "浏览器内直接阅读，含图文证据"] };
  const cards = files.map((x) => { const [kind, mark, name, note] = kinds[x.name]; return `<a class="report-card report-${kind}" href="${esc(safeURL(x.url))}" target="_blank" rel="noopener"><span class="report-logo">${mark}</span><span class="report-text"><strong>${name}</strong><small>${note}</small></span><span class="report-go">${x.name === "report.docx" ? "下载" : "查看"}${icon("chevron")}</span></a>`; }).join("");
  return `<section class="report-cards" aria-label="本会话报告"><h3>${icon("file")}${latest ? "本会话报告" : "本轮报告"}</h3><div class="report-card-row">${cards}<button type="button" class="report-card report-all" data-action="open-reports" data-task-id="${esc(t.id)}"><span class="report-logo">${icon("layers")}</span><span class="report-text"><strong>全部报告与成果</strong><small>在右侧“报告”栏随时查看</small></span><span class="report-go">打开${icon("chevron")}</span></button></div></section>`;
}
function renderTurnSources(t,pkg) {
  const html=renderSearchSources(t.policy||pkg.policy,t.narrative?.cited_policy_ids||pkg.narrative?.cited_policy_ids||[],t.id);
  return html?`<details class="supporting-details turn-sources" data-ui-state="turn-sources-${esc(t.id)}"><summary>参考资料与出处${icon('chevron')}</summary>${html}</details>`:'';
}
function renderAnswerArtifacts(items) {
  const visible = items.filter(
    (x) => safeURL(x.url) && ["report.docx", "report.html"].includes(x.name),
  );
  return visible.length
    ? `<div class="answer-artifacts">${visible.map((x) => `<a href="${esc(safeURL(x.url))}" target="_blank" rel="noopener">${icon("file")}${x.name === "report.docx" ? "下载 Word 报告" : "查看完整报告"}</a>`).join("")}</div>`
    : "";
}
function evidenceView() {
  const t = evidenceTask(),
    pkg = taskPackage(t),
    selected = evidenceSelection(
      t,
      state.evidenceTab,
      state.evidenceComparison,
    ),
    plan = selected.presentation;
  if ((t.research_result || pkg.research_result)?.mode === "availability")
    return (
      scienceEvidence(t.scientific_evidence || pkg.scientific_evidence || []) +
      renderArtifacts(coreArtifacts(t))
    );
  const tabs = ["key", ...plan.groups],
    comparison = selected.visible.length > 1;
  const activeGroup = selected.activeComparison;
  return `<section class="evidence-panel key-evidence"><div class="evidence-panel-heading"><h2>关键证据</h2><span>${selected.visible.length} 张${selected.additional.length ? ` · 另 ${selected.additional.length} 张` : ""}</span></div>${plan.groups.length ? `<nav class="evidence-tabs" aria-label="证据类型">${tabs.map((tab) => `<button type="button" data-action="evidence-tab" data-tab="${tab}" aria-pressed="${selected.tab === tab}" class="${selected.tab === tab ? "active" : ""}">${esc(EVIDENCE_GROUP_LABELS[tab])}</button>`).join("")}</nav>` : ""}${selected.tab === "key" && plan.mode === "comparison" && plan.comparison_groups.length > 1 ? `<nav class="evidence-comparison-tabs" aria-label="证据对照组">${plan.comparison_groups.map((group, index) => `<button data-action="evidence-comparison" data-index="${index}" aria-pressed="${selected.comparisonIndex === index}">${esc(group.title)}</button>`).join("")}</nav>` : ""}${activeGroup ? `<h3 class="comparison-heading">${esc(activeGroup.title)}</h3>` : ""}${activeGroup?.reason || plan.reason ? `<p class="evidence-selection-reason">${esc(business(activeGroup?.reason || plan.reason))}</p>` : ""}${selected.visible.length ? `<div class="evidence-display ${comparison ? "comparison" : "single"}">${selected.visible.map((row) => renderImage(row.image, row.index)).join("")}</div>` : `<div class="no-evidence">${icon("image")}<p>${isRunning(t) ? "影像就绪后显示在这里。" : selected.additional.length ? "尚无通过位置核验的关键目标证据；可切换类型或展开其他影像作背景参考。" : "本次没有可展示的影像。"}</p></div>`}${selected.additional.length ? `<details class="supporting-details additional-evidence" data-ui-state="additional-${esc(t.id)}-${selected.tab}"><summary>其他${selected.tab === "key" ? "证据" : EVIDENCE_GROUP_LABELS[selected.tab]}影像 <span>${selected.additional.length}</span>${icon("chevron")}</summary>${selected.additional.map((row) => renderImage(row.image, row.index)).join("")}</details>` : ""}</section>${renderArtifacts(coreArtifacts(t))}${scienceEvidence(t.scientific_evidence || pkg.scientific_evidence || [])}`;
}
function scienceEvidence(items) {
  if (!items.length) return "";
  return `<details class="supporting-details science-evidence" data-ui-state="science"><summary>卫星数据与分析 <span>${items.length}</span>${icon("chevron")}</summary>${items
    .map((item) => {
      const windows = scientificWindows(item),
        downloaded = windows.some(
          (window) => window.status === "science_window_downloaded",
        ),
        analysis = item.analysis || {};
      return `<section><h3>${esc(sourceLabel(item.source))}</h3><p>${esc(scientificStatus(item))}</p>${downloaded && !item.science_pixels_analyzed ? "<p>尚未开展定量像素分析。</p>" : ""}${item.time_range ? `<p>检索时段：${esc(item.time_range)}</p>` : ""}${renderScienceAvailability(item)}${analysis.status === "completed" ? `<div class="markdown-body">${renderMarkdown(business(analysis.summary || ""))}</div>${analysis.uncertainties?.length ? `<ul>${analysis.uncertainties.map((value) => `<li>${esc(business(value))}</li>`).join("")}</ul>` : ""}` : ""}${windows
        .map((window, index) => {
          const links = [
              ...(window.artifact_url
                ? [
                    {
                      artifact: window.artifact,
                      artifact_url: window.artifact_url,
                    },
                  ]
                : []),
              ...(window.artifacts || []),
            ],
            seen = new Set();
          return `<div class="science-window">${windows.length > 1 ? `<h4>窗口 ${index + 1} · ${esc(evidenceTime(window.scene_acquired_at, false))}</h4>` : ""}${window.limitation ? `<p>${esc(business(window.limitation))}</p>` : ""}${window.full_task_aoi_covered === false ? "<p>本次为局部窗口，未覆盖全部任务区域。</p>" : ""}${links
            .filter((link) => {
              if (!safeURL(link.artifact_url) || seen.has(link.artifact_url))
                return false;
              seen.add(link.artifact_url);
              return true;
            })
            .map(
              (link, n) =>
                `<p><a href="${esc(safeURL(link.artifact_url))}" target="_blank" rel="noopener">${n === 0 ? "下载科学数据窗口" : "下载配套科学文件 " + n}${link.artifact?.split(".").at(-1) ? "（" + esc(link.artifact.split(".").at(-1).toUpperCase()) + "）" : ""}</a></p>`,
            )
            .join("")}</div>`;
        })
        .join("")}</section>`;
    })
    .join("")}</details>`;
}

function progressText(t) {
  const current = workEntries(t).findLast((e) =>
    ["running", "started", "queued"].includes(e.status),
  );
  return (
    current?.title ||
    (t.status === "queued"
      ? "我会在轮到这项任务后开始核查"
      : "我正在继续核对材料")
  );
}

function renderFusion(f,task=state.active) {
  const business=value=>reviewText(businessText(value,task?.images||[]));
  if (!screeningVerdictVisible(f)) return "";
  return `<div class="verdict-line"><strong>${esc(business(f.judgment || ""))}</strong>${f.basis ? `<p>${esc(business(f.basis))}</p>` : ""}${f.conflict_present ? "<p>不同证据存在分歧，请结合逐图复核结果与可比性评估。</p>" : ""}</div>`;
}

function renderImage(im, i) {
  const m = im.metadata || {},
    url = safeURL(
      im.previewMode === "annotated"
        ? im.annotated_url
        : im.url || im.image_url,
    );
  if (!url) return "";
  const ledger = sourceObservationLedger(evidenceTask()).find(
      (row) => row.asset_id === im.id,
    ),
    assessment = (evidenceTask().image_assessments || []).find(
      (row) => row.asset_id === im.id,
    ),
    background =
      im.analysis_scope === "background_only" ||
      assessment?.analysis_scope === "background_only" ||
      ledger?.same_point_eligible === false,
    role = ledger?.role || "用途未记录";
  const date =
    ledger?.acquisition_time || im.acquisition_time || m.acquisition_time;
  return `<article class="evidence-image" id="evidence-image-${i + 1}"><div class="image-caption"><strong>图${i + 1} · ${esc(sourceLabel(im.source || m.source || im.provider))}</strong><span>${esc(evidenceTime(date, false))}${!date && m.period_label ? ` · 资料期间 ${esc(m.period_label)}` : ""}</span><span>${esc(background ? "区域背景参考" : role)}</span></div><button class="image-preview" data-action="image" data-index="${i}" aria-label="放大查看图${i + 1}"><img src="${esc(url)}" loading="lazy" alt="图${i + 1} · ${esc(role)}"><span>${icon("expand")}查看</span></button><div class="image-controls">${im.annotated_url ? `<div class="image-pair"><button class="${im.previewMode !== "annotated" ? "active" : ""}" data-action="image-mode" data-index="${i}" data-mode="original">原图</button><button class="${im.previewMode === "annotated" ? "active" : ""}" data-action="image-mode" data-index="${i}" data-mode="annotated">标注</button></div>` : "<span></span>"}<button class="text-button" data-action="image" data-index="${i}">${icon("expand")}缩放</button></div><details class="image-description" data-ui-state="image-${esc(im.id)}"><summary>${background ? "区域背景参考" : assessment ? { positive: "发现疑似线索", negative: "本图未发现目标", unknown: "暂无法判断" }[assessment.decision] || "影像信息" : "影像信息"}${icon("chevron")}</summary>${ledger?.observation ? `<div class="markdown-body">${renderMarkdown(business(ledger.observation))}</div>` : ""}<dl class="metadata-grid">${[
    ["拍摄时间", evidenceTime(date)],
    ["获取时间", evidenceTime(ledger?.retrieved_at)],
    [
      "原生分辨率",
      ledger?.resolution && ledger.resolution !== "未知"
        ? ledger.resolution + " 米"
        : "未知",
    ],
    [
      "用途",
      ledger?.same_point_eligible === false
        ? "位置关系不支持同点判断，仅供背景参考"
        : background
          ? "区域背景参考，不用于固废目标识别"
          : role,
    ],
    ["位置校验", ledger?.spatial || "空间对应未知"],
  ]
    .map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`)
    .join("")}</dl></details></article>`;
}
function renderArtifacts(items) {
  return `<section class="evidence-panel" id="evidence-reports"><div class="evidence-panel-heading"><h2>${icon("file")}报告</h2></div>${
    items.length
      ? items
          .filter((a) => a.name !== "report.md" && safeURL(a.url))
          .map(
            (a) =>
              `<a class="report-card" href="${esc(safeURL(a.url))}" target="_blank" rel="noopener">${icon("file")}<span><strong>${esc(a.url?.includes("/conversations/")?({"report.docx":"会话报告 · Word","report.html":"会话报告 · 网页版","report.md":"会话报告 · 文本版"}[a.name]||a.name):({ "report.docx": "本轮报告 · Word", "report.html": "本轮报告 · 网页版", "report.md": "本轮报告 · 文本版", "finding-package.json": "完整证据归档（技术人员用）", "annotations.geojson": "空间标注文件（技术人员用）" }[a.name] || a.name || a.filename || "分析报告"))}</strong><small>${esc(a.kind || "任务成果 · 点击查看")}</small></span>${icon("download")}</a>`,
          )
          .join("")
      : '<p class="no-evidence">报告生成后，会在这里统一查看与下载。</p>'
  }</section>`;
}
function renderSourceDetails(pkg,task=state.active) {
  const business=value=>reviewText(businessText(value,task?.images||[]));
  const ranking = pkg.selection?.ranking || [];
  return ranking.length
    ? `<section class="trace-detail"><h3>图源选择依据</h3>${ranking.map((r) => `<p><strong>${esc(sourceLabel(r.source || r.group))}</strong>：${esc(business(r.reason || ""))}${r.independence_group === "bing_mosaic" ? "（Bing 与 MapQuest 属于同一来源组）" : ""}</p>`).join("")}</section>`
    : "";
}

function renderPolicies(policy) {
  const items = Array.isArray(policy)
    ? policy
    : policy?.items || policy?.results || policy?.sources || [];
  if (!items.length) return "";
  return `<details class="supporting-details" data-ui-state="sources"><summary>参考来源 <span>${items.length}</span>${icon("chevron")}</summary><ol class="policy-list">${items
    .filter((p) => safeURL(p.url || p.link))
    .map(
      (p) =>
        `<li><a href="${esc(safeURL(p.url || p.link))}" target="_blank" rel="noopener noreferrer">${esc(p.title || p.name || "政策资料")}</a></li>`,
    )
    .join("")}</ol></details>`;
}

let renderedKey = null,
  imageViewer = null;
function render() {
  voiceTools.context(
    `${state.user?.id}:${state.route}:${state.active?.id || ""}`,
  );
  const key = `${state.user?.id || "anonymous"}:${state.route}:${state.conversation?.id || state.active?.conversation_id || state.active?.id || ""}`,
    same = key === renderedKey,
    chat = document.querySelector("#chat-scroll"),
    evidence = document.querySelector("#evidence-scroll"),
    focus = document.activeElement,
    focusId = focus?.id,
    selection = focus?.selectionStart;
  document
    .querySelectorAll("details[data-ui-state]")
    .forEach((node) => (state.detailState[node.dataset.uiState] = node.open));
  const top = same ? chat?.scrollTop || 0 : 0,
    stick =
      same &&
      chat &&
      chat.scrollHeight - chat.scrollTop - chat.clientHeight < 100;
  if (evidence) state.evidenceScroll = evidence.scrollTop;
  resetRenderCache();
  root.innerHTML = !state.user
    ? authView()
    : state.route === "admin"
      ? renderAdminLazy(state, icon, () => { if (state.route === "admin") render(); })
      : shell();
  renderedKey = key;
  document
    .querySelectorAll("details[data-ui-state]")
    .forEach(
      (node) =>
        (node.open = state.detailState[node.dataset.uiState] ?? node.open),
    );
  bindWorkspace();
  const nextChat = document.querySelector("#chat-scroll");
  if (nextChat) nextChat.scrollTop = stick ? nextChat.scrollHeight : top;
  const nextEvidence = document.querySelector("#evidence-scroll");
  if (nextEvidence) nextEvidence.scrollTop = state.evidenceScroll || 0;
  if (same && ["objective", "center", "history-search"].includes(focusId)) {
    const node = document.getElementById(focusId);
    node?.focus({ preventScroll: true });
    if (node && typeof selection === "number")
      node.setSelectionRange(selection, selection);
  }
  updateLatest();
}
function resizeComposer() {
  const node = document.querySelector("#objective");
  if (node) {
    node.style.height = "auto";
    node.style.height = Math.min(180, Math.max(64, node.scrollHeight)) + "px";
  }
}
function updateComposer() {
  const questionDock = document.querySelector("#question-dock");
  if (questionDock) questionDock.innerHTML = questionCard(state.guide);
  const area = document.querySelector("#composer-area"),
    focused = document.activeElement?.id === "objective",
    position = document.activeElement?.selectionStart;
  if (area) area.innerHTML = composer(state.route === "task");
  resizeComposer();
  if (focused) {
    const input = document.querySelector("#objective");
    input?.focus({ preventScroll: true });
    if (typeof position === "number")
      input?.setSelectionRange(position, position);
  }
}
function updateLatest() {
  const chat = document.querySelector("#chat-scroll"),
    button = document.querySelector(".scroll-latest");
  if (button)
    button.hidden =
      !chat || chat.scrollHeight - chat.scrollTop - chat.clientHeight < 160;
}
function scrollLatest(smooth = true) {
  const chat = document.querySelector("#chat-scroll");
  chat?.scrollTo({
    top: chat.scrollHeight,
    behavior: smooth ? "smooth" : "instant",
  });
}
function setEvidenceWidth(value) {
  const workspace = document.querySelector(".conversation-layout");
  if (!workspace) return;
  state.evidenceWidth = evidencePanelWidth(value, workspace.clientWidth);
  workspace.style.setProperty("--evidence-width", state.evidenceWidth + "px");
  const handle = document.querySelector(".evidence-resizer");
  handle?.setAttribute("aria-valuenow", String(state.evidenceWidth));
}
function bindWorkspace() {
  resizeComposer();
  const handle = document.querySelector(".evidence-resizer");
  if (!handle) return;
  let drag = null;
  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    drag = event.pointerId;
    handle.setPointerCapture(drag);
    document.body.classList.add("resizing-evidence");
    event.preventDefault();
  });
  handle.addEventListener("pointermove", (event) => {
    if (drag === event.pointerId) {
      const rect = handle.parentElement.getBoundingClientRect();
      setEvidenceWidth(rect.right - event.clientX);
    }
  });
  const stop = () => {
    drag = null;
    document.body.classList.remove("resizing-evidence");
  };
  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
  handle.addEventListener("lostpointercapture", stop);
  handle.addEventListener("keydown", (event) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      setEvidenceWidth(
        event.key === "Home"
          ? 280
          : event.key === "End"
            ? 800
            : state.evidenceWidth + (event.key === "ArrowLeft" ? 24 : -24),
      );
    }
  });
}

async function loadSession() {
  const data = await api("/session");
  state.auth = data.auth || null;
  state.preferences = data.preferences || {};
  state.usage = data.usage || null;
  applyAppearance(state.preferences);
  state.user = data.user;
  state.csrf = data.csrf_token;
  state.bootstrap = !!data.bootstrap_required;
  taskDefaults = data.task_defaults || taskDefaults;
  if (!state.draft.search_touched)
    state.draft.search_policy = taskDefaults.search_policy ?? true;
  if (state.user) loadAnswerModels();
}
async function loadAnswerModels() {
  try {
    const data = await api("/answer-models");
    state.answerModels = Array.isArray(data.models) ? data.models : null;
  } catch {
    state.answerModels = null; // the composer falls back to 见微 only
  }
  updateComposer();
}
async function conversationList(offset=0) {
  try {
    const page=await api(`/conversations?limit=100&offset=${offset}`);
    if(!Array.isArray(page.items))throw new Error("会话列表格式无效，请刷新后重试。");
    return page;
  } catch(error) {
    if(![404,405].includes(error.status))throw error;
    return api(`/tasks?limit=100&offset=${offset}`);
  }
}
async function loadLists() {
  const owner=state.user?.id;
  const results=await Promise.allSettled([conversationList(),api("/uploads")]);
  if(owner!==state.user?.id)return;
  if(results[0].status==="fulfilled"){
    state.tasks=normaliseList(results[0].value);
    state.historyOffset=state.tasks.length;
    state.historyHasMore=!!results[0].value.has_more;
  }else toast(results[0].reason.message,true);
  if(results[1].status==="fulfilled")state.uploads=normaliseList(results[1].value);
}
async function loadMoreHistory() {
  if(state.historyLoading||!state.historyHasMore)return;
  const owner=state.user?.id;
  state.historyLoading=true;
  const refresh=()=>{const list=document.querySelector("#history-list");if(list)list.innerHTML=history();};
  refresh();
  try{
    const page=await conversationList(state.historyOffset);
    if(owner!==state.user?.id)return;
    const rows=normaliseList(page), ids=new Set(state.tasks.map(conversationId));
    state.tasks.push(...rows.filter(row=>!ids.has(conversationId(row))));
    state.historyOffset+=rows.length;
    state.historyHasMore=!!page.has_more;
  }finally{if(owner===state.user?.id){state.historyLoading=false;refresh();}}
}
async function legacyConversation(task, before=null) {
  const turns=before?[]:[task],seen=new Set(turns.map(turn=>turn.id));
  let id=before||task.parent_task_id;
  while(id&&turns.length<50&&!seen.has(id)){
    seen.add(id);
    const parent=await api("/tasks/"+encodeURIComponent(id));
    turns.unshift(parent);id=parent.parent_task_id;
  }
  const first=turns[0]||task;
  return {id:task.conversation_id||first.id,title:task.conversation_title||taskTitle(first),
    latest_task_id:task.id,turns,has_more:!!id,before_cursor:id,legacy:true};
}
async function conversationPage(task,before=null) {
  const id=task.conversation_id||state.conversation?.id||task.id;
  try{
    const page=await api(`/conversations/${encodeURIComponent(id)}?limit=50${before?'&before='+encodeURIComponent(before):''}`);
    if(!Array.isArray(page.turns))throw new Error("对话记录格式无效，请刷新后重试。");
    return page;
  }catch(error){
    if(![404,405].includes(error.status))throw error;
    return legacyConversation(task,before);
  }
}
function applyConversationPage(page,{preserve=false,prepend=false}={}) {
  const same=state.conversation?.id===page.id;
  const previous=same&&preserve?state.conversationTurns:[];
  const oldFirst=previous[0]?.id,oldHasMore=state.conversationHasMore,oldBefore=state.conversationBefore;
  state.conversation={...(same?state.conversation:{}),...page};
  state.conversationTurns=mergeTurns(previous,page.turns);
  const retainedOlder=preserve&&!prepend&&oldFirst&&state.conversationTurns[0]?.id===oldFirst&&oldFirst!==page.turns[0]?.id;
  state.conversationHasMore=retainedOlder?oldHasMore:!!page.has_more;
  state.conversationBefore=retainedOlder?oldBefore:page.before_cursor||page.turns[0]?.id||null;
  const latest=state.conversationTurns.find(turn=>turn.id===page.latest_task_id);
  if(latest)state.active={...latest,conversation_id:page.id,conversation_title:page.title};
  if(state.active)syncTaskState({...state.active,conversation_id:page.id,conversation_title:page.title,turn_count:page.turn_count||state.conversationTurns.length});
}
async function loadMoreConversation() {
  if(state.conversationLoading||!state.conversationHasMore||!state.active)return;
  const owner=state.user?.id,generation=loadGeneration,id=state.conversation?.id;
  state.conversationLoading=true;
  updateTaskContent();
  try{
    const page=await conversationPage(state.active,state.conversationBefore);
    if(owner!==state.user?.id||generation!==loadGeneration||id!==state.conversation?.id)return;
    const chat=document.querySelector('#chat-scroll'),height=chat?.scrollHeight||0,top=chat?.scrollTop||0;
    applyConversationPage(page,{preserve:true,prepend:true});
    state.conversationLoading=false;
    updateTaskContent();
    if(chat)chat.scrollTop=top+chat.scrollHeight-height;
  }finally{
    if(owner===state.user?.id&&generation===loadGeneration){state.conversationLoading=false;updateTaskContent();}
  }
}
function keepImageModes(data) {
  const modes = new Map(
    (state.active?.images || []).map((image) => [image.id, image.previewMode]),
  );
  for (const image of data.images || [])
    image.previewMode = modes.get(image.id) || "original";
  return data;
}
async function openTask(id) {
  clearTimeout(pollTimer);
  const generation=++loadGeneration,owner=state.user?.id;
  const cached=state.conversationTurns.find(turn=>turn.id===id)||state.tasks.find(turn=>turn.id===id)||{id,status:"pending"};
  const nextConversation=cached.conversation_id||cached.id;
  const same=state.conversation?.id===nextConversation;
  state.route="task";
  if(!same)state.evidenceOpen=!window.matchMedia("(max-width: 1100px)").matches;
  state.evidenceTaskId=null;
  state.active=cached;
  if(!same){state.conversation={id:nextConversation,title:cached.conversation_title||cached.title||taskTitle(cached)};state.conversationTurns=[];state.conversationHasMore=false;state.conversationBefore=null;}
  state.conversationLoading=false;
  state.error="";state.sidebar=false;state.evidenceScroll=0;state.evidenceTab="key";state.evidenceComparison=0;
  state.draft=newDraft(state.draft);state.guide=null;
  render();
  try{
    const data=await api("/tasks/"+encodeURIComponent(id));
    if(generation!==loadGeneration||owner!==state.user?.id)return;
    state.active=data;
    const page=await conversationPage(data);
    if(generation!==loadGeneration||owner!==state.user?.id)return;
    applyConversationPage(page,{preserve:same});
    if((state.active.workflow_mode||state.active.task?.workflow_mode)==="contextual_qa"&&!state.active.images?.length)state.evidenceOpen=false;
    await refreshActiveScan(state.active.scan_id);
    if(generation!==loadGeneration||owner!==state.user?.id)return;
    render();
    const target=document.querySelector(`[data-task-id="${CSS.escape(id)}"]`);
    if(id!==state.active.id&&target)target.scrollIntoView({block:'start'});
    else if(isRunning(state.active)||same)scrollLatest(false);
    schedulePoll();
  }catch(error){
    if(generation!==loadGeneration||owner!==state.user?.id)return;
    state.error=error.message;render();
  }
}
function syncTaskState(data) {
  const current=state.conversation?.id;
  state.tasks=upsertConversation(state.tasks,data,current===conversationId(data)?state.conversation?.title:'');
  if(current===conversationId(data))state.conversationTurns=mergeTurns(state.conversationTurns,[data]);
  const list=document.querySelector("#history-list");if(list)list.innerHTML=history();
}
let lastThreadHTML = "", lastEvidenceHTML = "", lastComposerKey = "";
function resetRenderCache() { lastThreadHTML = lastEvidenceHTML = lastComposerKey = ""; }
function updateTaskContent() {
  const chat = document.querySelector("#chat-scroll"),
    thread = chat?.querySelector(".chat-thread"),
    evidence = document.querySelector("#evidence-scroll");
  if (!thread) return;
  // 先生成新内容；与上一次完全相同就什么都不动（轮询时最常见的情况）。
  const nextThread = taskView(),
    nextEvidence = evidence ? evidenceView() : "";
  const unchanged = nextThread === lastThreadHTML && nextEvidence === lastEvidenceHTML && thread.childElementCount > 0;
  if (unchanged) { updateLatest(); return; }
  const top = chat.scrollTop,
    stick = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 100,
    evidenceTop = evidence?.scrollTop || 0;
  document
    .querySelectorAll("details[data-ui-state]")
    .forEach((node) => (state.detailState[node.dataset.uiState] = node.open));
  if (nextThread !== lastThreadHTML || !thread.childElementCount) thread.innerHTML = nextThread;
  lastThreadHTML = nextThread;
  // 输入框只在它依赖的状态变化时才重建，避免打字过程中被轮询打断。
  const composerKey = [isRunning(state.active), !!state.guide?.question, state.busy, state.uploading, state.draft.files.length,
    state.draft.selected_sources?.length || 0, state.draft.search_policy, state.draft.center].join("|");
  if (composerKey !== lastComposerKey) { updateComposer(); lastComposerKey = composerKey; }
  const status = document.querySelector(".progress-pill");
  if (status)
    status.innerHTML =
      '<span class="status-dot ' +
      (isRunning(state.active) ? "live" : "") +
      '"></span>' +
      (isRunning(state.active) ? "正在工作" : statusLabel(state.active.status));
  if (evidence && nextEvidence !== lastEvidenceHTML) evidence.innerHTML = nextEvidence;
  lastEvidenceHTML = nextEvidence;
  document
    .querySelectorAll("details[data-ui-state]")
    .forEach(
      (node) =>
        (node.open = state.detailState[node.dataset.uiState] ?? node.open),
    );
  chat.scrollTop = stick ? chat.scrollHeight : top;
  if (evidence) evidence.scrollTop = evidenceTop;
  const toggle = document.querySelector(
      '.topbar [data-action="evidence-toggle"]',
    ),
    count = evidenceTask().images?.length || 0;
  if (toggle)
    toggle.innerHTML =
      icon("image") +
      (state.evidenceOpen ? "收起证据" : "影像与资料") +
      (count ? " <span>" + count + "</span>" : "");
  updateLatest();
}
let pollQuiet = 0, lastPollSignature = "";
async function refreshTask() {
  if(!state.active)return;
  const id=state.active.id,generation=loadGeneration,owner=state.user?.id;
  const data=await api("/tasks/"+encodeURIComponent(id));
  if(generation!==loadGeneration||owner!==state.user?.id)return;
  const signature=[data.status,data.result_status,(data.activity_entries||[]).length,(data.images||[]).length,data.queue_position??"",data.updated_at].join("|");
  pollQuiet=signature===lastPollSignature?Math.min(pollQuiet+1,4):0;lastPollSignature=signature;
  state.active=keepImageModes(data);syncTaskState(data);
  if(!isRunning(data)||!state.conversation?.core_report){
    const page=await conversationPage(data);
    if(generation!==loadGeneration||owner!==state.user?.id)return;
    applyConversationPage(page,{preserve:true});
  }
  await refreshActiveScan(state.active.scan_id);
  if(generation!==loadGeneration||owner!==state.user?.id)return;
  updateTaskContent();schedulePoll();
}
function schedulePoll() {
  clearTimeout(pollTimer);
  if (
    state.route !== "task" ||
    (!isRunning(state.active) && !complaintTools.running(state.active?.scan_id))
  )
    return;
  pollTimer = setTimeout(async () => {
    if (document.hidden) {
      schedulePoll();
      return;
    }
    try {
      await refreshTask();
    } catch (e) {
      toast(e.message, true);
      if (state.user) schedulePoll();
    }
  }, 3000 + pollQuiet * 2250);  // 有进展时 3 秒；越久没变化越慢，最多约 12 秒
}

let modalReturnFocus = null;
function showModal(content, wide = false) {
  if (!modalRoot.firstElementChild) modalReturnFocus = document.activeElement;
  root.inert = true;
  if (imageViewer?.observer) imageViewer.observer.disconnect();
  imageViewer = null;
  modalRoot.innerHTML = `<div class="modal-scrim"><section class="modal ${wide ? "image-modal" : ""}" role="dialog" aria-modal="true">${content}</section></div>`;
  modalRoot.querySelector("button,input")?.focus();
}
function closeModal() {
  profileTools.close();
  root.inert = false;
  if (imageViewer?.observer) imageViewer.observer.disconnect();
  imageViewer = null;
  modalRoot.innerHTML = "";
  if (modalReturnFocus?.isConnected)
    modalReturnFocus.focus({ preventScroll: true });
  else
    document
      .querySelector('[data-action="profile"]')
      ?.focus({ preventScroll: true });
  modalReturnFocus = null;
}
function viewerZoom(next, point) {
  const viewer = imageViewer;
  if (!viewer?.image.naturalWidth) return;
  const vp = viewer.viewport,
    img = viewer.image,
    scale = imageZoom(next),
    old = viewer.scale || 1,
    x = point?.x ?? vp.clientWidth / 2,
    y = point?.y ?? vp.clientHeight / 2,
    oldLeft = Math.max(
      16,
      (Math.max(vp.clientWidth, img.naturalWidth * old + 32) -
        img.naturalWidth * old) /
        2,
    ),
    oldTop = Math.max(
      16,
      (Math.max(vp.clientHeight, img.naturalHeight * old + 32) -
        img.naturalHeight * old) /
        2,
    ),
    worldX = (vp.scrollLeft + x - oldLeft) / old,
    worldY = (vp.scrollTop + y - oldTop) / old;
  viewer.scale = scale;
  const width = Math.max(vp.clientWidth, img.naturalWidth * scale + 32),
    height = Math.max(vp.clientHeight, img.naturalHeight * scale + 32);
  viewer.canvas.style.width = width + "px";
  viewer.canvas.style.height = height + "px";
  img.style.width = img.naturalWidth * scale + "px";
  img.style.height = img.naturalHeight * scale + "px";
  vp.scrollLeft = worldX * scale + (width - img.naturalWidth * scale) / 2 - x;
  vp.scrollTop = worldY * scale + (height - img.naturalHeight * scale) / 2 - y;
  modalRoot.querySelector(".zoom-value").textContent =
    Math.round(scale * 100) + "%";
}
function fitViewer() {
  const v = imageViewer;
  if (!v?.image.naturalWidth) return;
  viewerZoom(
    imageFit(
      v.image.naturalWidth,
      v.image.naturalHeight,
      v.viewport.clientWidth,
      v.viewport.clientHeight,
    ),
  );
  v.viewport.scrollLeft = 0;
  v.viewport.scrollTop = 0;
}
function openImageViewer(index) {
  const image = evidenceTask().images?.[index];
  if (!image) return;
  const url = safeURL(
    image.previewMode === "annotated"
      ? image.annotated_url
      : image.url || image.image_url,
  );
  if (!url) return;
  showModal(
    `<header class="modal-header"><h2>图${index + 1} · ${esc(sourceLabel(image.source || image.metadata?.source))}</h2><button class="icon-button" data-action="close-modal" aria-label="关闭影像查看器">${icon("close")}</button></header><div class="viewer-toolbar"><button class="secondary" data-action="zoom-out" aria-label="缩小">${icon("minus")}</button><output class="zoom-value" aria-live="polite">加载中</output><button class="secondary" data-action="zoom-in" aria-label="放大">${icon("plus")}</button><span class="toolbar-divider"></span><button class="secondary" data-action="zoom-fit">${icon("fit")}适应窗口</button><button class="secondary" data-action="zoom-actual">100%</button><button class="secondary" data-action="zoom-reset">${icon("refresh")}恢复</button><span class="viewer-hint">滚轮缩放 · 拖动查看</span></div><div class="image-viewport" tabindex="0" role="region" aria-label="影像预览，可用滚轮缩放、拖动或方向键移动"><div class="image-canvas"><img src="${esc(url)}" alt="图${index + 1}" draggable="false"></div></div>`,
    true,
  );
  const viewport = modalRoot.querySelector(".image-viewport"),
    img = viewport.querySelector("img");
  imageViewer = {
    viewport,
    image: img,
    canvas: viewport.querySelector(".image-canvas"),
    scale: 1,
  };
  img.addEventListener("load", fitViewer);
  if (img.complete && img.naturalWidth) fitViewer();
  img.addEventListener("error", () => {
    const label = modalRoot.querySelector(".zoom-value");
    if (label) label.textContent = "图片未能加载";
  });
  viewport.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      const rect = viewport.getBoundingClientRect();
      viewerZoom(
        imageViewer.scale *
          Math.exp(
            -event.deltaY *
              (event.deltaMode === 1
                ? 16
                : event.deltaMode === 2
                  ? viewport.clientHeight
                  : 1) *
              0.002,
          ),
        { x: event.clientX - rect.left, y: event.clientY - rect.top },
      );
    },
    { passive: false },
  );
  let drag = null;
  viewport.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const rect = viewport.getBoundingClientRect();
    if (
      event.clientX - rect.left >= viewport.clientWidth ||
      event.clientY - rect.top >= viewport.clientHeight
    )
      return;
    drag = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      left: viewport.scrollLeft,
      top: viewport.scrollTop,
    };
    viewport.focus({ preventScroll: true });
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add("dragging");
    event.preventDefault();
  });
  viewport.addEventListener("pointermove", (event) => {
    if (drag?.id === event.pointerId) {
      viewport.scrollLeft = drag.left - (event.clientX - drag.x);
      viewport.scrollTop = drag.top - (event.clientY - drag.y);
    }
  });
  const stop = () => {
    drag = null;
    viewport.classList.remove("dragging");
  };
  viewport.addEventListener("pointerup", stop);
  viewport.addEventListener("pointercancel", stop);
  viewport.addEventListener("lostpointercapture", stop);
  viewport.addEventListener("keydown", (event) => {
    if (
      [
        "+",
        "=",
        "-",
        "0",
        "Home",
        "ArrowLeft",
        "ArrowRight",
        "ArrowUp",
        "ArrowDown",
      ].includes(event.key)
    ) {
      event.preventDefault();
      if (["+", "="].includes(event.key)) viewerZoom(imageViewer.scale * 1.25);
      else if (event.key === "-") viewerZoom(imageViewer.scale / 1.25);
      else if (["0", "Home"].includes(event.key)) fitViewer();
      else
        viewport.scrollBy({
          left:
            event.key === "ArrowRight"
              ? 120
              : event.key === "ArrowLeft"
                ? -120
                : 0,
          top:
            event.key === "ArrowDown"
              ? 120
              : event.key === "ArrowUp"
                ? -120
                : 0,
        });
    }
  });
  imageViewer.observer = new ResizeObserver(() => {
    if (imageViewer?.viewport === viewport) fitViewer();
  });
  imageViewer.observer.observe(modalRoot.querySelector(".image-modal"));
}

function addMaterialEvent(title, summary, status = "completed") {
  state.materialEvents.push({
    id: "local-" + Date.now() + "-" + state.materialEvents.length,
    title,
    summary,
    status,
    time: new Date().toISOString(),
  });
  const thread = document.querySelector(".chat-thread");
  if (state.route === "home" && thread) thread.innerHTML = homeView();
  else if (thread) {
    let log = thread.querySelector(".pending-material-log");
    if (!log) {
      log = document.createElement("div");
      log.className = "pending-material-log";
      thread.append(log);
    }
    log.innerHTML = localMaterialLog(state.materialEvents, icon);
  }
  scrollLatest(false);
}
async function stopCurrentTask() {
  const current = state.active,owner=state.user?.id,generation=loadGeneration;
  if (!current) return;
  if (isRunning(current)) {
    const stopped = await api(
      "/tasks/" + encodeURIComponent(current.id) + "/cancel",
      { method: "POST", body: {} },
    );
    if(owner!==state.user?.id||generation!==loadGeneration||state.active?.id!==current.id)return;
    state.active=stopped;
    syncTaskState(state.active);
    clearTimeout(pollTimer);
  }
  render();
  toast("任务已停止，已有证据仍可查看。");
}

async function uploadFiles(files) {
  state.uploading = true;
  updateComposer();
  let workbook = null,
    archive = null;
  for (const file of files) {
    try {
      const data = new FormData();
      data.append("file", file);
      const uploaded = await api("/uploads", { method: "POST", body: data });
      const record = {
        ...uploaded,
        filename: uploaded.filename || uploaded.name || file.name,
        size: file.size,
      };
      state.draft.files.push(record);
      state.uploads.unshift(record);
      if (/\.xlsx$/i.test(file.name)) workbook = record;
      if (/\.zip$/i.test(file.name)) archive = record;
      addMaterialEvent(
        "我收到了你上传的材料",
        `${file.name} · ${fileSize(file.size)}。${/\.zip$/i.test(file.name) ? "我正在检查压缩包里有哪些材料。" : /\.tiff?$/i.test(file.name) ? "我保留了原始栅格，会核对格式、波段和位置信息。" : /\.(png|jpe?g|webp)$/i.test(file.name) ? "我会先看原图，再按需核对容易混淆的地物。" : "我已保留这份资料，会按你的问题提取相关内容。"}`,
      );
      toast(`${file.name} 已上传`);
    } catch (e) {
      addMaterialEvent(
        "这份材料还没有上传成功",
        `${file.name}：${e.message}`,
        "failed",
      );
      toast(`${file.name}：${e.message}`, true);
    }
  }
  state.uploading = false;
  updateComposer();
  if (workbook) await openWorkbook(workbook.id, 0);
  else if (archive)
    await workspaceTools.action("archive-open", {
      dataset: { id: archive.id },
    });
  else await workspaceTools.attachmentsChanged();
}
async function openWorkbook(id, offset = 0) {
  showModal(
    `<header class="modal-header"><h2>选择工作簿点位</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon("close")}</button></header><div class="modal-body"><div class="empty-state"><span class="spin"></span>正在读取点位记录…</div></div>`,
  );
  try {
    const data = await api(
      "/uploads/" +
        encodeURIComponent(id) +
        "/workbook?offset=" +
        offset +
        "&limit=20",
    );
    state.workbook = { id, ...data };
    showModal(
      `<header class="modal-header"><h2>工作簿点位 <span class="pill">共 ${data.total} 条</span></h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon("close")}</button></header><div class="modal-body"><div class="workbook-top-actions"><p class="admin-description">选择一个点位，或批量创建现状与时序核查任务。表格原有结论作为用户资料保留。</p><button class="secondary" data-action="workbook-batch-open">${icon("layers")}批量核查点位</button></div><div class="workbook-list">${(data.records || []).map((r) => `<button class="workbook-record" data-action="workbook-record" data-id="${esc(r.record_id)}"><span class="role-icon">${icon("pin")}</span><span><strong>${esc(r.site_id || r.record_id)}</strong><small>${esc([r.administrative_area, r.address].filter(Boolean).join(" · ") || r.source?.sheet || "工作簿记录")}</small></span><span class="pill">${r.embedded_image_count || 0} 张影像</span>${icon("chevron")}</button>`).join("") || '<div class="empty-state">工作簿中没有可选点位。</div>'}</div><div class="workbook-pagination"><button class="secondary" data-action="workbook-page" data-offset="${Math.max(0, offset - 20)}" ${offset === 0 ? "disabled" : ""}>上一页</button><span>${offset + 1}–${Math.min(offset + 20, data.total)} / ${data.total}</span><button class="secondary" data-action="workbook-page" data-offset="${offset + 20}" ${!data.has_more ? "disabled" : ""}>下一页</button></div></div>`,
    );
  } catch (e) {
    toast(e.message, true);
    closeModal();
  }
}
async function openWorkbookBatch() {
  const id = state.workbook?.id;
  if (!id) return;
  const prepared = await Promise.allSettled([
    api("/uploads/" + encodeURIComponent(id) + "/workbook?offset=0&limit=50"),
    api("/uploads/" + encodeURIComponent(id) + "/workbook/tasks", {
      method: "POST",
      body: {},
    }),
  ]);
  if (prepared[0].status !== "fulfilled") throw prepared[0].reason;
  const data = prepared[0].value,
    proposals =
      prepared[1].status === "fulfilled"
        ? prepared[1].value.proposals || []
        : [];
  const rows = [...(data.records || [])];
  if (data.has_more) {
    for (let offset = rows.length; offset < data.total && offset < 500; ) {
      const page = await api(
        "/uploads/" +
          encodeURIComponent(id) +
          "/workbook?offset=" +
          offset +
          "&limit=50",
      );
      if (!page.records?.length) break;
      rows.push(...page.records);
      offset = rows.length;
    }
  }
  state.workbookBatch = { id, records: rows, total: data.total, proposals };
  showModal(workbookBatchModal(state.workbookBatch, state.draft, icon), true);
}
function updateBatchCount() {
  const form = document.querySelector("#workbook-batch-form");
  if (form) {
    const count = form.querySelectorAll('[name="record_id"]:checked').length;
    form.querySelector(".batch-selection-count").textContent =
      "已选 " + count + " 个点位";
  }
}
async function submitWorkbookBatch(form) {
  const body = workbookBatchPayload(
      new FormData(form),
      state.draft.selected_sources || [],
      JSON.parse(form.dataset.proposalTaskTypes || "[]"),
    ),
    button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const result = await api(
      "/uploads/" + encodeURIComponent(form.dataset.id) + "/workbook/batch",
      { method: "POST", body },
    );
    closeModal();
    await loadLists();
    const items = result.items || [];
    toast("已创建 " + items.length + " 个逐点任务，按队列执行。");
    if (items[0]) await openTask(items[0].id || items[0].task_id);
    else render();
  } finally {
    button.disabled = false;
  }
}
async function selectWorkbookRecord(id, button) {
  button.disabled = true;
  try {
    const record = state.workbook.records.find((x) => x.record_id === id),
      documentId = state.workbook.id,
      result = await api(
        "/uploads/" +
          encodeURIComponent(documentId) +
          "/workbook/" +
          encodeURIComponent(id) +
          "/images",
        { method: "POST", body: {} },
      );
    state.draft = selectWorkbookPoint(
      state.draft,
      documentId,
      record,
      result.assets || [],
    );
    closeModal();
    updateComposer();
    toast(
      `已切换至点位 ${record.site_id || id}，关联 ${result.assets?.length || 0} 张影像`,
    );
  } catch (e) {
    button.disabled = false;
    toast(e.message, true);
  }
}
async function uploadLibrary() {
  try {
    state.uploads = normaliseList(await api("/uploads"));
    showModal(
      `<header class="modal-header"><h2>我的资料库</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon("close")}</button></header><div class="modal-body"><p class="admin-description">选择已上传的影像或资料，加入当前会话。</p><div class="workbook-list">${state.uploads.map((f) => `<button class="workbook-record" data-action="library-select" data-id="${esc(f.id)}"><span class="role-icon">${icon(f.kind === "image" ? "image" : "file")}</span><span><strong>${esc(f.filename || f.name || "资料文件")}</strong><small>${f.kind === "image" ? "影像" : "参考资料"} · ${fileSize(f.bytes || f.size || 0)}</small></span>${icon("plus")}</button>`).join("") || '<div class="empty-state">还没有上传资料。可以在任务输入框中上传。</div>'}</div></div>`,
    );
  } catch (e) {
    toast(e.message, true);
  }
}
async function submitBody(body) {
  const owner = state.user?.id,
    draft = state.draft;
  const materialEvents = [...state.materialEvents];
  const task = await api("/tasks", { method: "POST", body });
  if (owner !== state.user?.id || draft !== state.draft) return;
  syncTaskState({...task,objective:body.objective});
  state.busy = false;
  state.clientEventsByTask[task.id || task.task_id] = materialEvents;
  state.materialEvents = [];
  await openTask(task.id || task.task_id);
}
async function refreshActiveScan(id) {
  if (!id) return;
  try {
    await complaintTools.refresh(id);
  } catch {
    state.scanCache[id] = {
      ...(state.scanCache[id] || {}),
      scan_id: id,
      execution_status: "unavailable",
      summary: {
        ...(state.scanCache[id]?.summary || {}),
        conclusion:
          "区域覆盖记录暂未读取成功。请刷新区域记录；不能据此视为已经覆盖或没有疑点。",
      },
    };
  }
}
const complaintTools = createComplaintController({
  state,
  api,
  showModal,
  closeModal,
  toast,
  icon,
  loadLists,
  openTask,
  refreshTask,
  render,
});
const workspaceTools = createWorkspaceController({
  state,
  api,
  render,
  showModal,
  closeModal,
  toast,
  submitBody,
  updateComposer,
  addMaterialEvent,
  scrollLatest: ()=>scrollLatest(false),
  planComplaint: (task) => complaintTools.planConversation(task),
  startComplaint: (id) => complaintTools.startConversation(id),
});
async function submitTask(form) {
  if (voiceTools.isActive()) {
    toast("请先完成语音输入，再确认发送文字。");
    return;
  }
  if (state.dialoguePending || state.guide?.pending) return;
  state.error = "";
  const data = new FormData(form);
  const text = String(data.get("objective") || "").trim();
  if (!text) return;
  state.busy = true;
  try {
    if (state.guide) {
      await workspaceTools.free(text);
      state.busy = false;
      updateComposer();
      return;
    }
    if (isRunning(state.active)) {
      // 发送不再悄悄终止正在进行的核查：必须明确确认。
      if (!window.confirm("当前核查还在进行。要停止它，并按你刚写的新要求继续吗？\n已获取的影像和证据会保留。")) {
        state.busy = false;
        updateComposer();
        return;
      }
      await stopCurrentTask();
    }
    state.draft.objective = text;
    state.draft.center = String(data.get("center") || "").trim();
    const body = {
      ...taskPayload(state.draft, state.route === "task" ? state.active?.id : null),
      ...modelPayload(state.draft, state.answerModels),
    };
    updateComposer();
    await workspaceTools.start(body);
  } catch (e) {
    state.busy = false;
    state.error = e.message;
    render();
  }
}
root.addEventListener("input", (e) => {
  if (e.target.id === "guide-free" && state.guide) {
    state.guide.freeText = e.target.value;
    const hasAnswer = !!e.target.value.trim();
    if (hasAnswer) {
      state.guide.selected = null;
      document.querySelectorAll(".question-option").forEach((option) => {
        option.classList.remove("selected");
        option.setAttribute("aria-pressed", "false");
      });
    }
    e.target
      .closest(".question-free-label")
      ?.classList.toggle("has-answer", hasAnswer);
    const next = document.querySelector('[data-action="guide-next"]');
    if (next) {
      next.disabled = !hasAnswer && !state.guide.selected;
      next.textContent =
        hasAnswer || state.guide.selected ? "确认并继续" : "下一步";
    }
  }
  if (e.target.id === "objective") {
    state.draft.objective = e.target.value;
    const send = document.querySelector(".composer-send");
    if (send)
      send.disabled = state.busy || state.uploading || !e.target.value.trim();
    resizeComposer();
  }
  if (e.target.id === "center") state.draft.center = e.target.value;
  if (e.target.id === "history-search") {
    state.historyFilter = e.target.value;
    document.querySelector("#history-list").innerHTML = history();
  }
});
root.addEventListener("input", handleAdminModelInput);
root.addEventListener("change", (e) => {
  handleAdminModelInput(e);
  if (e.target.id === "file-input") void uploadFiles([...e.target.files]);
});
root.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (e.target.id === "auth-form") {
    const data = new FormData(e.target);
    if (state.bootstrap && data.get("password") !== data.get("confirm")) {
      state.error = "两次密码输入不一致。";
      render();
      return;
    }
    state.busy = true;
    state.error = "";
    const payload = {
      username: data.get("username"),
      password: data.get("password"),
    };
    if (state.bootstrap) payload.display_name = data.get("display_name");
    render();
    try {
      await api(state.bootstrap ? "/bootstrap" : "/login", {
        method: "POST",
        body: payload,
      });
      await loadSession();
      await loadLists();
      state.busy = false;
      state.error = "";
      render();
    } catch (err) {
      state.busy = false;
      state.error = err.message;
      render();
    }
  } else if (e.target.id === "task-form") await submitTask(e.target);
  else if (state.route === "admin")
    await handleAdminSubmit(e, state, { api, render, toast, closeModal, icon });
});
async function action(e) {
  const button = e.target.closest("[data-action]");
  if (!button) return;
  if (button.tagName === "A") e.preventDefault();
  const action = button.dataset.action;
  try {
    if (await voiceTools.action(action)) return;
    if (action === "model-open") {
      showModal(modelPicker(state.answerModels, state.draft, icon));
      return;
    }
    if (action === "model-select") {
      const model = selectedModel(state.answerModels, button.dataset.model);
      if (model.id !== button.dataset.model) {
        toast("该模型暂不可用，已保留当前选择。", true);
        return;
      }
      state.draft.answer_model = model.id;
      closeModal();
      updateComposer();
      toast(model.id === "shensi" ? "已切换到无隅慎思：追问会结合所在区域、周边设施和政策作答。" : "已切换回见微遥感判读。");
      return;
    }
    if (action === "thinking-toggle") {
      state.draft.deep_thinking = state.draft.deep_thinking === false;
      updateComposer();
      return;
    }
    if (await profileTools.action(action, button)) return;
    if (await complaintTools.action(action, button)) return;
    if (await workspaceTools.action(action, button)) return;
    if (action === "batch-proposal") {
      const proposal = (state.workbookBatch?.proposals || []).find(
          (item) => item.id === button.dataset.id,
        ),
        form = document.querySelector("#workbook-batch-form");
      if (proposal && form) {
        applyWorkbookProposal(form, proposal);
        updateBatchCount();
        toast("已填入建议任务，请确认坐标依据、点位和时段后提交。");
      }
    } else if (action === "workbook-batch-open") {
      await openWorkbookBatch();
    } else if (action === "batch-select-all" || action === "batch-clear") {
      for (const field of document.querySelectorAll(
        '#workbook-batch-form [name="record_id"]',
      ))
        field.checked = action === "batch-select-all";
      updateBatchCount();
    } else if (action === "conversation-more") {
      await loadMoreConversation();
    } else if (action === "history-more") {
      await loadMoreHistory();
    } else if (action === "sidebar-collapse") {
      state.sidebarCollapsed = !state.sidebarCollapsed;
      try {
        localStorage.setItem(
          "wuyu-sidebar-collapsed",
          String(state.sidebarCollapsed),
        );
      } catch {}
      render();
    } else if (action === "open-reports") {
      const origin = button.dataset.taskId;
      if (origin && state.conversationTurns.some((turn) => turn.id === origin)) state.evidenceTaskId = origin;
      state.evidenceOpen = true;
      render();
      document.querySelector("#evidence-reports")?.scrollIntoView({ block: "start", behavior: "smooth" });
    } else if (action === "stop-task") {
      await stopCurrentTask();
    } else if (action === "starter") {
      state.draft.objective = button.dataset.prompt;
      updateComposer();
      document.querySelector("#objective")?.focus();
    } else if (action === "latest") {
      scrollLatest();
    } else if (action === "evidence-comparison") {
      state.evidenceComparison = Number(button.dataset.index);
      render();
    } else if (action === "evidence-tab") {
      state.evidenceTab = button.dataset.tab;
      render();
    } else if (action === "evidence-focus") {
      const origin=button.closest('[data-task-id]')?.dataset.taskId;
      if(origin&&state.conversationTurns.some(turn=>turn.id===origin))state.evidenceTaskId=origin;
      state.evidenceOpen = true;
      state.evidenceTab = "key";
      const index = Number(button.dataset.index);
      render();
      const target = document.querySelector("#evidence-image-" + (index + 1));
      const disclosure = target?.closest(".additional-evidence");
      if (disclosure) disclosure.open = true;
      target?.scrollIntoView({ block: "start", behavior: "smooth" });
    } else if (action === "evidence-toggle") {
      state.evidenceOpen = !state.evidenceOpen;
      render();
    } else if (action === "zoom-in") {
      if (imageViewer) viewerZoom(imageViewer.scale * 1.25);
    } else if (action === "zoom-out") {
      if (imageViewer) viewerZoom(imageViewer.scale / 1.25);
    } else if (action === "zoom-fit" || action === "zoom-reset") {
      fitViewer();
    } else if (action === "zoom-actual") {
      viewerZoom(1);
    } else if (action === "reload") {
      location.reload();
    } else if (action === "workbook") {
      await openWorkbook(button.dataset.id, 0);
    } else if (action === "workbook-page") {
      await openWorkbook(state.workbook.id, Number(button.dataset.offset));
    } else if (action === "workbook-record") {
      await selectWorkbookRecord(button.dataset.id, button);
    } else if (action === "library") {
      await uploadLibrary();
    } else if (action === "library-select") {
      const file = state.uploads.find((x) => x.id === button.dataset.id);
      if (file && !state.draft.files.some((x) => x.id === file.id))
        state.draft.files.push({
          ...file,
          filename: file.filename || file.name,
        });
      closeModal();
      updateComposer();
      if (file && /\.xlsx$/i.test(file.name || file.filename || ""))
        await openWorkbook(file.id, 0);
    } else if (action === "image-mode") {
      const im = evidenceTask().images?.[Number(button.dataset.index)];
      if(!im)return;
      im.previewMode = button.dataset.mode;
      button.closest(".evidence-image").outerHTML = renderImage(
        im,
        Number(button.dataset.index),
      );
    } else if (action === "new") {
      workspaceTools.reset();
      voiceTools.close();
      ++loadGeneration;
      clearTimeout(pollTimer);
      state.route = "home";
      state.active = null;
      state.ancestors = [];
      state.conversation = null;
      state.conversationTurns = [];
      state.conversationHasMore = false;
      state.conversationBefore = null;
      state.conversationLoading = false;
      state.evidenceTaskId = null;
      state.error = "";
      state.draft = newDraft(state.draft);
      state.guide = null;
      state.materialEvents = [];
      state.dialogueTurns = [];
      state.sidebar = false;
      render();
    } else if (action === "task") await openTask(button.dataset.id);
    else if (action === "menu") {
      state.sidebar = !state.sidebar;
      document
        .querySelector(".sidebar")
        ?.classList.toggle("open", state.sidebar);
    } else if (action === "logout") {
      voiceTools.close();
      await api("/logout", { method: "POST" });
      resetPrivateView();
      closeModal();
      submissionKeys.clear();
      state.preferences = {};
      applyAppearance();
      clearTimeout(pollTimer);
      state.user = null;
      state.csrf = null;
      state.active = null;
      state.scanCache = {};
      state.route = "home";
      render();
    } else if (action === "upload")
      document.querySelector("#file-input").click();
    else if (action === "remove-file") {
      state.draft.files.splice(Number(button.dataset.index), 1);
      updateComposer();
    } else if (action === "close-modal") closeModal();
    else if (action === "search-toggle") {
      state.draft.search_policy = !state.draft.search_policy;
      state.draft.search_touched = true;
      if (state.guide)
        state.guide.body.search_policy = state.draft.search_policy;
      updateComposer();
    } else if (action === "refresh-task") await refreshTask();
    else if (action === "image") {
      openImageViewer(Number(button.dataset.index));
    } else if (action === "admin" && state.user.role === "admin") {
      clearTimeout(pollTimer);
      state.route = "admin";
      state.error = "";
      render();
      try {
        const [config, users, overview] = await Promise.all([
          api("/config"),
          api("/users"),
          api("/admin/overview"),
        ]);
        state.config = config;
        state.users = normaliseList(users);
        state.adminOverview = overview;
        render();
      } catch (err) {
        state.error = err.message;
        render();
      }
    } else if (state.route === "admin")
      await handleAdminAction(action, button, state, {
        api,
        render,
        toast,
        showModal,
        closeModal,
        icon,
      });
  } catch (err) {
    toast(err.message, true);
  }
}
root.addEventListener("click", (event) => {
  const source = event.target.closest('a[href^="#source-"]');
  if (source) {
    const target = document.getElementById(
      source.getAttribute("href").slice(1),
    );
    if (target) {
      event.preventDefault();
      for(let parent=target.parentElement;parent;parent=parent.parentElement)
        if(parent.tagName==='DETAILS')parent.open=true;
      target.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
  const link = event.target.closest('a[href^="#evidence-image-"]');
  if (link && !link.dataset.action) {
    link.dataset.action = "evidence-focus";
    link.dataset.index = String(
      Number(link.getAttribute("href").split("-").at(-1)) - 1,
    );
  }
  void action(event);
});
root.addEventListener(
  "scroll",
  (event) => {
    if (event.target.id === "chat-scroll") updateLatest();
  },
  true,
);
root.addEventListener("paste", (event) => {
  if (event.target.id === "objective" && event.clipboardData?.files?.length) {
    event.preventDefault();
    void uploadFiles([...event.clipboardData.files]);
  }
});
root.addEventListener("dragover", (event) => {
  if (
    state.user &&
    state.route !== "admin" &&
    event.dataTransfer?.types.includes("Files")
  ) {
    event.preventDefault();
    document.querySelector(".composer")?.classList.add("drop-ready");
  }
});
root.addEventListener("dragleave", (event) => {
  if (!root.contains(event.relatedTarget))
    document.querySelector(".composer")?.classList.remove("drop-ready");
});
root.addEventListener("drop", (event) => {
  if (
    state.user &&
    state.route !== "admin" &&
    event.dataTransfer?.files?.length
  ) {
    event.preventDefault();
    document.querySelector(".composer")?.classList.remove("drop-ready");
    void uploadFiles([...event.dataTransfer.files]);
  }
});
modalRoot.addEventListener("click", (e) => {
  if (e.target.classList.contains("modal-scrim")) closeModal();
  else void action(e);
});
modalRoot.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    if (await profileTools.submit(e)) return;
    if (e.target.id === "workbook-batch-form") {
      await submitWorkbookBatch(e.target);
      return;
    }
    if (await workspaceTools.submit(e)) return;
    await handleAdminSubmit(e, state, { api, render, toast, closeModal, icon });
  } catch (err) {
    toast(err.message, true);
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModal();
  if (event.key === "Tab" && modalRoot.firstElementChild) {
    const controls = [
      ...modalRoot.querySelectorAll(
        'button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]',
      ),
    ].filter((el) => el.getClientRects().length);
    const first = controls[0],
      last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  }
  if (
    shouldSend(event, state.preferences.send_key) &&
    document.activeElement?.id === "objective"
  ) {
    event.preventDefault();
    document.querySelector("#task-form")?.requestSubmit();
  }
});
try {
  await loadSession();
  if (state.user) await loadLists();
  render();
} catch (e) {
  state.error = e.message;
  root.innerHTML = `<div class="connection-error">${brand()}<h1>暂时无法连接工作台</h1><p>${esc(e.message)}</p><button class="primary" data-action="reload">重新连接</button></div>`;
}

modalRoot.addEventListener("change", (event) => {
  void profileTools.change(event);
  if (event.target.name === "record_id") updateBatchCount();
  if (event.target.name === "task_type")
    event.target.form.dataset.proposalTaskTypes = "[]";
});

