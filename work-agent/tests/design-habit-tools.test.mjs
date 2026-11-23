// op196: respond_design_habit_notice — schema, backend call and user-facing text, offline.
import test from "node:test";
import assert from "node:assert/strict";
import { registerDesignHabitTools } from "../dist/design-habit-tools.js";

function setup(reply) {
    const tools = {}, calls = [];
    const server = { registerTool: (name, spec, handler) => { tools[name] = { spec, handler }; } };
    const api = { post: async (path, body) => { calls.push([path, body]); if (reply instanceof Error) throw reply; return { data: reply }; } };
    registerDesignHabitTools(server, api);
    return { tool: tools.respond_design_habit_notice, calls };
}

test("registers one tool with a bounded schema", () => {
    const { tool } = setup({});
    assert.ok(tool);
    const schema = tool.spec.inputSchema;
    assert.deepEqual(Object.keys(schema).sort(), ["action", "habit_ids"]);
    assert.equal(schema.action.safeParse("undo").success, true);
    assert.equal(schema.action.safeParse("delete").success, false);
    assert.equal(schema.habit_ids.safeParse([]).success, false);
    assert.equal(schema.habit_ids.safeParse(Array(13).fill("x")).success, false);
    assert.match(tool.spec.description, /design_habits\.learned/);
});

test("undo calls the owner endpoint and tells the user what stopped", async () => {
    const { tool, calls } = setup({ action: "undo", changed: [{ id: "h1", rule: "不使用人物头像照片", status: "revoked" }] });
    const result = await tool.handler({ habit_ids: ["h1"], action: "undo" });
    assert.deepEqual(calls, [["/api/design-memory/habits/notices", { habit_ids: ["h1"], action: "undo" }]]);
    assert.match(result.content[0].text, /已撤销：不使用人物头像照片/);
    assert.equal(result.structuredContent.changed[0].status, "revoked");
});

test("announced and empty results", async () => {
    assert.match((await setup({ changed: [{ id: "h1" }] }).tool.handler({ habit_ids: ["h1"], action: "announced" })).content[0].text, /已标记/);
    assert.match((await setup({ changed: [] }).tool.handler({ habit_ids: ["h1"], action: "undo" })).content[0].text, /没有可撤销/);
});

test("backend errors become the shared safe error", async () => {
    const result = await setup(new Error("boom /root/secret")).tool.handler({ habit_ids: ["h1"], action: "undo" });
    assert.equal(result.isError, true);
    assert.doesNotMatch(JSON.stringify(result), /root\/secret/);
});
