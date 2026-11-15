import test from "node:test";
import assert from "node:assert/strict";
import { resolveTheme, shouldSend } from "./appearance.mjs";
import { profileButton, profileContents, quotaCard } from "./profile.mjs";
const icon = () => "<svg></svg>";
const model = {
  tab: "account",
  data: {
    user: { display_name: "测试用户", role: "user" },
    account: { name: "<script>unsafe</script>", email: "a@example.test" },
    preferences: {
      theme: "system",
      font_size: 16,
      font_size_override: null,
      usage: { qa: 2, imagery: 3 },
      qa_quota: 10,
      imagery_quota: 5,
      period: "2026-10",
    },
  },
};

test("themes resolve explicit choices and follow OS only in automatic mode", () => {
  assert.equal(resolveTheme("light", true), "light");
  assert.equal(resolveTheme("dark", false), "dark");
  assert.equal(resolveTheme("system", true), "dark");
  assert.equal(resolveTheme("system", false), "light");
});
test("send keys respect newlines, OS shortcuts and Chinese IME composition", () => {
  assert.equal(shouldSend({ key: "Enter" }), true);
  assert.equal(shouldSend({ key: "Enter" }, "ctrl_enter"), false);
  for (const event of [
    { key: "Enter", isComposing: true, ctrlKey: true },
    { key: "Enter", keyCode: 229 },
    { key: "Enter", shiftKey: true },
    { key: "Enter", altKey: true },
    { key: "a" },
  ])
    assert.equal(shouldSend(event), false);
  assert.equal(shouldSend({ key: "Enter", ctrlKey: true }, "ctrl_enter"), true);
  assert.equal(shouldSend({ key: "Enter", metaKey: true }, "ctrl_enter"), true);
});
test("personal center is a named button and safely renders untrusted names", () => {
  assert.match(
    profileButton({ display_name: "<name>" }, icon),
    /aria-label="个人中心"/,
  );
  const html = profileContents(model, icon);
  assert.ok(!html.includes("<script>"));
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /personal-profile-form/);
  assert.ok(!html.includes('data-action="admin"'));
});
test("actual zero, missing, unlimited and overused quotas are distinct", () => {
  assert.match(quotaCard("问答", 2, 5), /<strong>3<small>/);
  assert.match(quotaCard("问答", 9, 5), /<strong>0<small>/);
  assert.match(quotaCard("问答", 2, null), /不限额/);
  assert.match(quotaCard("问答", undefined, 5), /<strong>—/);
});
test("points errors do not display fake zero and do not claim product credits", () => {
  const html = profileContents(
    {
      ...model,
      tab: "quota",
      credits: { status: "unavailable", message: "读取失败" },
    },
    icon,
  );
  assert.match(html, /可用积分<\/span><strong>—/);
  assert.match(html, /读取失败/);
  assert.match(html, /独立额度/);
  assert.match(
    profileContents(
      {
        ...model,
        tab: "quota",
        credits: { status: "available", available: 0, reserved: 0 },
      },
      icon,
    ),
    /可用积分<\/span><strong>0/,
  );
});
test("appearance settings expose themes, inherited font and send shortcut", () => {
  const html = profileContents({ ...model, tab: "appearance" }, icon);
  for (const text of [
    "浅色",
    "深色",
    "跟随系统",
    "personal-font-size",
    "personal-send-key",
    "profile-reset",
  ])
    assert.ok(html.includes(text));
  assert.match(html, /value="" selected/);
});
