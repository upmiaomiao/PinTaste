import { escapeHTML as esc, safeURL, statusLabel } from "./lib.mjs";
import { portalBase } from "./ui-config.mjs";

export function validateScanBBox(value) {
  const box = Array.isArray(value)
    ? value
    : String(value || "")
        .split(/[,，\s]+/)
        .filter(Boolean)
        .map(Number);
  if (
    box.length !== 4 ||
    !box.every(Number.isFinite) ||
    !(
      -180 <= box[0] &&
      box[0] < box[2] &&
      box[2] <= 180 &&
      -85 <= box[1] &&
      box[1] < box[3] &&
      box[3] <= 85
    )
  )
    throw new Error(
      "请填写 WGS84 范围：西经度、南纬度、东经度、北纬度，且西小于东、南小于北。",
    );
  return [...box];
}
const list = (value) => (Array.isArray(value) ? value : []);
export function scanGrid(plan = {}, summary = {}) {
  const windows = list(plan.windows).filter(
    (row) =>
      Array.isArray(row.bbox) &&
      row.bbox.length === 4 &&
      row.bbox.every(Number.isFinite),
  );
  let region;
  try {
    region = validateScanBBox(plan.region_bbox);
  } catch {
    return "";
  }
  const bounds = [
      Math.min(region[0], ...windows.map((w) => w.bbox[0])),
      Math.min(region[1], ...windows.map((w) => w.bbox[1])),
      Math.max(region[2], ...windows.map((w) => w.bbox[2])),
      Math.max(region[3], ...windows.map((w) => w.bbox[3])),
    ],
    pad = 15,
    w = 510,
    h = 220,
    dx = bounds[2] - bounds[0],
    dy = bounds[3] - bounds[1];
  const rect = (box) => {
    const x = pad + ((box[0] - bounds[0]) / dx) * w,
      y = pad + ((bounds[3] - box[3]) / dy) * h;
    return `x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${(((box[2] - box[0]) / dx) * w).toFixed(2)}" height="${(((box[3] - box[1]) / dy) * h).toFixed(2)}"`;
  };
  const reviewed = new Set(
    list(summary.ledger)
      .filter((row) => row.visual_reviewed === true)
      .map((row) => row.window_id),
  );
  return `<figure class="scan-grid"><svg viewBox="0 0 540 250" role="img" aria-label="扫描范围与逐窗网格示意"><rect width="540" height="250" fill="#f8faf9"/>${windows.map((row, index) => `<rect ${rect(row.bbox)} fill="${reviewed.has(row.window_id) ? "#dcece3" : "#eef1f0"}" stroke="#9ca3af" stroke-width="1"/><text x="${(pad + ((row.center?.[0] - bounds[0]) / dx) * w || pad + 12).toFixed(2)}" y="${(pad + ((bounds[3] - row.center?.[1]) / dy) * h || pad + 16).toFixed(2)}" text-anchor="middle" fill="#374151" font-size="10">${index + 1}</text>`).join("")}<rect ${rect(region)} fill="none" stroke="#374151" stroke-width="2" stroke-dasharray="5 3"/></svg><figcaption>虚线为确认范围，方格为取图窗口；仅作范围与窗口示意，不代替地图定位。</figcaption></figure>`;
}
const pct = (value) =>
  typeof value === "number" && Number.isFinite(value)
    ? (value * 100).toFixed(1) + "%"
    : "待核实";
export function renderScanCoverage(view, scanId = "", icon = () => "") {
  if (!scanId) return "";
  if (!view)
    return `<section class="scan-coverage"><h2>举报区域核查</h2><p>我正在读取各窗口的覆盖与盲区记录…</p></section>`;
  const summary = view.summary || {},
    items = list(view.items),
    id = esc(view.scan_id || scanId),
    metrics = [
      ["planned_coverage", "计划覆盖"],
      ["downloaded_requested_coverage", "取得影像"],
      ["visual_reviewed_coverage", "有效审阅"],
      ["time_qualified_coverage", "时相合格"],
    ];
  return `<section class="scan-coverage" aria-label="举报区域覆盖与盲区"><header><h2>${icon("scan")}举报区域核查</h2><span>${view.execution_status === "running" ? "逐窗执行中" : view.execution_status === "completed" ? "逐窗任务已结束" : view.execution_status === "unavailable" ? "覆盖记录待重试" : "尚未执行"}</span></header><div class="scan-controls"><button class="secondary" data-action="scan-refresh" data-scan-id="${id}">刷新区域记录</button>${view.execution_status === "running" ? `<button class="stop-button" data-action="scan-cancel" data-scan-id="${id}">停止整片区域核查</button>` : ""}</div><p class="scan-aggregate-conclusion">${esc(summary.conclusion || "尚无区域结论，等待窗口核查记录。")}</p><div class="scan-metrics">${metrics.map(([key, label]) => `<div><strong>${pct(summary[key])}</strong><span>${label}</span></div>`).join("")}</div><p>有效审阅窗口 ${summary.visual_reviewed_windows ?? "待核实"} / ${summary.planned_windows ?? list(view.plan?.windows).length}；时相合格窗口 ${summary.time_qualified_windows ?? "待核实"}。下载完成不等于已经审阅，成像时间未知仍保留时相缺口。</p>${scanGrid(view.plan || {}, summary)}${list(summary.missing_visual_window_ids).length ? `<p class="scan-blindspots"><strong>审阅盲区</strong> ${esc(summary.missing_visual_window_ids.join("、"))}</p>` : ""}${list(summary.time_unqualified_window_ids).length ? `<p class="scan-blindspots"><strong>时相待补</strong> ${esc(summary.time_unqualified_window_ids.join("、"))}</p>` : ""}${items.length ? `<div class="scan-window-links">${items.map((item, index) => `<button class="secondary" data-action="task" data-id="${esc(item.id)}">窗口 ${index + 1} · ${esc(statusLabel(item.status))}</button>`).join("")}</div><div class="answer-artifacts"><a href="${esc(safeURL(portalBase() + "/portal/api/complaints/scans/" + id + "/report.docx"))}" target="_blank" rel="noopener">${icon("file")}下载区域 Word 报告</a><a href="${esc(safeURL(portalBase() + "/portal/api/complaints/scans/" + id + "/report.html"))}" target="_blank" rel="noopener">${icon("file")}查看区域报告</a></div>` : ""}<details class="supporting-details" data-ui-state="scan-limits-${id}"><summary>范围、日期与不可观测限制${icon("chevron")}</summary><ul>${list(
    summary.limitations,
  )
    .map((value) => `<li>${esc(value)}</li>`)
    .join("")}</ul></details></section>`;
}
export function createComplaintController(ctx) {
  const { state, api, toast, loadLists, openTask } = ctx;
  async function refresh(id) {
    if (!id) return null;
    const owner = state.user?.id;
    const view = await api("/complaints/scans/" + encodeURIComponent(id));
    if (owner !== state.user?.id) return view;
    state.scanCache ??= {};
    state.scanCache[id] = view;
    return view;
  }
  async function action(action, button) {
    if (!action.startsWith("scan-")) return false;
    if (action === "scan-refresh") {
      await refresh(button.dataset.scanId);
      ctx.render?.();
      return true;
    }
    if (action === "scan-cancel") {
      button.disabled = true;
      try {
        await api(
          "/complaints/scans/" +
            encodeURIComponent(button.dataset.scanId) +
            "/cancel",
          { method: "POST", body: {} },
        );
        await refresh(button.dataset.scanId);
        await loadLists();
        await ctx.refreshTask?.();
        toast("已停止整片区域的后续核查，已完成的窗口与证据保留。");
      } finally {
        button.disabled = false;
      }
      return true;
    }
    return false;
  }
  function running(id) {
    return (
      !!id &&
      (!state.scanCache?.[id] ||
        state.scanCache[id].execution_status === "running")
    );
  }
  async function planConversation(task) {
    const body = { ...task };
    delete body.location_id;
    delete body.parent_task_id;
    const scan = body.complaint_scan;
    delete body.complaint_scan;
    return await api("/complaints/plan", {
      method: "POST",
      body: { task: body, scan },
    });
  }
  async function startConversation(id) {
    const owner = state.user?.id,
      draft = state.draft;
    if (!id) throw new Error("核查计划不存在，请重新确认范围。");
    const result = await api(
      "/complaints/scans/" + encodeURIComponent(id) + "/start",
      { method: "POST", body: {} },
    );
    if (owner !== state.user?.id || draft !== state.draft) return result;
    await refresh(id);
    await loadLists();
    const first = result.items?.[0];
    if (first && owner === state.user?.id && draft === state.draft)
      await openTask(first.id);
    toast("我已开始逐窗核查，将持续返回各窗口的影像和复核结果。");
    return result;
  }
  return {
    action,
    refresh,
    running,
    planConversation,
    startConversation,
  };
}
