import { fontSize } from "./ui-config.mjs";

export function resolveTheme(theme, prefersDark = false) {
  return theme === "dark" || (theme !== "light" && prefersDark)
    ? "dark"
    : "light";
}

export function shouldSend(event, sendKey = "enter") {
  if (
    event.key !== "Enter" ||
    event.isComposing ||
    event.keyCode === 229 ||
    event.shiftKey ||
    event.altKey
  )
    return false;
  return sendKey === "ctrl_enter"
    ? !!(event.ctrlKey || event.metaKey)
    : !event.ctrlKey && !event.metaKey;
}

let current = {};
const media = globalThis.matchMedia?.("(prefers-color-scheme: dark)");
export function applyAppearance(preferences = {}) {
  current = preferences;
  const root = globalThis.document?.documentElement;
  if (!root) return;
  root.dataset.theme = resolveTheme(preferences.theme, media?.matches);
  root.style.setProperty(
    "--portal-font-size",
    fontSize(preferences.font_size) + "px",
  );
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute(
      "content",
      root.dataset.theme === "dark" ? "#17191d" : "#ffffff",
    );
}
media?.addEventListener("change", () => applyAppearance(current));
