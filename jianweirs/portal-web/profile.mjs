import { escapeHTML as esc, safeURL } from "./lib.mjs";
import { applyAppearance } from "./appearance.mjs";

export function profileButton(user, icon, compact = false) {
  const name = user.display_name || user.username || "用户";
  return `<button type="button" class="profile-trigger ${compact ? "compact" : ""}" data-action="profile" aria-label="个人中心" aria-haspopup="dialog" title="个人中心"><span class="avatar">${esc(name.slice(0, 1))}</span><span class="account-text"><strong>${esc(name)}</strong>${compact ? "" : "<small>个人中心与设置</small>"}</span>${compact ? "" : icon("chevron")}</button>`;
}

const TABS = [
  ["account", "账号资料", "users"],
  ["quota", "积分与额度", "database"],
  ["appearance", "外观设置", "settings"],
];
const THEMES = [
  ["light", "浅色"],
  ["dark", "深色"],
  ["system", "跟随系统"],
];
const number = (n) =>
  Number.isSafeInteger(n) && n >= 0 ? n.toLocaleString("zh-CN") : "—";

export function quotaCard(label, used, limit) {
  const validUsed = Number.isSafeInteger(used) && used >= 0;
  const validLimit = Number.isSafeInteger(limit) && limit >= 0;
  const available =
    limit === null
      ? "不限额"
      : validUsed && validLimit
        ? number(Math.max(0, limit - used))
        : "—";
  return `<article class="personal-quota-card"><span>${esc(label)}剩余</span><strong>${available}<small>${limit === null || available === "—" ? "" : " 次"}</small></strong><p>本月已用 ${number(used)} 次${limit === null ? "" : ` / 额度 ${number(limit)} 次`}</p>${validUsed && validLimit ? `<progress value="${Math.min(used, limit)}" max="${Math.max(limit, 1)}" aria-label="${esc(label)}本月已用比例"></progress>` : ""}</article>`;
}

export function profileContents(model, icon) {
  const { data, tab, loading, saving, error, note, credits } = model;
  const p = data.preferences || {},
    a = data.account || {},
    user = data.user || {};
  const disabled = loading || saving ? "disabled" : "";
  const name = a.name || user.display_name || user.username || "用户";
  let content;
  if (tab === "account") {
    content = `<div class="personal-identity"><span class="avatar">${esc(name.slice(0, 1))}</span><div><strong>${esc(name)}</strong><p>${esc(a.email || a.legacy_alias || (data.shared_account ? "无隅 AI 统一账号" : user.username))}</p></div><span class="personal-role">${user.role === "admin" ? "见微管理员" : "见微用户"}</span></div><form id="personal-profile-form"><label class="field">姓名<input name="name" autocomplete="name" maxlength="60" required value="${esc(name)}" ${disabled}></label>${data.shared_account ? `<label class="field">所在机构<input name="organization" autocomplete="organization" maxlength="160" required value="${esc(a.organization || "")}" ${disabled}></label><label class="field">身份<select name="identity" required ${disabled}><option value="" disabled ${!a.identity ? "selected" : ""}>请选择身份</option>${(data.identity_options || []).map((v) => `<option value="${esc(v)}" ${a.identity === v ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label><p class="personal-note">账号资料与无隅 AI 平台同步，外观设置仅用于见微。</p>` : ""}<button class="primary" type="submit" ${disabled}>${saving ? "正在保存…" : "保存资料"}</button></form><div class="personal-account-actions">${safeURL(data.account_url) ? `<a class="secondary" href="${esc(safeURL(data.account_url))}" target="_blank" rel="noopener noreferrer">管理无隅 AI 账号 ${icon("arrow")}</a>` : ""}<button class="secondary" data-action="logout">${icon("logout")}退出登录</button></div>`;
  } else if (tab === "quota") {
    const status = credits?.status;
    content = `<div class="personal-section-heading"><h3>无隅 AI 工作积分</h3><button class="icon-button" data-action="profile-refresh" aria-label="刷新积分与额度" title="刷新积分与额度" ${loading || status === "loading" ? "disabled" : ""}>${icon("refresh")}</button></div><div class="personal-points"><span>可用积分</span><strong>${status === "available" ? number(credits.available) : "—"}</strong><p>${status === "available" ? `处理中预留 ${number(credits.reserved)} 积分` : esc(status === "loading" || !status ? "正在读取平台积分…" : credits.message || "暂时无法读取平台积分")}</p></div><p class="personal-note">积分由无隅 AI 平台统一管理。见微遥感任务按下方的独立额度使用。</p><h3>见微本月额度</h3><div class="personal-quota-grid">${quotaCard("影像获取", p.usage?.imagery, p.imagery_quota)}${quotaCard("问答任务", p.usage?.qa, p.qa_quota)}</div><p class="personal-note">统计月份：${esc(p.period || "—")} · 每月按北京时间重置。额度调整请联系见微系统管理员。</p>${status === "available" && credits.updated_at ? `<p class="personal-sync">积分更新时间 ${esc(new Date(credits.updated_at).toLocaleString("zh-CN", { hour12: false }))}</p>` : ""}`;
  } else {
    content = `<h3>主题</h3><div class="personal-theme-choices" role="group" aria-label="界面主题">${THEMES.map(([id, label]) => `<button type="button" class="theme-choice" data-action="profile-theme" data-theme-value="${id}" aria-pressed="${(p.theme || "system") === id}" ${disabled}><span class="theme-preview theme-preview-${id}" aria-hidden="true"><i></i><b></b><em></em></span><span>${label}</span>${(p.theme || "system") === id ? icon("check") : ""}</button>`).join("")}</div><label class="personal-preference-row"><span>阅读字号<small>调整对话、分析过程和结论的字号</small></span><select name="personal-font-size" aria-label="阅读字号" ${disabled}><option value="" ${p.font_size_override == null ? "selected" : ""}>跟随系统设置（${p.font_size_override == null ? p.font_size || 16 : "默认"}）</option>${Array.from(
      { length: 13 },
      (_, i) => i + 12,
    )
      .map(
        (n) =>
          `<option value="${n}" ${p.font_size_override === n ? "selected" : ""}>${n} px${n === 16 ? " · 标准" : ""}</option>`,
      )
      .join(
        "",
      )}</select></label><div class="personal-font-preview">我会先核对影像日期和覆盖范围，再逐一复核疑似堆存点。</div><label class="personal-preference-row"><span>发送快捷键<small>${p.send_key === "ctrl_enter" ? "Enter 换行" : "Shift + Enter 换行"}</small></span><select name="personal-send-key" aria-label="发送快捷键" ${disabled}><option value="enter" ${p.send_key !== "ctrl_enter" ? "selected" : ""}>Enter 发送</option><option value="ctrl_enter" ${p.send_key === "ctrl_enter" ? "selected" : ""}>Ctrl / ⌘ + Enter 发送</option></select></label><p class="personal-note">修改自动保存到当前账号，在其他设备登录见微时也会生效。</p><button class="secondary" data-action="profile-reset" ${disabled}>恢复默认外观</button>`;
  }
  return `<header class="modal-header"><h2 id="personal-center-title">个人中心</h2><button class="icon-button" data-action="close-modal" aria-label="关闭个人中心">${icon("close")}</button></header><div class="personal-layout"><nav class="personal-nav" aria-label="个人中心分类">${TABS.map(([id, label, ic]) => `<button type="button" data-action="profile-tab" data-tab="${id}" ${tab === id ? 'aria-current="page"' : ""}>${icon(ic)}${label}</button>`).join("")}</nav><section class="personal-content" aria-label="${TABS.find((x) => x[0] === tab)?.[1] || "外观设置"}">${error ? `<p class="personal-error" role="alert">${esc(error)}</p>` : ""}<p class="personal-save-status" role="status">${loading ? "正在读取个人资料…" : saving ? "正在保存…" : esc(note || "")}</p>${content}</section></div>`;
}

export function createProfileController(
  state,
  { api, icon, showModal, render, updateComposer },
) {
  let model = null,
    generation = 0;
  const mounted = () => document.querySelector(".personal-dialog");
  function paint(focusSelector) {
    const node = mounted();
    if (!node || !model) return;
    const focused = document.activeElement;
    const restore =
      focusSelector ||
      (focused?.name
        ? `[name="${focused.name}"]`
        : focused?.dataset.tab
          ? `[data-tab="${focused.dataset.tab}"]`
          : null);
    node.innerHTML = profileContents(model, icon);
    if (restore) node.querySelector(restore)?.focus({ preventScroll: true });
  }
  async function refreshCredits(id, owner) {
    let credits;
    try {
      credits = await api("/profile/credits");
    } catch {
      credits = {
        status: "unavailable",
        message: "平台积分暂时未能读取，请刷新重试。",
      };
    }
    if (id !== generation || state.user?.id !== owner) return;
    model.credits = credits;
    if (model.tab === "quota") paint();
  }
  async function load() {
    const id = generation,
      owner = state.user?.id;
    model.loading = true;
    model.error = "";
    model.credits = { status: "loading" };
    void refreshCredits(id, owner);
    try {
      const data = await api("/profile");
      if (id !== generation || state.user?.id !== owner) return;
      model.data = data;
      state.preferences = data.preferences;
      applyAppearance(state.preferences);
    } catch (e) {
      if (id === generation) model.error = e.message;
    } finally {
      if (id === generation) {
        model.loading = false;
        paint();
      }
    }
  }
  async function savePreferences(patch, focusSelector) {
    if (!model || model.saving || model.loading) return;
    const id = generation,
      owner = state.user.id;
    model.saving = true;
    model.error = "";
    model.note = "";
    paint();
    try {
      const prefs = await api("/profile/preferences", {
        method: "PATCH",
        body: patch,
      });
      if (state.user?.id !== owner) return;
      state.preferences = prefs;
      applyAppearance(prefs);
      updateComposer();
      if (id === generation) {
        model.data.preferences = prefs;
        model.note = "设置已保存";
      }
    } catch (e) {
      if (id === generation) model.error = e.message;
    } finally {
      if (id === generation) {
        model.saving = false;
        paint(focusSelector);
      }
    }
  }
  return {
    close() {
      generation++;
      model = null;
    },
    async action(action, button) {
      if (action === "profile") {
        if (!state.user) return true;
        generation++;
        model = {
          tab: "account",
          loading: true,
          saving: false,
          error: "",
          note: "",
          credits: { status: "loading" },
          data: {
            user: state.user,
            preferences: state.preferences,
            shared_account: state.auth?.mode === "wuyu",
          },
        };
        showModal(profileContents(model, icon));
        const node = document.querySelector("#modal-root .modal");
        node.classList.add("personal-dialog");
        node.setAttribute("aria-labelledby", "personal-center-title");
        await load();
        return true;
      }
      if (!action.startsWith("profile-") || !model) return false;
      if (
        action === "profile-tab" &&
        TABS.some((x) => x[0] === button.dataset.tab)
      ) {
        model.tab = button.dataset.tab;
        model.note = "";
        paint(`[data-tab="${model.tab}"]`);
      }
      if (action === "profile-refresh") {
        paint();
        await load();
      }
      if (action === "profile-theme")
        await savePreferences(
          { theme: button.dataset.themeValue },
          `[data-theme-value="${button.dataset.themeValue}"]`,
        );
      if (action === "profile-reset")
        await savePreferences(
          { theme: "system", font_size: null, send_key: "enter" },
          '[data-action="profile-reset"]',
        );
      return true;
    },
    async change(event) {
      if (event.target.name === "personal-font-size")
        await savePreferences(
          {
            font_size:
              event.target.value === "" ? null : Number(event.target.value),
          },
          '[name="personal-font-size"]',
        );
      if (event.target.name === "personal-send-key")
        await savePreferences(
          { send_key: event.target.value },
          '[name="personal-send-key"]',
        );
    },
    async submit(event) {
      if (event.target.id !== "personal-profile-form") return false;
      if (!model || model.saving || model.loading) return true;
      const id = generation,
        owner = state.user.id,
        payload = Object.fromEntries(new FormData(event.target));
      model.saving = true;
      model.error = "";
      model.note = "";
      // Preserve the form's unsaved text if the upstream rejects the edit.
      model.data.account = { ...model.data.account, ...payload };
      paint();
      try {
        const result = await api("/profile", {
          method: "PATCH",
          body: payload,
        });
        if (state.user?.id !== owner) return true;
        state.user = result.user;
        render();
        if (id === generation) {
          model.data.user = result.user;
          model.data.account = result.account;
          model.note = "账号资料已保存";
        }
      } catch (e) {
        if (id === generation) model.error = e.message;
      } finally {
        if (id === generation) {
          model.saving = false;
          paint('#personal-profile-form button[type="submit"]');
        }
      }
      return true;
    },
  };
}
