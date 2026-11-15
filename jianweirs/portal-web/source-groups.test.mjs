import test from "node:test";
import assert from "node:assert/strict";
import { groupSources, sourcePicker } from "./source-groups.mjs";

const optical = (source, more = {}) => ({
  source,
  group: "optical",
  selectable: true,
  ...more,
});

test("published optical rows are separated into current, historical and dated groups", () => {
  const input = [
    "jilin2024",
    "bing",
    "century",
    "google",
    "history246",
    "siwei2025",
    "gaofen2026",
    "arcgis",
    "tianditu",
  ].map((s) => optical(s));
  const before = structuredClone(input);
  const groups = groupSources(input);
  assert.deepEqual(
    groups.slice(0, 3).map((g) => g.label),
    ["2026 高分影像", "连贯时序影像", "确定时间影像"],
  );
  assert.deepEqual(
    groups[0].items.map((r) => r.source),
    ["gaofen2026", "tianditu", "arcgis", "bing", "google"],
  );
  assert.deepEqual(
    groups[1].items.map((r) => r.source),
    ["history246", "century"],
  );
  assert.deepEqual(
    groups[2].items.map((r) => r.source),
    ["siwei2025", "jilin2024"],
  );
  assert.deepEqual(
    input,
    before,
    "UI grouping must not mutate the acquisition catalogue or its priority",
  );
});

test("ordinary Google imagery is never renamed or grouped as a historical date source", () => {
  const groups = groupSources([optical("google"), optical("history246")]);
  assert.deepEqual(
    groups[0].items.map((r) => r.source),
    ["google"],
  );
  assert.deepEqual(
    groups[1].items.map((r) => r.source),
    ["history246"],
  );
  const html = sourcePicker([optical("google"), optical("history246")]);
  assert.match(html, /Google 普通底图/);
  assert.match(html, /不作为 Google 历史日期目录/);
  assert.match(html, /Google 历史时序影像/);
});

test("fixed-year product editions are not claimed to have one verified capture date", () => {
  const html = sourcePicker([
    optical("siwei2025"),
    optical("jilin2024"),
    optical("century"),
  ]);
  assert.match(html, /年度版次不是统一拍摄日/);
  assert.match(html, /实际拍摄日期逐图核实/);
  assert.match(html, /实际拍摄日期以返回信息为准/);
  assert.match(html, /不代表每张影像的拍摄年份/);
  assert.ok(
    !html.includes('type="date"'),
    "the picker cannot fabricate per-image capture dates",
  );
});

test("unavailable or administratively disabled sources cannot submit through stale selection", () => {
  const html = sourcePicker(
    [
      optical("mapbox", {
        selectable: false,
        reason: "upstream_not_configured",
      }),
      optical("bing", { selectable: false, reason: "administrator_disabled" }),
      optical("arcgis"),
    ],
    ["mapbox", "bing", "arcgis"],
  );
  assert.match(html, /value="mapbox"[^>]*disabled/);
  assert.match(html, /value="bing"[^>]*disabled/);
  assert.doesNotMatch(html, /value="mapbox"[^>]*checked/);
  assert.doesNotMatch(html, /value="bing"[^>]*checked/);
  assert.match(html, /value="arcgis" checked/);
  assert.match(html, /管理员已停用/);
  assert.match(html, /远端未配置/);
});

test("science modalities follow the optical groups and retain their exact source identifiers", () => {
  const rows = [
    { source: "emit", group: "hyperspectral", selectable: true },
    { source: "sentinel1-rtc", group: "sar", selectable: true },
    { source: "sentinel2", group: "multispectral", selectable: true },
  ];
  const groups = groupSources(rows);
  assert.deepEqual(
    groups.slice(3).map((g) => g.id),
    ["multispectral", "hyperspectral", "sar"],
  );
  assert.deepEqual(
    groups.slice(3).map((g) => g.items[0].source),
    ["sentinel2", "emit", "sentinel1-rtc"],
  );
  const html = sourcePicker(rows);
  assert.ok(
    html.indexOf('data-source-group="multispectral"') <
      html.indexOf('data-source-group="hyperspectral"'),
  );
  assert.ok(
    html.indexOf('data-source-group="hyperspectral"') <
      html.indexOf('data-source-group="sar"'),
  );
});

test("unknown published rows are retained safely without fabricating unreturned options", () => {
  const html = sourcePicker([
    optical("custom_optical", { label: '<img src=x onerror="bad">' }),
    {
      source: "other_sensor",
      group: "experimental",
      selectable: false,
      label: "其他探测器",
    },
    optical("custom_optical"),
  ]);
  assert.equal((html.match(/value="custom_optical"/g) || []).length, 1);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /其他已发布来源/);
  assert.ok(
    !html.includes('value="gaofen2026"'),
    "only real directory rows are offered",
  );
});

test("task-specific picker guidance preserves the same safe form contract", () => {
  const html = sourcePicker([optical("bing")], [], {
    workflow_mode: "environmental_risk",
    title: "风险研判影像 <test>",
  });
  assert.match(html, /风险路径与环境受体/);
  assert.match(html, /风险研判影像 &lt;test&gt;/);
  assert.match(html, /id="source-picker-form"/);
  assert.match(html, /name="source"/);
  assert.match(html, /不等于独立卫星/);
});
