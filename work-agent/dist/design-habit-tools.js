// Design habits learned from the user's own revision requests (op196).
// The workflow snapshot carries design_habits.learned (newly active habits the user has not been
// told about yet), design_habits.applied and design_habits.deck_type. This tool records that the
// conversation told the user, or undoes a learned habit at the user's request. Owner-scoped by the
// backend; it never creates or edits habits and never starts model work.
import { z } from "zod/v4";
import { safeError } from "./workflow-tools.js";

export function registerDesignHabitTools(server, api) {
    server.registerTool("respond_design_habit_notice", {
        description: "Use only for habits listed in the current workflow snapshot's design_habits.learned. After you have told the user in one sentence which design habit was learned from their earlier revision requests (e.g. 我记住了你的习惯：……，以后生成会默认这样做；不需要可以说撤销), call with action=announced and those habit ids so it is not repeated. When the user asks to undo / not keep a learned habit (撤销、不要记这个、以后不用这样), call with action=undo and that habit id; it stops being applied and will not be learned again. Free, no model call. Never call on your own initiative for other habits, and never create or edit habits here.",
        inputSchema: { habit_ids: z.array(z.string().min(1).max(36)).min(1).max(12), action: z.enum(["announced", "undo"]) },
        annotations: { destructiveHint: false, idempotentHint: true },
    }, async ({ habit_ids, action }) => {
        try {
            const data = (await api.post("/api/design-memory/habits/notices", { habit_ids, action })).data;
            const changed = Array.isArray(data?.changed) ? data.changed : [];
            const text = action === "undo"
                ? (changed.length ? `已撤销：${changed.map(item => item.rule).join("；")}。以后不会再按这条生成，也不会再次学习。` : "没有可撤销的已学习习惯（可能已撤销或不属于当前用户）。")
                : (changed.length ? "已标记为已告知用户。" : "没有需要标记的习惯。");
            return { content: [{ type: "text", text }, { type: "text", text: JSON.stringify(data) }], structuredContent: data };
        }
        catch (e) {
            return safeError(e);
        }
    });
}
