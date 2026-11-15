/** 管理端按需加载：只有管理员点开"系统管理"时才下载 admin.mjs 和 admin.css。
 *  对 app.mjs 暴露与原来同名、同参数的函数，调用方几乎不用改。 */
let adminModule = null;
let loading = null;
const VERSION = new URL(import.meta.url).searchParams.get("v") || "";

function loadStylesheet() {
  if (document.querySelector("link[data-admin-css]")) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.dataset.adminCss = "1";
  link.href = "admin.css" + (VERSION ? "?v=" + VERSION : "");
  document.head.appendChild(link);
}

export function ensureAdmin() {
  if (adminModule) return Promise.resolve(adminModule);
  loading ??= import("./admin.mjs" + (VERSION ? "?v=" + VERSION : ""))
    .then((module) => (adminModule = module))
    .catch((error) => { loading = null; throw error; });
  loadStylesheet();
  return loading;
}

export const adminLoaded = () => !!adminModule;

/** 同步渲染：还没加载完时先给一个简短占位，加载完成后调用 onReady 重新渲染。 */
export function renderAdminLazy(state, icon, onReady) {
  if (adminModule) return adminModule.renderAdmin(state, icon);
  ensureAdmin().then(() => onReady?.(), () => onReady?.());
  return `<div class="initial-loader" role="status"><span class="loading-dot"></span> 正在打开系统管理…</div>`;
}

export function handleAdminModelInputLazy(event) {
  return adminModule ? adminModule.handleAdminModelInput(event) : undefined;
}

export async function handleAdminActionLazy(...args) {
  return (await ensureAdmin()).handleAdminAction(...args);
}

export async function handleAdminSubmitLazy(...args) {
  // 只有已经进入管理端才需要处理其表单；没加载过就不要为了一次普通提交去下载它。
  return adminModule ? adminModule.handleAdminSubmit(...args) : undefined;
}
