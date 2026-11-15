import { escapeHTML as esc, sourceLabel } from "./lib.mjs";

/** Presentation groups do not change acquisition priority or upstream rights. */
export const SOURCE_GROUPS = Object.freeze([
  {
    id: "current_highres",
    label: "2026 高分影像",
    description:
      "用于查看当前可取得的地表形态。“2026”是图源分组名称，不代表每张影像的拍摄年份。",
    order: [
      "gaofen2026",
      "tianditu",
      "arcgis",
      "bing",
      "mapquest",
      "amap",
      "tencent",
      "google",
      "mapbox",
      "siwei",
      "siwei_daily",
    ],
  },
  {
    id: "continuous_history",
    label: "连贯时序影像",
    description:
      "先查询目标位置可用的历史日期，再选择可比较时相。世纪空间提供 2021–2025 年度版次，实际拍摄日期需逐图核实。",
    order: ["history246", "century"],
  },
  {
    id: "dated_imagery",
    label: "确定时间影像",
    description:
      "按指定年度产品选择。年度版次不是统一拍摄日，结论仍使用返回的实际日期及其适用范围。",
    order: ["siwei2025", "jilin2024"],
  },
  {
    id: "multispectral",
    label: "多光谱",
    description: "按本轮确认的时段检索场景，取得后核对实际拍摄时间与有效覆盖。",
    order: [],
  },
  {
    id: "hyperspectral",
    label: "高光谱",
    description: "按本轮确认的时段检索场景，同时核对波段、质量与实际拍摄时间。",
    order: [],
  },
  {
    id: "sar",
    label: "SAR 雷达",
    description:
      "按本轮确认的时段检索，比较前核对实际日期、轨道、极化与可比性。",
    order: [],
  },
]);

const HISTORY_SOURCES = new Set(["history246", "century"]);
const DATED_SOURCES = new Set(["siwei2025", "jilin2024"]);
const LABELS = {
  history246: "Google 历史时序影像",
  century: "世纪空间年度时序影像",
  google: "Google 普通底图",
  siwei2025: "四维地球 2025 版",
  jilin2024: "吉林一号 2024 版",
};
const DETAILS = {
  history246: "按目标位置查询历史日期与可用影像",
  century: "2021–2025 年度时序，实际拍摄日期逐图核实",
  siwei2025: "2025 年度版，实际拍摄日期以返回信息为准",
  jilin2024: "2024 年度版，实际拍摄日期以返回信息为准",
  google: "普通拼接底图，不作为 Google 历史日期目录",
};

function category(row) {
  if (row.group === "optical") {
    if (HISTORY_SOURCES.has(row.source)) return "continuous_history";
    if (DATED_SOURCES.has(row.source)) return "dated_imagery";
    return "current_highres";
  }
  return ["multispectral", "hyperspectral", "sar"].includes(row.group)
    ? row.group
    : "other";
}

export function groupSources(rows = []) {
  const buckets = new Map(SOURCE_GROUPS.map((group) => [group.id, []]));
  buckets.set("other", []);
  const seen = new Set();
  for (const row of Array.isArray(rows) ? rows : []) {
    if (
      !row ||
      typeof row.source !== "string" ||
      !row.source ||
      seen.has(row.source)
    )
      continue;
    seen.add(row.source);
    buckets.get(category(row)).push(row);
  }
  const groups = SOURCE_GROUPS.map((group) => {
    const positions = new Map(
      group.order.map((source, index) => [source, index]),
    );
    const items = [...buckets.get(group.id)].sort(
      (a, b) =>
        (positions.get(a.source) ?? group.order.length) -
        (positions.get(b.source) ?? group.order.length),
    );
    return { ...group, items };
  });
  if (buckets.get("other").length)
    groups.push({
      id: "other",
      label: "其他已发布来源",
      description: "保留目录中已发布的其他来源，其用途与时相仍需单独核对。",
      items: buckets.get("other"),
    });
  return groups;
}

function rowHTML(row, selected) {
  const selectable = row.selectable === true;
  const label = LABELS[row.source] || row.label || sourceLabel(row.source);
  const reason = selectable
    ? DETAILS[row.source] || "按本地点实际覆盖、质量和权限获取"
    : row.reason === "administrator_disabled"
      ? "管理员已停用"
      : "远端未配置";
  // A stale selected value must never make an unavailable source submit-able.
  return `<label class="source-choice source-picker-choice"><input type="checkbox" name="source" value="${esc(row.source)}" ${selectable && selected.has(row.source) ? "checked" : ""} ${selectable ? "" : "disabled"}><span class="source-picker-copy"><b>${esc(label)}</b><small>${esc(reason)}</small></span></label>`;
}

const WORKFLOW_HINTS = {
  site_investigation: "本轮影像用于点位核查、疑似堆存发现与可见范围判读。",
  complaint_investigation: "本轮影像将围绕举报线索已确认的区域和时段组织核查。",
  environmental_risk: "本轮影像用于场景、风险路径与环境受体关系研判。",
  temporal_change: "本轮按实际拍摄日期比较同一位置，未知时相保留为证据缺口。",
};

export function sourcePicker(rows = [], selected = [], options = {}) {
  const selectedSet = new Set(Array.isArray(selected) ? selected : []);
  const groups = groupSources(rows);
  const hint = WORKFLOW_HINTS[options?.workflow_mode];
  return `<header class="modal-header"><h2>${esc(options?.title || "本轮使用哪些影像")}</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">×</button></header><div class="modal-body source-picker-body"><form id="source-picker-form"><p class="source-picker-intro">留空由系统按当前问题建议。选定后，本轮只在这些来源内取图；底图提供商不等于独立卫星。</p>${hint ? `<p class="source-picker-purpose">${esc(hint)}</p>` : ""}${groups
    .map(
      (group) =>
        `<fieldset class="source-group source-picker-group" data-source-group="${esc(group.id)}"><legend>${esc(group.label)}</legend><p class="source-group-description">${esc(group.description)}</p>${group.items.map((row) => rowHTML(row, selectedSet)).join("") || '<p class="source-group-empty">当前目录暂无该类图源</p>'}</fieldset>`,
    )
    .join(
      "",
    )}<footer class="source-picker-footer"><button class="primary" type="submit">保存本轮选择</button></footer></form></div>`;
}
