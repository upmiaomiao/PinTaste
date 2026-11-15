// Page lock, unlocked-page regeneration and cloud-editor tools.
// Registered beside the workflow tools; the workflow action contract and every
// backend ownership, revision, quota and review check stay in force.
import { randomUUID } from "node:crypto";
import { z } from "zod/v4";
import { BananaApiError } from "./api-client.js";
import { projectIdFromHandle } from "./tokens.js";
import { safeError, conversationalWorkflow, workflowContent } from "./workflow-tools.js";
import { compactWorkflowSnapshot } from "./workflow-model-view.js";

function workflowResult(data, projectId, handle, config) {
    const { editable_draft: _unrequestedDraft, ...state } = data;
    const snapshot = conversationalWorkflow(state, projectId, config);
    const uiSnapshot = { ...snapshot, project_handle: handle, stop_wait: snapshot.conversation?.stop_wait };
    return { content: workflowContent(snapshot, handle), structuredContent: compactWorkflowSnapshot(uiSnapshot),
        _meta: { "wuyu/workflow-snapshot": uiSnapshot } };
}

export function registerPageLockTools(server, api, config) {
    server.registerTool("set_presentation_page_lock", {
        description: "Lock or unlock generated pages. Locked pages keep their current image, native trace and manual edits when the other pages are regenerated. Free and reversible; does not start generation. Use source=user_chat when the user asked in the conversation (e.g. 锁定第1、3页 / 解锁第2页); the detail panel uses user_ui. Never lock or unlock on your own initiative.",
        inputSchema: { project_handle: z.string().min(20), numbers: z.array(z.number().int().min(1).max(80)).min(1).max(80), locked: z.boolean(),
            source: z.enum(["user_chat", "user_ui"]).default("user_chat"), expected_revision: z.number().int().nonnegative().optional() },
        annotations: { destructiveHint: false, idempotentHint: true },
    }, async ({ project_handle, numbers, locked, source, expected_revision }) => {
        try {
            if (new Set(numbers).size !== numbers.length)
                throw new BananaApiError("页码不能重复。", 400);
            const id = projectIdFromHandle(project_handle, config.capabilitySecret);
            const response = await api.post(`/api/projects/${id}/workflow/action`, { action: "lock_pages", payload: { numbers, locked, source },
                ...(expected_revision !== undefined ? { expected_revision } : {}), request_id: randomUUID() });
            return workflowResult(response.data, id, project_handle, config);
        }
        catch (e) {
            return safeError(e);
        }
    });
    server.registerTool("regenerate_presentation_unlocked", {
        description: "Regenerate only the pages that are NOT locked, after the user asked for it (e.g. 其余页面重新生成 / 未锁定的页重做). Costs one planning call plus one image per regenerated page, so state which pages will be redone and the expected cost first and wait for the user's go-ahead. The confirmed template and style stay unchanged; locked pages keep their image, trace and manual edits. Page count stays as first generated; pass page_count ONLY when the user explicitly names a new total. feedback is the user's own wording for the unlocked pages. locked_numbers must be the locked page numbers you just read (rejected with PAGE_LOCK_CHANGED if they changed). Changing the template or overall requirements needs every page unlocked first (PAGES_LOCKED). The regenerated deck still needs the normal final all-image review.",
        inputSchema: { project_handle: z.string().min(20), feedback: z.string().trim().min(1).max(12000).optional(),
            page_count: z.number().int().min(1).max(80).optional(), locked_numbers: z.array(z.number().int().min(1).max(80)).max(80).optional(),
            expected_revision: z.number().int().nonnegative().optional(), request_id: z.string().uuid().optional() },
        annotations: { destructiveHint: false, idempotentHint: false, openWorldHint: true },
    }, async ({ project_handle, feedback, page_count, locked_numbers, expected_revision, request_id }) => {
        try {
            const id = projectIdFromHandle(project_handle, config.capabilitySecret);
            const payload = { ...(feedback ? { feedback } : {}), ...(page_count !== undefined ? { page_count } : {}),
                ...(locked_numbers ? { locked_numbers } : {}) };
            const response = await api.post(`/api/projects/${id}/workflow/action`, { action: "regenerate_unlocked", payload,
                ...(expected_revision !== undefined ? { expected_revision } : {}), request_id: request_id || randomUUID() });
            return workflowResult(response.data, id, project_handle, config);
        }
        catch (e) {
            return safeError(e);
        }
    });
    server.registerTool("save_presentation_text_material", {
        description: "Save content text that the USER wrote or pasted in the conversation (e.g. a full report draft, company copy, page-by-page wording) as a project material, so analyze_materials reads it as evidence like an uploaded file. Use it when the user's own message contains substantive content for the slides, especially when they say to use only their text. Pass the user's text verbatim — do not summarise, translate, reorder or add anything, and never save model-written text or tool output. One call per distinct text; it creates a normal Markdown material (text only, no images, no model call, free). After saving, run analyze_materials as usual. Do not use it for short requirements (page count, style, audience) — those belong in the requirements.",
        inputSchema: { project_handle: z.string().min(20), title: z.string().trim().min(1).max(60),
            text: z.string().min(80).max(200000), source_message_id: z.string().min(1).max(200) },
        annotations: { destructiveHint: false, idempotentHint: false },
    }, async ({ project_handle, title, text, source_message_id }) => {
        try {
            const id = projectIdFromHandle(project_handle, config.capabilitySecret);
            const safe = title.replace(/[\\/:*?"<>|\x00-\x1f]/g, " ").replace(/\s+/g, " ").trim().slice(0, 60) || "对话文案";
            const body = text.replace(/\r\n?/g, "\n");
            const bytes = Buffer.from(body, "utf8");
            if (bytes.length > 2 * 1024 * 1024)
                throw new BananaApiError("文案过长（上限 2 MB），请分段保存或上传文件。", 400);
            const response = await api.postForm(`/api/projects/${id}/workflow/materials`, { role: "material" }, "file",
                { name: `${safe}.md`, type: "text/markdown", data: bytes });
            const result = workflowResult(response.data, id, project_handle, config);
            result.content = [{ type: "text", text: `已把用户在对话中提供的文案保存为材料「${safe}.md」（${body.length} 字，来源消息 ${source_message_id}）。接下来按正常流程阅读材料。` }, ...result.content];
            return result;
        }
        catch (e) {
            return safeError(e);
        }
    });
    server.registerTool("open_presentation_editor", {
        description: "Return the cloud editor (ONLYOFFICE) link for this project's current editable PPTX. Only an editable (traced) deck can be edited; for an image-only deck explain that editable conversion is needed first. Saving in the editor writes a new project version and locks the edited pages automatically. Give the user the link; do not claim the edit was made.",
        inputSchema: { project_handle: z.string().min(20) },
        annotations: { readOnlyHint: true, idempotentHint: true },
    }, async ({ project_handle }) => {
        try {
            const id = projectIdFromHandle(project_handle, config.capabilitySecret);
            const status = (await api.get(`/api/projects/${id}/workflow/office/status`)).data;
            const configured = process.env.WUYU_OFFICE_EDITOR_BASE?.trim();
            const base = (configured || `${(config.frontendUrl || "").replace(/\/$/, "")}/agent-api/office/editor`).replace(/\/$/, "");
            const data = { ...status, editor_url: status.available ? `${base}/${encodeURIComponent(id)}` : null };
            const text = status.available ? "在线编辑链接已生成；保存后自动成为项目新版本，编辑过的页面会自动锁定。"
                : String(status.reason || "当前项目还没有可编辑版 PPTX，不能在线编辑。");
            return { content: [{ type: "text", text }, { type: "text", text: JSON.stringify(data) }], structuredContent: data };
        }
        catch (e) {
            return safeError(e);
        }
    });
}
