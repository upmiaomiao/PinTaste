import test from "node:test";
import assert from "node:assert/strict";
import {
  createWorkspaceController,
  renderDialogueTranscript,
} from "./workspace.mjs";

function setup(api, extra = {}) {
  const state = { draft: {}, user: { id: "owner" }, guide: null },
    sent = [];
  const controller = createWorkspaceController({
    state,
    api,
    render() {},
    showModal() {},
    closeModal() {},
    toast() {},
    submitBody: (b) => sent.push(b),
    updateComposer() {},
    ...extra,
  });
  return { state, sent, controller };
}
test("cancelled preparation cannot re-open questions or submit paid work", async () => {
  let finish;
  const h = setup(() => new Promise((resolve) => (finish = resolve)));
  h.state.guide = {
    body: { objective: "举报线索" },
    question: { kind: "complaint_goal" },
  };
  const pending = h.controller.start(h.state.guide.body);
  await h.controller.action("guide-cancel", {});
  finish({ ready: true, task: { objective: "must not submit" } });
  await pending;
  assert.equal(h.sent.length, 0);
  assert.equal(h.state.guide, null);
  assert.equal(h.state.dialoguePending, false);
});
test("old response never clears a newer question pending state", async () => {
  const calls = [];
  const h = setup(() => new Promise((r) => calls.push(r)));
  const first = h.controller.start({ objective: "first" });
  const second = h.controller.start({ objective: "second" });
  calls[0]({ ready: false, question: { kind: "old" } });
  await first;
  assert.equal(h.state.dialoguePending, true);
  calls[1]({ ready: false, question: { kind: "new" } });
  await second;
  assert.equal(h.state.guide.question.kind, "new");
  assert.equal(h.state.dialoguePending, false);
});
test("logout or a different draft discards late ready/plan results", async () => {
  let finish;
  const h = setup(() => new Promise((r) => (finish = r)));
  const pending = h.controller.start({ objective: "private clue" });
  h.controller.reset();
  h.state.user = { id: "other" };
  h.state.draft = {};
  h.state.dialogueTurns = [];
  finish({ ready: true, task: { objective: "old user" } });
  await pending;
  assert.equal(h.sent.length, 0);
  assert.equal(h.state.guide, null);
  assert.deepEqual(h.state.dialogueTurns, []);
});
test("complaint ready plans a scan but waits for explicit start confirmation", async () => {
  const plans = [],
    starts = [];
  const h = setup(
    async () => ({
      ready: true,
      mode: "complaint_scan",
      dialogue: { mode: "complaint" },
      task: {
        objective: "核查举报",
        complaint_scan: { region_confirmed: true },
      },
    }),
    {
      planComplaint: async (b) => {
        plans.push(b);
        return {
          scan_id: "scan",
          plan: { status: "ready", windows: [{}, {}] },
        };
      },
      startComplaint: async (id) => starts.push(id),
    },
  );
  await h.controller.start({ objective: "核查举报" });
  assert.equal(plans.length, 1);
  assert.equal(h.sent.length, 0);
  assert.equal(starts.length, 0);
  assert.equal(h.state.guide.question.kind, "complaint_plan");
  await h.controller.action("guide-select", { dataset: { value: "start" } });
  assert.equal(starts.length, 0);
  // The UI's explicit confirmation action reads selected value (no free text).
  const before = globalThis.document;
  globalThis.document = { querySelector: () => null };
  try {
    await h.controller.action("guide-next", {});
  } finally {
    globalThis.document = before;
  }
  assert.deepEqual(starts, ["scan"]);
});
test("negative imagery permission never becomes consent through the word platform", async () => {
  for (const text of [
    "别用平台影像",
    "禁止平台取图",
    "不允许平台辅助",
    "只分析上传图片",
  ]) {
    const requests = [];
    const h = setup(async (_p, o) => {
      requests.push(o.body);
      return { ready: true };
    });
    h.state.guide = {
      body: { objective: "看上传图片" },
      question: { kind: "online_supplement" },
    };
    await h.controller.free(text);
    assert.equal(requests[0].online_supplement, "uploaded_only", text);
  }
});
test("map-origin correction wins over a negated GPS phrase", async () => {
  const paths = [];
  const h = setup(async (path) => {
    paths.push(path);
    return path === "/coordinates/confirm"
      ? { center: [104, 31], location_id: "match" }
      : { ready: true };
  });
  h.state.guide = {
    body: { objective: "检查", center: [104, 31] },
    question: { kind: "coordinate_basis" },
  };
  await h.controller.free("不是GPS，是高德坐标");
  assert.equal(paths[0], "/coordinates/confirm");
});
test("dialogue renders quoted material safely and the selected task type remains data", () => {
  const html = renderDialogueTranscript([
    { role: "user", text: "<script>steal()</script>" },
    { role: "assistant", text: "请确认范围" },
  ]);
  assert.ok(!html.includes("<script>"));
  assert.ok(html.includes("&lt;script&gt;"));
  assert.match(html, /请确认范围/);
});

test('opening a submitted task unlocks its fresh composer draft', async()=>{
 const state={draft:{},user:{id:'owner'}};
 const controller=createWorkspaceController({state,api:async()=>({ready:true}),render(){},submitBody:async()=>{state.draft={};}});
 await controller.start({objective:'normal task'});
 assert.equal(state.dialoguePending,false);
});
