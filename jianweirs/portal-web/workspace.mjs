import {sourcePicker} from "./source-groups.mjs";
export {sourcePicker};
import { validateScanBBox } from "./complaints.mjs";
import {
  escapeHTML as esc,
  sourceLabel,
  parseCenter,
  taskPayload,
} from "./lib.mjs";

export function questionCard(guide) {
  if (!guide?.question) return "";
  const q = guide.question,
    hasFreeText = !!guide.freeText?.trim();
  return `<section class="dialogue-question ${guide.collapsed ? "collapsed" : ""}" aria-live="polite" aria-label="需要你确认的问题"><header><span>我先确认一个关键点</span><div><button type="button" class="icon-button" data-action="guide-collapse" aria-label="${guide.collapsed ? "展开问题" : "收起问题"}">${guide.collapsed ? "⌃" : "⌄"}</button><button type="button" class="icon-button" data-action="guide-cancel" aria-label="放弃这次提问" title="放弃这次提问，不会影响已有任务">×</button></div></header><div class="question-body"><strong>${esc(q.question)}</strong><div class="question-options">${(q.options || []).map((o, i) => `<button type="button" ${guide.pending ? "disabled" : ""} class="question-option ${guide.selected === o.value ? "selected" : ""}" data-action="guide-select" data-value="${esc(o.value)}" aria-pressed="${guide.selected === o.value}"><span class="option-number">${i + 1}</span><span class="question-option-copy"><b>${esc(o.label)}</b>${o.description ? `<small>${esc(o.description)}</small>` : ""}</span></button>`).join("")}</div><label class="question-free-label ${hasFreeText ? "has-answer" : ""}"><span class="custom-answer-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m14 5 5 5M4 20l4-1L20 7a2 2 0 0 0-4-4L4 15Z"/></svg></span><span class="sr-only">也可以直接回答</span><input id="guide-free" maxlength="2000" placeholder="输入你的答案，或直接告诉我你的想法" value="${esc(guide.freeText || "")}"></label><footer><span>确认后，我会按你的选择继续</span><button type="button" class="primary" data-action="guide-next" ${guide.pending || (!guide.selected && !hasFreeText) ? "disabled" : ""}>${guide.selected || hasFreeText ? "确认并继续" : "下一步"}</button></footer></div></section>`;
}

export function locationPicker(result = null) {
  return `<header class="modal-header"><h2>查找并确认地点</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">×</button></header><div class="modal-body"><form id="location-search-form" class="location-search"><label class="field">地名或详细地址<input name="query" required maxlength="200" placeholder="例如成都市青白江区某园区"></label><label class="field">所在城市 可选<input name="city" maxlength="80" placeholder="成都"></label><button class="primary" type="submit">搜索候选地点</button></form><div class="location-results">${result ? (result.candidates || []).map((c) => `<button class="location-candidate" data-action="location-confirm" data-id="${esc(c.id)}"><strong>${esc(c.name)}</strong><span>${esc(c.address || "")}</span><small>选择这个检索位置</small></button>`).join("") || "<p>没有匹配地点，请补充城市和地址，或直接填写经纬度。</p>" : ""}</div><p class="location-note">地点结果用于确定检索区域。高德坐标转换为近似 WGS84，并不证明疑似堆体的精确位置；可在后续图像中核对。</p></div>`;
}

export function archivePicker(plan) {
  return `<header class="modal-header"><h2>选择压缩包中的影像</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">×</button></header><div class="modal-body"><p>原 ZIP 已保留。本次最多添加 8 张显示图片，拍摄日期与位置仍需核实。</p><form id="archive-images-form"><div class="source-group">${(plan.files || []).map((f) => `<label class="source-choice"><input type="checkbox" name="member" value="${esc(f.id)}" ${f.status === "ready" && f.import_kind === "display_image" ? "" : "disabled"}><span>${esc(f.original_filename)}<small>${f.import_kind === "numeric_raster" ? "数值栅格已保留，需先配置模态和波段" : f.import_kind === "display_image" ? "可添加到本轮影像" : f.status === "unsupported" ? "尚未支持此格式" : "元数据或未通过影像检查"}</small></span></label>`).join("")}</div><button type="submit" class="primary">添加所选图片</button></form></div>`;
}

export function nextQuestions(task) {
  if (!task || !["succeeded", "completed"].includes(task.status)) return [];
  const hasImages = (task.images || []).length > 0;
  if (!hasImages) return [];
  const pkg = task.package || task.result || {};
  const recommendations =
    task.narrative?.recommendations || pkg.narrative?.recommendations || [];
  return recommendations
    .filter((q) => typeof q === "string" && q.trim())
    .slice(0, 2)
    .map((q) => "继续核查：" + q.slice(0, 220));
}

export function createWorkspaceController(ctx) {
  const {
    state,
    api,
    render,
    showModal,
    closeModal,
    toast,
    submitBody,
    updateComposer,
  } = ctx;
  let generation = 0;
  function turn(role, text) {
    state.dialogueTurns ??= [];
    if (text && state.dialogueTurns.at(-1)?.text !== text)
      state.dialogueTurns.push({ role, text: String(text).slice(0, 12000) });
  }
  async function start(body, reply = null) {
    const draft = state.draft,
      owner = state.user?.id,
      ticket = ++generation;
    const dialogue = state.guide?.dialogue || {};
    if (!state.dialogueTurns?.length) turn("user", body.objective);
    if (state.guide) state.guide.pending = true;
    state.dialoguePending = true;
    render?.();
    try {
      const result = await api("/conversation/prepare", {
        method: "POST",
        body: {
          ...body,
          ...(Object.keys(dialogue).length ? { _dialogue: dialogue } : {}),
          ...(reply ? { _reply: reply } : {}),
        },
      });
      if (
        ticket !== generation ||
        draft !== state.draft ||
        owner !== state.user?.id
      )
        return;
      const prepared = result.task || body;
      if (!result.ready) {
        turn("assistant", result.question?.question);
        state.guide = {
          body: prepared,
          dialogue: result.dialogue || dialogue,
          question: result.question,
        };
        state.draft.objective = "";
        state.busy = false;
        return;
      }
      if (result.messages?.length && ctx.addMaterialEvent)
        for (const message of result.messages)
          ctx.addMaterialEvent(
            "我找到了你提到的检索区域",
            typeof message === "string"
              ? message
              : message.summary || message.message || "已匹配检索区域",
          );
      if (result.mode === "complaint_scan") {
        const planned = await ctx.planComplaint(prepared);
        if (ticket !== generation || draft !== state.draft) return;
        const count = planned.plan?.windows?.length || 0,
          ready = planned.plan?.status === "ready";
        const message = ready
          ? `范围和时段已整理好。我将分成 ${count} 个窗口逐一核查，并保留未覆盖或不能判读的区域。是否开始？`
          : `这片范围暂时不能直接开始：${planned.plan?.next_action || "请缩小区域后重新规划。"}`;
        turn("assistant", message);
        state.guide = {
          body: prepared,
          dialogue: result.dialogue || dialogue,
          scan_id: planned.scan_id,
          question: {
            kind: "complaint_plan",
            question: message,
            options: [
              ...(ready
                ? [
                    {
                      value: "start",
                      label: `开始核查 ${count} 个窗口`,
                      description: "按确认范围取图、逐窗复核并记录覆盖缺口。",
                    },
                  ]
                : []),
              {
                value: "revise",
                label: "调整核查范围",
                description: "先修改边界，再重新核对窗口数量。",
              },
            ],
          },
        };
        state.draft.objective = "";
        state.busy = false;
        return;
      }
      state.guide = null;
      await submitBody(prepared);
    } finally {
      if (ticket === generation && owner === state.user?.id) {
        state.dialoguePending = false;
        if (state.guide) state.guide.pending = false;
        render?.();
        if (state.guide) ctx.scrollLatest?.();
      }
    }
  }
  async function attachmentsChanged() {
    if (!state.guide) return;
    const files = taskPayload({
      ...state.draft,
      objective: state.guide.body.objective,
    });
    const body = { ...state.guide.body };
    if (files.asset_ids) body.asset_ids = files.asset_ids;
    if (files.document_ids) body.document_ids = files.document_ids;
    await start(body);
  }
  async function answer(value) {
    const guide = state.guide;
    if (!guide) return;
    const body = { ...guide.body },
      kind = guide.question.kind;
    const selected = guide.question.options?.find((o) => o.value === value);
    if (selected) turn("user", selected.label);
    if (
      [
        "material_goal",
        "waste_confirmation",
        "complaint_goal",
        "complaint_region",
        "complaint_region_confirm",
        "complaint_period",
        "science_period",
        "policy_region",
      ].includes(kind)
    ) {
      if (value === "custom" || value === "local") {
        toast(
          kind === "complaint_region"
            ? "请在对话中填写 WGS84 西、南、东、北四个数值。"
            : kind === "policy_region"
              ? "请直接输入所在省、市或地区。"
              : "请直接告诉我希望核查的年份或起止日期。",
        );
        return;
      }
      await start(body, { kind, value });
      return;
    }
    if (kind === "complaint_plan") {
      if (value === "start") {
        state.guide.pending = true;
        state.busy = true;
        render();
        try {
          await ctx.startComplaint(guide.scan_id);
          state.guide = null;
        } finally {
          state.busy = false;
          render();
        }
        return;
      }
      if (value === "revise") {
        body.complaint_scan = {
          ...body.complaint_scan,
          region_confirmed: false,
        };
        delete body.complaint_scan.region_bbox;
        await start(body);
        return;
      }
    }
    if (kind === "location_match") {
      const result = await api("/locations/confirm", {
        method: "POST",
        body: { search_id: guide.question.search_id, candidate_id: value },
      });
      state.draft.center = result.center.join(", ");
      state.draft.location_id = result.location_id;
      state.draft.coordinate_system = "wgs84";
      await start({
        ...body,
        center: result.center,
        coordinate_system: "wgs84",
        location_id: result.location_id,
      });
      return;
    }
    if (value === "gcj02") {
      const center = body.center || state.active?.task?.center;
      if (!center) {
        showModal(locationPicker());
        return;
      }
      const result = await api("/coordinates/confirm", {
        method: "POST",
        body: { center, coordinate_system: "gcj02" },
      });
      state.draft.center = result.center.join(", ");
      state.draft.location_id = result.location_id;
      state.draft.coordinate_system = "wgs84";
      await start({
        ...body,
        center: result.center,
        coordinate_system: "wgs84",
        location_id: result.location_id,
      });
      return;
    }
    if (value === "search_place" || value === "unknown") {
      showModal(locationPicker());
      return;
    }
    if (value === "coordinates") {
      toast("请输入经度、纬度，并说明来自 GPS 还是高德地图。");
      return;
    }
    if (value === "upload_images" || value === "upload_dated") {
      document.querySelector("#file-input")?.click();
      return;
    }
    if (kind === "online_supplement") {
      body.online_supplement = value;
      state.draft.online_supplement = value;
      if (value === "uploaded_only") {
        body.acquire_optical = false;
        body.selected_sources = [];
        state.draft.selected_sources = [];
      }
    }
    if (kind === "coordinate_basis" && value === "wgs84")
      body.coordinate_system = "wgs84";
    if (
      kind === "comparison_period" &&
      ["since2021", "recent90", "this_year"].includes(value)
    ) {
      await start(body, { kind, value });
      return;
    }
    if (kind === "comparison_period") {
      if (value === "provide_period") {
        toast("请输入希望比较的开始和结束日期。");
        return;
      }
      if (value === "current_only") {
        // The backend narrows the task; the user's own words stay intact.
        await start(body, { kind, value });
        return;
      }
    }
    await start(body);
  }
  async function free(text) {
    const guide = state.guide;
    if (!guide) return false;
    const body = { ...guide.body },
      kind = guide.question.kind,
      center = parseCenter(text);
    turn("user", text);
    if (kind === "waste_confirmation") {
      const value = /未确认|没确认|没有确认|不确定|尚未|先评估|可能风险/.test(text)
        ? "unverified"
        : /已确认|已经确认|现场确认|确认存在|确定有|确认了/.test(text)
          ? "user_confirmed"
          : null;
      if (value) {
        await start(body, { kind, value });
        return true;
      }
    }
    if (kind === "complaint_goal") {
      const value = /变化|清理|历史|何时|什么时候|时序/.test(text)
        ? "changes"
        : /风险|环境|河|居民|沟渠/.test(text)
          ? "environment"
          : /有没有|是否|存在|堆存|类型|核查/.test(text)
            ? "discovery"
            : null;
      if (value) {
        await start(body, { kind, value });
        return true;
      }
    }
    if (kind === "material_goal") {
      const value = /整理|摘要|总结|先读/.test(text)
        ? "summary"
        : /核查|取图|卫星|扫描/.test(text)
          ? "complaint"
          : null;
      if (value) {
        await start(body, { kind, value });
        return true;
      }
    }
    if (kind === "complaint_region_confirm") {
      if (/调整|重选|不对|不要|修改/.test(text)) {
        await start(body, { kind, value: "revise" });
        return true;
      }
      if (/确认|可以|按这个|就这/.test(text)) {
        await start(body, { kind, value: "confirm" });
        return true;
      }
    }
    if (
      kind === "policy_region" &&
      /^(?:先查|只查|仅查)?(?:国家|全国)/.test(text)
    ) {
      await start(body, { kind, value: "national" });
      return true;
    }
    if (
      kind === "complaint_plan" &&
      /^(?:开始|确认开始|按计划开始|可以开始)[。！!\s]*$/.test(text.trim())
    ) {
      await answer("start");
      return true;
    }

    if (
      ["comparison_period", "science_period", "complaint_period"].includes(kind)
    ) {
      if (/(?:历史|过去|最早).*(?:最近|最新|现在)|从2021/.test(text)) {
        await start(body, { kind, value: "since2021" });
        return true;
      }
      if (/近.{0,2}(?:3|三)个?月/.test(text)) {
        await start(body, { kind, value: "recent90" });
        return true;
      }
      if (/今年至今|今年到现在/.test(text)) {
        await start(body, { kind, value: "this_year" });
        return true;
      }
      if (kind === "complaint_period" && /最新|近期|现在/.test(text)) {
        await start(body, { kind, value: "latest" });
        return true;
      }
    }

    if (kind === "complaint_region" || kind === "complaint_region_confirm") {
      try {
        const bounds = validateScanBBox(text);
        body.complaint_scan = {
          ...body.complaint_scan,
          region_bbox: bounds,
          region_confirmed: false,
        };
        await start(body);
        return true;
      } catch {}
      if (/^(?:周边|半径|大约|约)?\s*(250|500)\s*(?:米)?$/.test(text.trim())) {
        await start(body, {
          kind: "complaint_region",
          value: text.match(/250|500/)[0],
        });
        return true;
      }
      toast("请填写四个边界数值，或选择周边250米、500米。", true);
      return true;
    }
    if (kind === "policy_region") {
      if (/不知道|不清楚|不确定|暂不/.test(text)) {
        toast("可以先选择国家层面的规定，知道省市后再补地方政策。");
        return true;
      }
      if (text.trim().length < 2 || text.length > 200) {
        toast("请补充省、市或地区名称。", true);
        return true;
      }
      body.jurisdiction = text.trim();
      await start(body);
      return true;
    }
    if (kind === "complaint_plan") {
      toast("请先确认开始，或选择调整范围。");
      return true;
    }
    if (center) {
      body.center = center;
      delete body.location_id;
      body.coordinate_system = /WGS\s*84|GPS|4326/i.test(text)
        ? "wgs84"
        : "unknown";
      await start(body);
      return true;
    }
    if (kind === "location" || kind === "location_match") {
      body.objective += "\n地点补充：" + text;
      await start(body);
      return true;
    }
    if (kind === "coordinate_basis") {
      if (/高德|腾讯|GCJ[\s_-]*02/i.test(text)) {
        await answer("gcj02");
        return true;
      }
      if (
        /WGS\s*84|GPS|4326/i.test(text) &&
        !/(?:不是|非|不确定|不知)[^，。；]{0,8}(?:WGS|GPS|4326)/i.test(text)
      ) {
        await answer("wgs84");
        return true;
      }
      toast(
        "请说明坐标来自GPS/WGS84，还是高德、腾讯地图；不确定时可重新搜索地名。",
      );
      return true;
    }
    if (kind === "online_supplement") {
      const deny =
        /(?:别|不要|不准|禁止|勿|拒绝|无需|不允许|不能|不使用|不用|不需)[^，。；]{0,12}(?:平台|卫星|取图|影像)|^(?:只|仅)[^，。；]{0,10}(?:上传|已有)/.test(
          text,
        );
      const allow =
        /^(?:允许|可以|同意|需要|请)[^，。；]{0,15}(?:平台|卫星|补充|辅助|取图|影像)|^(?:可以|同意|允许)[。！!\s]*$/.test(
          text,
        );
      if (deny) await answer("uploaded_only");
      else if (allow) await answer("allowed");
      else toast("请明确选择只分析上传影像，还是允许平台补充影像。");
      return true;
    }
    // Free text answers the pending question; it is not appended to the
    // objective, so routing keywords in the reply cannot change the mode.
    await start(body, { kind: "free_text", value: text });
    return true;
  }
  async function action(action, button) {
    if (action === "guide-select") {
      if (state.guide) {
        state.guide.selected = button.dataset.value;
        state.guide.freeText = "";
        render();
      }
      return true;
    }
    if (action === "guide-next") {
      if (state.guide?.pending || state.dialoguePending) return true;
      const text = document.querySelector("#guide-free")?.value?.trim();
      if (text) await free(text);
      else if (state.guide?.selected) await answer(state.guide.selected);
      return true;
    }
    if (action === "guide-collapse") {
      if (state.guide) {
        state.guide.collapsed = !state.guide.collapsed;
        render();
      }
      return true;
    }
    if (action === "guide-cancel") {
      reset();
      state.draft.objective =
        state.guide?.body.objective || state.draft.objective;
      state.guide = null;
      state.busy = false;
      render();
      return true;
    }
    if (action === "archive-open") {
      state.archivePlan = await api(
        "/uploads/" + encodeURIComponent(button.dataset.id) + "/archive",
      );
      ctx.addMaterialEvent?.(
        "我看过压缩包里的材料了",
        `我读到了 ${state.archivePlan.files?.length || 0} 个文件，已区分显示影像、数值栅格和附属资料。选好要分析的图片，我就可以继续。`,
      );
      showModal(archivePicker(state.archivePlan), true);
      return true;
    }
    if (action === "guide-answer") {
      await answer(button.dataset.value);
      return true;
    }
    if (action === "location-open") {
      showModal(locationPicker());
      return true;
    }
    if (action === "sources-open") {
      const result = await api("/source-choices");
      state.sourceChoices = result.sources || [];
      showModal(
        sourcePicker(state.sourceChoices, state.draft.selected_sources || [], {workflow_mode:state.guide?.body?.workflow_mode||state.active?.task?.workflow_mode}),
        true,
      );
      return true;
    }
    if (action === "location-confirm") {
      const result = await api("/locations/confirm", {
        method: "POST",
        body: {
          search_id: state.locationSearch.search_id,
          candidate_id: button.dataset.id,
        },
      });
      state.draft.center = result.center.join(", ");
      state.draft.location_id = result.location_id;
      state.draft.coordinate_system = "wgs84";
      closeModal();
      if (state.guide)
        await start({
          ...state.guide.body,
          center: result.center,
          coordinate_system: "wgs84",
          location_id: result.location_id,
        });
      else {
        updateComposer();
        toast("已确认检索地点，可以继续描述问题。");
      }
      return true;
    }
    if (action === "next-question") {
      state.draft.objective =
        nextQuestions(state.active)[Number(button.dataset.index)] || "";
      if (state.draft.objective.includes("选择另外")) {
        const result = await api("/source-choices");
        state.sourceChoices = result.sources || [];
        showModal(sourcePicker(state.sourceChoices, []), true);
      }
      updateComposer();
      document.querySelector("#objective")?.focus();
      return true;
    }
    return false;
  }
  async function submit(event) {
    const form = event.target;
    if (form.id === "archive-images-form") {
      const ids = new FormData(form).getAll("member");
      if (!ids.length || ids.length > 8) {
        toast("每轮请选择 1 至 8 张显示图片。", true);
        return true;
      }
      const result = await api(
        "/uploads/" + state.archivePlan.archive_id + "/archive/images",
        { method: "POST", body: { member_ids: ids } },
      );
      const zip = state.draft.files.find((f) => f.id === result.archive_id);
      if (zip) zip.assets = result.assets;
      else
        for (const asset of result.assets || [])
          if (!state.draft.files.some((f) => f.id === asset.id))
            state.draft.files.push(asset);
      closeModal();
      updateComposer();
      toast(
        "已添加 " +
          result.assets.length +
          " 张图片，选好后告诉我你想核查什么，我会接着分析。",
      );
      await attachmentsChanged();
      return true;
    }
    if (form.id === "source-picker-form") {
      state.draft.selected_sources = new FormData(form).getAll("source");
      if (state.guide)
        state.guide.body.selected_sources = [...state.draft.selected_sources];
      closeModal();
      updateComposer();
      toast(
        state.draft.selected_sources.length
          ? "已保存本轮来源选择"
          : "本轮由系统按问题建议图源",
      );
      return true;
    }
    if (form.id === "location-search-form") {
      const data = new FormData(form),
        button = form.querySelector("button");
      button.disabled = true;
      try {
        state.locationSearch = await api("/locations/search", {
          method: "POST",
          body: { query: data.get("query"), city: data.get("city") },
        });
        showModal(locationPicker(state.locationSearch));
      } finally {
        button.disabled = false;
      }
      return true;
    }
    return false;
  }
  function reset() {
    generation++;
    state.dialoguePending = false;
    if (state.guide) state.guide.pending = false;
  }
  return { start, free, action, submit, attachmentsChanged, reset };
}

export function renderDialogueTranscript(turns = []) {
  return turns.length
    ? `<section class="dialogue-transcript" aria-label="需求确认对话">${turns.map((t) => `<div class="dialogue-turn dialogue-${t.role === "user" ? "user" : "assistant"}"><span>${t.role === "user" ? "你" : "无隅见微"}</span><p>${esc(t.text)}</p></div>`).join("")}</section>`
    : "";
}
