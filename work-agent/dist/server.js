import { createHash, timingSafeEqual } from "node:crypto";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import express from "express";
import { hostHeaderValidation } from "@modelcontextprotocol/sdk/server/middleware/hostHeaderValidation.js";
import { registerAppResource, registerAppTool, RESOURCE_MIME_TYPE, } from "@modelcontextprotocol/ext-apps/server";
import { z } from "zod/v4";
import { BananaApiClient, BananaApiError } from "./api-client.js";
import { issueExportApproval, issueProjectHandle, projectIdFromHandle, verifyExportApproval, } from "./tokens.js";
import { PRESET_STYLES } from "./preset_styles.js";
import { registerWorkflowTools, safeError, presentWorkflow, conversationalWorkflow, workflowContent, researchPreparationOrderInstruction } from "./workflow-tools.js";
import { registerAssetRoutes } from "./asset-http.js";
import { registerPageLockTools } from "./page-lock-tools.js";
import { registerDesignHabitTools } from "./design-habit-tools.js";
import { registerMaterialUploadRoutes } from "./material-upload-http.js";
import { resolveGenerationIntent } from "./generation-intent.js";
const UI_URI = "ui://banana-slides/project-panel.html";
const here = dirname(fileURLToPath(import.meta.url));
/** Fallback style used when the caller does not choose a preset or custom text. */
const DEFAULT_TEMPLATE_STYLE = "Professional editorial presentation with clear hierarchy and varied layouts";
/**
 * Resolve a `template_style` argument to the full description sent to the
 * backend. Accepts a preset id (e.g. `business-simple`, optionally prefixed
 * `preset:`), a preset's Chinese name, or free-form custom text; returns the
 * default when the argument is empty.
 * @param input - the raw template_style tool argument.
 * @returns the resolved style description.
 */
function resolveTemplateStyle(input) {
    const trimmed = input?.trim();
    if (!trimmed)
        return DEFAULT_TEMPLATE_STYLE;
    const key = trimmed.startsWith("preset:") ? trimmed.slice("preset:".length) : trimmed;
    const preset = PRESET_STYLES[key];
    if (preset)
        return preset.description;
    for (const candidate of Object.values(PRESET_STYLES)) {
        if (candidate.name === trimmed)
            return candidate.description;
    }
    return trimmed;
}
/** Built-in system template images (id -> Chinese display name) available as `template_preset`. */
const PRESET_TEMPLATES = {
    "business-plan": "商业计划书",
    "circular-economy": "循环经济",
    "phd-defense-1": "博士开题答辩 1",
    "phd-defense-2": "博士开题答辩 2",
    "xuejian": "学件材料",
    "solid-waste": "固体废物治理",
};
/**
 * Resolve a `template_preset` argument to the backend's preset id. Accepts the
 * id directly (optionally prefixed `preset:`) or the preset's Chinese name;
 * returns undefined when empty so the project keeps no template image.
 * @param input - the raw template_preset tool argument.
 * @returns the preset id, or undefined.
 */
function resolveTemplatePreset(input) {
    const trimmed = input?.trim();
    if (!trimmed)
        return undefined;
    const key = trimmed.startsWith("preset:") ? trimmed.slice("preset:".length) : trimmed;
    if (PRESET_TEMPLATES[key])
        return key;
    for (const [id, name] of Object.entries(PRESET_TEMPLATES)) {
        if (name === trimmed)
            return id;
    }
    return undefined;
}
/**
 * Extract the raw base64 payload and mime type from a client-supplied template
 * image. Accepts a `data:` URL or a bare base64 string; non-image data URLs
 * and unparseable payloads fall back to PNG so the backend rejects them.
 * @param input - the raw upload tool argument.
 * @returns decoded bytes, mime type, and file extension.
 */
function decodeTemplateImage(input) {
    const trimmed = input.trim();
    const dataUrl = /^data:([a-z0-9.+-]+\/[a-z0-9.+-]+);base64,(.+)$/is.exec(trimmed);
    if (dataUrl) {
        const mime = dataUrl[1].toLowerCase();
        if (!["image/png", "image/jpeg"].includes(mime))
            throw new BananaApiError("Only PNG/JPEG template images are supported", 400);
        const ext = mime === "image/jpeg" ? "jpg" : "png";
        const encoded = dataUrl[2].replace(/\s+/g, "");
        if (!/^[A-Za-z0-9+/]*={0,2}$/.test(encoded) || encoded.length % 4 !== 0)
            throw new Error("Invalid base64 upload");
        const data = Buffer.from(encoded, "base64");
        if (!data.length || data.length > 20 * 1024 * 1024)
            throw new Error("File too large (20 MB maximum)");
        return { data, mime, ext };
    }
    const encoded = trimmed.replace(/\s+/g, "");
    if (!/^[A-Za-z0-9+/]*={0,2}$/.test(encoded) || encoded.length % 4 !== 0)
        throw new Error("Invalid base64 upload");
    const data = Buffer.from(encoded, "base64");
    if (!data.length || data.length > 20 * 1024 * 1024)
        throw new Error("File too large (20 MB maximum)");
    return { data, mime: "image/png", ext: "png" };
}
/**
 * Extract a user template id (a UUID) from a possibly-prefixed argument. The
 * model may pass the id directly or the trailing id inside a
 * `（PPT用户模板：…/<id>）` prefix.
 * @param input - the raw user_template_id tool argument.
 * @returns the UUID, or undefined when none is present.
 */
function resolveUserTemplateId(input) {
    const trimmed = input?.trim();
    if (!trimmed)
        return undefined;
    const match = /([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/.exec(trimmed);
    return match ? match[1] : undefined;
}
function requiredEnv(name) {
    const value = process.env[name]?.trim();
    if (!value)
        throw new Error(`${name} is required`);
    return value;
}
function loadConfig() {
    return {
        backendUrl: (process.env.BANANA_BACKEND_URL || "http://localhost:5011").replace(/\/$/, ""),
        publicBackendUrl: (process.env.BANANA_PUBLIC_BACKEND_URL || process.env.BANANA_BACKEND_URL || "http://localhost:5011").replace(/\/$/, ""),
        frontendUrl: (process.env.BANANA_FRONTEND_URL || "http://localhost:3011").replace(/\/$/, ""),
        allowedHosts: (process.env.BANANA_AGENT_ALLOWED_HOSTS || "localhost,127.0.0.1,[::1]")
            .split(",")
            .map((host) => host.trim())
            .filter(Boolean),
        apiKey: process.env.BANANA_AGENT_API_KEY?.trim() || undefined,
        actorTokenPath: process.env.BANANA_ACTOR_TOKEN_PATH?.trim() || undefined,
        username: process.env.BANANA_ACTOR_TOKEN_PATH ? "" : requiredEnv("BANANA_AGENT_USERNAME"),
        password: process.env.BANANA_ACTOR_TOKEN_PATH ? "" : requiredEnv("BANANA_AGENT_PASSWORD"),
        capabilitySecret: requiredEnv("BANANA_AGENT_SECRET"),
        publicAgentUrl: process.env.BANANA_PUBLIC_AGENT_URL || `${(process.env.BANANA_FRONTEND_URL || "http://localhost:3011").replace(/\/$/, "")}/ppt-agent`,
    };
}
function unwrap(response) {
    return response.data;
}
function textResult(data, summary) {
    return {
        content: [{ type: "text", text: summary }],
        structuredContent: data,
    };
}
function errorResult(error) {
    return safeError(error);
}
function absoluteUrl(path, baseUrl) {
    if (typeof path !== "string" || !path)
        return null;
    if (/^https?:\/\//.test(path))
        return path;
    return `${baseUrl}${path.startsWith("/") ? "" : "/"}${path}`;
}
export function isValidBearerAuthorization(header, apiKey) {
    if (!apiKey)
        return true;
    const expected = Buffer.from(`Bearer ${apiKey}`);
    const actual = Buffer.from(header || "");
    return actual.length === expected.length && timingSafeEqual(actual, expected);
}
function presentProject(project, handle, config) {
    project = presentWorkflow(project, project.project_id, config);
    return {
        ...project,
        project_handle: handle,
        editor_url: `${config.frontendUrl}/project/${project.project_id}/preview`,
        template_image_url: absoluteUrl(project.template_image_url, config.publicBackendUrl),
        pages: (project.pages || []).map((page) => ({
            ...page,
            image_url: absoluteUrl(page.generated_image_url, config.publicBackendUrl),
        })),
        exports: (project.exports || []).map((file) => ({
            ...file,
            download_url: absoluteUrl(file.download_url, config.publicBackendUrl),
        })),
    };
}
function outlineText(project) {
    const lines = [];
    (project.pages || []).forEach((page, index) => {
        lines.push(`[Page ${index + 1}] ${page.outline_content?.title ?? "(untitled)"} (page_id: ${page.page_id})`);
        const points = page.outline_content?.points ?? [];
        for (const point of points) {
            lines.push(`  - ${point}`);
        }
    });
    return lines.join("\n");
}
function projectRevision(project) {
    const revisionData = (project.pages || []).map((page) => ({
        id: page.page_id,
        updated_at: page.updated_at,
        image: page.generated_image_url,
        status: page.status,
    }));
    return createHash("sha256").update(JSON.stringify(revisionData)).digest("hex");
}
async function readProject(api, projectId) {
    const project = unwrap(await api.get(`/api/projects/${projectId}`));
    // Attach prior exports so the panel can offer existing download links
    // without re-confirming slides; a missing/unavailable list degrades to none.
    try {
        const exported = unwrap(await api.get(`/api/projects/${projectId}/exports`));
        project.exports = exported.files ?? [];
    }
    catch {
        project.exports = [];
    }
    return project;
}
async function requireLegacyProject(api, projectId) {
    const project = unwrap(await api.get(`/api/projects/${projectId}`));
    if (project.workflow_managed)
        throw new BananaApiError("此项目使用无隅 PPT 工作流，请在原项目面板通过 presentation_workflow_action 操作。", 400);
}
export function createBananaMcpServer(config, api = new BananaApiClient({
    actorTokenPath: config.actorTokenPath,
    baseUrl: config.backendUrl,
    username: config.username,
    password: config.password,
})) {
    const server = new McpServer({ name: "banana-slides", version: "0.1.0" }, { capabilities: { logging: {} } });
    registerWorkflowTools(server, api, config);
    registerPageLockTools(server, api, config);
    registerDesignHabitTools(server, api);
    server.registerTool("create_presentation", {
        title: "Create Wuyu presentation project",
        description: researchPreparationOrderInstruction + "Create a durable Wuyu project using the user's actual request. deck makes 2–80 slides including a cover; pages makes exactly 1–8 content pages without a mandatory cover; asset makes one standalone diagram, illustration or icon. Preserve the explicit page count and output preference; model configuration is administrator-controlled. Creation only saves the brief. Follow conversation.required_action: confirm a real template before generate_images, review all actual images before native tracing. Do not generate disposable samples or fabricate approval. For an existing uploaded-material project, update its brief instead of creating a replacement or discarding materials. Gemini reads original material text, tables and verified images through analyze_materials and its evidence-checked draft. For incomplete reading, offer supplement/replace or explicitly consented partial continuation only when the current backend continuation_options allow it; preserve all unread-range warnings and never bypass quota, security or an invalid content plan by generating directly. Show actual output and named detail links.",
        inputSchema: {
            task_mode: z.enum(['create','relayout']).default('create').describe('Match the explicit composer task. relayout creates an intake project for one original PPTX, never a new default 12-slide deck.'),
            idea_prompt: z.string().min(3).describe("Preserve the user's presentation goal, audience, content and requested research/confirmation sequence. Do not shorten away a request to offer research choices before searching or generating."),
            language: z.enum(["zh", "en", "ja", "auto"]).default("zh"),
            creation_kind: z.enum(["deck", "pages", "asset"]).default("deck"),
            page_count: z.number().int().min(1).max(80).optional().describe("Exact count: deck 2–80 including cover; pages 1–8 content pages; asset exactly 1. Defaults to 12 for deck, 1 for pages/asset."),
            output_preference: z.enum(["image", "editable"]).optional().describe("image skips native tracing; editable requests PPTX with native objects where validated, and honest image fallbacks for unavailable tracing."),
            image_profile: z.enum(["default", "gpt", "gemini"]).default("default").describe("Choose one administrator-configured image profile for this project only. Does not change the server default, credentials, or queued tasks. Check get_presentation_generation_options for availability."),
            asset_kind: z.enum(["diagram", "illustration", "icon"]).default("diagram"),
            template_style: z.string().optional().describe("Slide visual style. Pass a preset id to use a built-in style — " +
                Object.entries(PRESET_STYLES).map(([id, s]) => `${id}（${s.name}）`).join("、") +
                " — or pass free-form style text. Omit to use the default professional editorial style."),
            template_preset: z.string().optional().describe("Slide template image (reference design) to match. Pass a preset id — " +
                Object.entries(PRESET_TEMPLATES).map(([id, name]) => `${id}（${name}）`).join("、") +
                " — or omit for no template image."),
            user_template_id: z.string().optional().describe("A user-uploaded template's id (UUID) to use as the reference image. Extract it from the （PPT用户模板：<name> / <id>）prefix in the user's message, or omit when the user picked a preset or no template."),
            image_aspect_ratio: z.literal("16:9").default("16:9").describe("Wuyu PPT currently supports 16:9 slides"),
            image_mode: z.enum(["stable", "creative", "best"]).default("best").describe("Legacy compatibility field; use image_profile for actual GPT Image or Gemini selection. Never silently substitute the selected model."),
            scenario: z.enum(["research", "commercial"]).default("research"),
            review_policy: z.enum(["guided", "template_only", "template_and_final"]).default("template_and_final").describe("template_and_final (default): after the template is chosen every page is generated in the background, then the user reviews all images once. guided reviews page by page and stops after each page until the user replies; use it only when the user asks for page-by-page review."),
        },
        annotations: { destructiveHint: false, idempotentHint: false, openWorldHint: true },
    }, async ({ task_mode, idea_prompt, language, page_count, creation_kind, output_preference, image_profile, asset_kind, template_style, template_preset, user_template_id, image_aspect_ratio, scenario, review_policy }) => {
        try {
            const intent = resolveGenerationIntent({ creation_kind: task_mode === 'relayout' ? 'deck' : creation_kind, page_count: task_mode === 'relayout' ? 2 : page_count, output_preference: task_mode === 'relayout' ? 'image' : output_preference, image_profile, asset_kind });
            const resolvedStyle = resolveTemplateStyle(template_style);
            const resolvedPreset = resolveTemplatePreset(template_preset);
            const resolvedUserTemplate = resolveUserTemplateId(user_template_id);
            const created = unwrap(await api.post("/api/projects", {
                creation_type: "idea",
                idea_prompt,
                template_style: resolvedStyle,
                template_preset: resolvedPreset,
                user_template_id: resolvedUserTemplate,
                image_aspect_ratio,
                image_mode: "stable",
            }));
            let workflow = unwrap(await api.post(`/api/projects/${created.project_id}/workflow/action`, {
                action: "update_brief", payload: { brief: { task_mode, topic: idea_prompt, goal: idea_prompt, language, ...intent,
                        style: resolvedStyle, aspect_ratio: image_aspect_ratio, scenario,
                        review_policy: creation_kind === "deck" ? review_policy : "template_only" } },
            }));
            const project = await readProject(api, created.project_id);
            if (resolvedUserTemplate || resolvedPreset) {
                const prefix = `/files/${created.project_id}/template/`;
                const path = project.template_image_url;
                if (typeof path === "string" && path.startsWith(prefix) && /^[\w.-]+$/.test(path.slice(prefix.length))) {
                    const image = await api.getBinary(path);
                    if (!image.data.byteLength || image.data.byteLength > 20 * 1024 * 1024)
                        throw new BananaApiError("Template image exceeds upload size limit", 413);
                    workflow = unwrap(await api.postForm(`/api/projects/${created.project_id}/workflow/materials`, { role: "template" }, "file", { name: "selected-template.png", type: image.contentType, data: image.data }));
                }
                else {
                    workflow.template_warning = "所选旧模板暂无可用图片，请在项目面板上传模板或选择内置参考。";
                }
            }
            const handle = issueProjectHandle(created.project_id, config.capabilitySecret);
            const snapshot = { ...presentProject(project, handle, config), ...conversationalWorkflow(workflow, created.project_id, config) };
            return { content: workflowContent(snapshot, handle), structuredContent: snapshot };
        }
        catch (error) {
            return errorResult(error);
        }
    });
    registerAppTool(server, "open_presentation_panel", {
        title: "Open presentation project panel",
        description: "Open a project detail tab. Use view=templates before generation so the user can inspect five template body pages and click Confirm in that tab. That explicit template choice does not generate images. Other feedback stays in the conversation.",
        inputSchema: { project_handle: z.string().min(20), view: z.enum(["materials", "brief", "research", "templates", "outline", "references", "samples", "slides", "history", "trace", "export"]).optional(), page_number: z.number().int().min(1).max(80).optional() },
        _meta: { ui: { resourceUri: UI_URI } },
        annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    }, async ({ project_handle, view, page_number }) => {
        try {
            const projectId = projectIdFromHandle(project_handle, config.capabilitySecret);
            const project = await readProject(api, projectId);
            return textResult({ ...presentProject(project, project_handle, config), preview_target: { view: view === "references" ? "templates" : view || "templates", page_number },
                preview_url: `${config.frontendUrl.replace(/\/$/, "")}/work#wuyu-ppt?view=${view === "references" ? "templates" : view || "templates"}${page_number ? `&page=${page_number}` : ""}&project=${encodeURIComponent(projectId)}` }, "已打开所选详情预览；请在对话中继续。");
        }
        catch (error) {
            return errorResult(error);
        }
    });
    server.registerTool("list_user_templates", {
        title: "List user-uploaded templates",
        description: "List the user's uploaded template images (id + name) so one can be picked as a reference image for a new presentation.",
        inputSchema: {},
        annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
    }, async () => {
        try {
            const result = unwrap(await api.get("/api/user-templates"));
            const templates = (result.templates ?? []).map((template) => ({
                template_id: template.template_id,
                name: template.name ?? null,
                thumb_url: absoluteUrl(template.thumb_url, config.publicBackendUrl),
            }));
            return textResult({ templates }, `Found ${templates.length} user template(s).`);
        }
        catch (error) {
            return errorResult(error);
        }
    });
    server.registerTool("upload_user_template", {
        title: "Upload user template image",
        description: "Upload a template image (PNG/JPG) as a reusable reference template. Returns the new template id to pass to create_presentation as user_template_id.",
        inputSchema: {
            image_base64: z.string().min(1).max(28_000_000).describe("The template image as a base64 string or data: URL (PNG/JPG, at most 20 MB)."),
            name: z.string().optional().describe("Display name for the template."),
        },
        annotations: { destructiveHint: false, idempotentHint: false, openWorldHint: true },
    }, async ({ image_base64, name }) => {
        try {
            const image = decodeTemplateImage(image_base64);
            const form = {};
            if (name !== undefined && name.trim() !== "") {
                form.name = name.trim();
            }
            const created = unwrap(await api.postForm("/api/user-templates", form, "template_image", { name: `template.${image.ext}`, data: image.data, type: image.mime }));
            return textResult({ template_id: created.template_id, name: created.name ?? null }, `Uploaded template "${created.name ?? created.template_id}".\n\ntemplate_id: ${created.template_id}`);
        }
        catch (error) {
            return errorResult(error);
        }
    });
    server.registerTool("get_presentation", {
        title: "Get presentation status",
        description: "Get current project, outline, page images, and workflow status.",
        inputSchema: { project_handle: z.string().min(20) },
        annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    }, async ({ project_handle }) => {
        try {
            const projectId = projectIdFromHandle(project_handle, config.capabilitySecret);
            const project = await readProject(api, projectId);
            return textResult(presentProject(project, project_handle, config), `Project status: ${project.status}\n\nProject handle: ${project_handle}\n\n${outlineText(project)}`);
        }
        catch (error) {
            return errorResult(error);
        }
    });
    server.registerTool("update_outline_page", {
        title: "Update outline page",
        description: "Update one outline page after the user requests a change. This invalidates any previous slide approval.",
        inputSchema: {
            project_handle: z.string().min(20),
            page_id: z.string().uuid(),
            title: z.string().min(1),
            points: z.array(z.string()).default([]),
            part: z.string().optional(),
        },
        annotations: { destructiveHint: true, idempotentHint: true },
    }, async ({ project_handle, page_id, title, points, part }) => {
        try {
            const projectId = projectIdFromHandle(project_handle, config.capabilitySecret);
            await requireLegacyProject(api, projectId);
            await api.put(`/api/projects/${projectId}/pages/${page_id}/outline`, {
                outline_content: { title, points },
            });
            if (part !== undefined) {
                await api.put(`/api/projects/${projectId}/pages/${page_id}`, { part });
            }
            const project = await readProject(api, projectId);
            return textResult(presentProject(project, project_handle, config), `Updated outline page ${page_id}.`);
        }
        catch (error) {
            return errorResult(error);
        }
    });
    server.registerTool("add_outline_page", {
        title: "Add outline page",
        description: "Add a new page to the presentation outline at the requested position.",
        inputSchema: {
            project_handle: z.string().min(20),
            order_index: z.number().int().min(0),
            title: z.string().min(1),
            points: z.array(z.string()).default([]),
            part: z.string().optional(),
        },
        annotations: { destructiveHint: true, idempotentHint: false },
    }, async ({ project_handle, order_index, title, points, part }) => {
        try {
            const projectId = projectIdFromHandle(project_handle, config.capabilitySecret);
            await requireLegacyProject(api, projectId);
            await api.post(`/api/projects/${projectId}/pages`, {
                order_index,
                outline_content: { title, points },
                part,
            });
            const project = await readProject(api, projectId);
            return textResult(presentProject(project, project_handle, config), "Outline page added.");
        }
        catch (error) {
            return errorResult(error);
        }
    });
    server.registerTool("delete_outline_page", {
        title: "Delete outline page",
        description: "Delete a page only after the user explicitly requests it.",
        inputSchema: { project_handle: z.string().min(20), page_id: z.string().uuid() },
        annotations: { destructiveHint: true, idempotentHint: true },
    }, async ({ project_handle, page_id }) => {
        try {
            const projectId = projectIdFromHandle(project_handle, config.capabilitySecret);
            await requireLegacyProject(api, projectId);
            await api.delete(`/api/projects/${projectId}/pages/${page_id}`);
            const project = await readProject(api, projectId);
            return textResult(presentProject(project, project_handle, config), "Outline page deleted.");
        }
        catch (error) {
            return errorResult(error);
        }
    });
    server.registerTool("reorder_outline_pages", {
        title: "Reorder outline pages",
        description: "Set the complete outline page order after the user or project panel moves pages.",
        inputSchema: {
            project_handle: z.string().min(20),
            page_ids: z.array(z.string().uuid()).min(1),
        },
        annotations: { destructiveHint: true, idempotentHint: true },
    }, async ({ project_handle, page_ids }) => {
        try {
            const projectId = projectIdFromHandle(project_handle, config.capabilitySecret);
            await requireLegacyProject(api, projectId);
            const project = await readProject(api, projectId);
            const existing = new Set(project.pages.map((page) => page.page_id));
            if (page_ids.length !== existing.size || page_ids.some((id) => !existing.has(id))) {
                throw new Error("page_ids must contain every current page exactly once");
            }
            await api.put(`/api/projects/${projectId}`, { pages_order: page_ids });
            const updated = await readProject(api, projectId);
            return textResult(presentProject(updated, project_handle, config), "Outline pages reordered.");
        }
        catch (error) {
            return errorResult(error);
        }
    });
    server.registerTool("confirm_outline", {
        title: "Confirm outline and generate slide content",
        description: "Call only after the user explicitly confirms the displayed outline. Starts asynchronous per-slide content generation.",
        inputSchema: {
            project_handle: z.string().min(20),
            language: z.enum(["zh", "en", "ja", "auto"]).default("zh"),
        },
        annotations: { destructiveHint: false, idempotentHint: false, openWorldHint: true },
    }, async ({ project_handle, language }) => {
        try {
            const projectId = projectIdFromHandle(project_handle, config.capabilitySecret);
            await requireLegacyProject(api, projectId);
            const project = await readProject(api, projectId);
            if (!project.pages.length)
                throw new Error("The outline has no pages");
            const task = unwrap(await api.post(`/api/projects/${projectId}/generate/descriptions`, { language }));
            return textResult({ ...task, project_handle, next_action: "Poll get_generation_task; when complete, call start_slide_generation." }, "Outline confirmed. Slide content generation started.");
        }
        catch (error) {
            return errorResult(error);
        }
    });
    server.registerTool("start_slide_generation", {
        title: "Generate slide images",
        description: "Start slide image generation after the confirmed outline's content task has completed. No additional user confirmation is required here.",
        inputSchema: {
            project_handle: z.string().min(20),
            language: z.enum(["zh", "en", "ja", "auto"]).default("zh"),
        },
        annotations: { destructiveHint: false, idempotentHint: false, openWorldHint: true },
    }, async ({ project_handle, language }) => {
        try {
            const projectId = projectIdFromHandle(project_handle, config.capabilitySecret);
            await requireLegacyProject(api, projectId);
            const project = await readProject(api, projectId);
            if (project.pages.some((page) => !page.description_content)) {
                throw new Error("Slide content is not complete; wait for the description task before generating images");
            }
            const task = unwrap(await api.post(`/api/projects/${projectId}/generate/images`, { language, use_template: true }));
            return textResult({ ...task, project_handle, next_action: "Poll get_generation_task, then show the completed slides for user confirmation." }, "Slide image generation started.");
        }
        catch (error) {
            return errorResult(error);
        }
    });
    server.registerTool("get_generation_task", {
        title: "Get generation task status",
        description: "Check an asynchronous content, image, or editable export task.",
        inputSchema: { project_handle: z.string().min(20), task_id: z.string().uuid() },
        annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    }, async ({ project_handle, task_id }) => {
        try {
            const projectId = projectIdFromHandle(project_handle, config.capabilitySecret);
            const task = unwrap(await api.get(`/api/projects/${projectId}/tasks/${task_id}`));
            const progress = task.progress;
            if (progress?.download_url) {
                task.progress = {
                    ...progress,
                    download_url: absoluteUrl(progress.download_url, config.publicBackendUrl),
                };
            }
            const project = await readProject(api, projectId);
            return textResult({ task: presentWorkflow(task, projectId, config), project: presentProject(project, project_handle, config), project_handle }, `Task ${task_id}: ${String(task.status)}`);
        }
        catch (error) {
            return errorResult(error);
        }
    });
    server.registerTool("regenerate_slide", {
        title: "Regenerate one slide",
        description: "Regenerate one slide image after the user asks for that page to be redone. Any previous slide confirmation becomes stale.",
        inputSchema: {
            project_handle: z.string().min(20),
            page_id: z.string().uuid(),
            language: z.enum(["zh", "en", "ja", "auto"]).default("zh"),
        },
        annotations: { destructiveHint: true, idempotentHint: false, openWorldHint: true },
    }, async ({ project_handle, page_id, language }) => {
        try {
            const projectId = projectIdFromHandle(project_handle, config.capabilitySecret);
            await requireLegacyProject(api, projectId);
            const task = unwrap(await api.post(`/api/projects/${projectId}/pages/${page_id}/generate/image`, {
                force_regenerate: true,
                use_template: true,
                language,
            }));
            const taskId = task.task_id;
            if (taskId) {
                const deadline = Date.now() + 90_000;
                while (Date.now() < deadline) {
                    await new Promise((resolve) => setTimeout(resolve, 2000));
                    const t = unwrap(await api.get(`/api/projects/${projectId}/tasks/${taskId}`));
                    if (t.status === 'COMPLETED')
                        break;
                    if (t.status === 'FAILED') {
                        const message = typeof t.error_message === 'string' ? t.error_message : 'Slide regeneration failed';
                        throw new Error(message);
                    }
                }
            }
            const project = await readProject(api, projectId);
            return textResult(presentProject(project, project_handle, config), "Slide regenerated; ask the user to review it again.");
        }
        catch (error) {
            return errorResult(error);
        }
    });
    server.registerTool("confirm_slides", {
        title: "Confirm generated slides",
        description: "Call only after the user explicitly confirms the current slide images. Returns the approval token required for export.",
        inputSchema: { project_handle: z.string().min(20) },
        annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
    }, async ({ project_handle }) => {
        try {
            const projectId = projectIdFromHandle(project_handle, config.capabilitySecret);
            await requireLegacyProject(api, projectId);
            const project = await readProject(api, projectId);
            if (!project.pages.length || project.pages.some((page) => !page.generated_image_url)) {
                throw new Error("All slide images must be generated before confirmation");
            }
            const revision = projectRevision(project);
            const approval_token = issueExportApproval(projectId, revision, config.capabilitySecret);
            return textResult({ project_handle, approval_token, revision, project: presentProject(project, project_handle, config) }, "Current slides confirmed. Export is now allowed until a slide changes.");
        }
        catch (error) {
            return errorResult(error);
        }
    });
    server.registerTool("export_presentation", {
        title: "Export presentation",
        description: "Export the user-confirmed slides as PPTX, PDF, or Codia advanced editable PPTX.",
        inputSchema: {
            project_handle: z.string().min(20),
            approval_token: z.string().min(20),
            format: z.enum(["pptx", "pdf", "advanced_editable_pptx"]),
            filename: z.string().optional(),
        },
        annotations: { destructiveHint: false, idempotentHint: false, openWorldHint: true },
    }, async ({ project_handle, approval_token, format, filename }) => {
        try {
            const projectId = projectIdFromHandle(project_handle, config.capabilitySecret);
            await requireLegacyProject(api, projectId);
            const project = await readProject(api, projectId);
            verifyExportApproval(approval_token, projectId, projectRevision(project), config.capabilitySecret);
            if (format === "advanced_editable_pptx") {
                const task = unwrap(await api.post(`/api/projects/${projectId}/export/advanced-editable-pptx`, filename ? { filename } : {}));
                return textResult({ ...task, project_handle, format, next_action: "Poll get_generation_task until the download URL is available." }, "Advanced editable PPTX export started.");
            }
            const query = filename ? `?filename=${encodeURIComponent(filename)}` : "";
            const result = unwrap(await api.get(`/api/projects/${projectId}/export/${format}${query}`));
            const downloadUrl = absoluteUrl(result.download_url, config.publicBackendUrl);
            return textResult({ ...result, download_url: downloadUrl, project_handle, format }, `${format.toUpperCase()} export completed: ${downloadUrl}`);
        }
        catch (error) {
            return errorResult(error);
        }
    });
    registerAppResource(server, "Banana Slides project panel", UI_URI, {
        title: "Banana Slides",
        description: "Review and control a Banana Slides presentation workflow.",
        _meta: { ui: { csp: { resourceDomains: [new URL(config.publicAgentUrl || config.frontendUrl).origin], connectDomains: [] } } },
    }, async () => {
        const html = await readFile(resolve(here, "ui", "index.html"), "utf8");
        return {
            contents: [{
                    uri: UI_URI,
                    mimeType: RESOURCE_MIME_TYPE,
                    text: html,
                }],
        };
    });
    return server;
}
export function createAgentHttpApp(config) {
    // Manual Express app (instead of the SDK's createMcpExpressApp) so the JSON
    // body limit accommodates base64 template-image uploads; the SDK default
    // (100kb) rejects them as "Payload Too Large".
    const app = express();
    const api = new BananaApiClient({ baseUrl: config.backendUrl, username: config.username, password: config.password,
        actorTokenPath: config.actorTokenPath });
    app.use(hostHeaderValidation(config.allowedHosts));
    app.use(express.json({ limit: "30mb" }));
    registerMaterialUploadRoutes(app, config, api, header => !!config.apiKey && isValidBearerAuthorization(header, config.apiKey));
    app.get("/health", (_req, res) => res.json({ status: "ok", service: "banana-slides-agent" }));
    registerAssetRoutes(app, config, api);
    app.use("/mcp", (_req, res, next) => {
        res.setHeader("Access-Control-Allow-Origin", "*");
        res.setHeader("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
        res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization, Mcp-Session-Id, Last-Event-ID, Accept");
        res.setHeader("Access-Control-Expose-Headers", "Mcp-Session-Id");
        if (_req.method === "OPTIONS") {
            res.status(204).end();
            return;
        }
        next();
    });
    app.use("/mcp", (req, res, next) => {
        if (isValidBearerAuthorization(req.header("authorization"), config.apiKey))
            return next();
        res.setHeader("WWW-Authenticate", "Bearer");
        return res.status(401).json({ error: "Unauthorized" });
    });
    app.post("/mcp", async (req, res) => {
        const server = createBananaMcpServer(config, api);
        const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
        // Install before a long wait starts: disconnect must abort its tool signal,
        // rather than leave an orphaned waiter until the request eventually returns.
        res.once("close", () => { void transport.close(); void server.close(); });
        try {
            await server.connect(transport);
            await transport.handleRequest(req, res, req.body);
        }
        catch (error) {
            console.error("MCP request failed", error instanceof Error ? error.name : "UnknownError");
            if (!res.headersSent) {
                res.status(500).json({ jsonrpc: "2.0", error: { code: -32603, message: "Internal server error" }, id: null });
            }
        }
    });
    app.get("/mcp", (_req, res) => res.status(405).json({ error: "Use Streamable HTTP POST" }));
    app.delete("/mcp", (_req, res) => res.status(405).json({ error: "Stateless MCP server" }));
    const safeHttpError = (error, _req, res, next) => {
        if (res.headersSent)
            return next(error);
        const status = error?.type === "entity.too.large" ? 413 : error?.type === "entity.parse.failed" ? 400 : 500;
        res.status(status).json({ error: status === 413 ? "Request exceeds upload size limit" : status === 400 ? "Invalid JSON request" : "Internal server error" });
    };
    app.use(safeHttpError);
    return app;
}
/** Keep container compatibility while allowing private per-account listeners. */
export function resolveAgentBindHost(value) {
    if (value === undefined)
        return "0.0.0.0";
    if (value === "0.0.0.0" || value === "127.0.0.1")
        return value;
    throw new Error("BANANA_AGENT_HOST must be exactly 0.0.0.0 or 127.0.0.1");
}
async function main() {
    const config = loadConfig();
    if (!config.apiKey)
        throw new Error("BANANA_AGENT_API_KEY is required when starting the HTTP service");
    const app = createAgentHttpApp(config);
    const port = Number(process.env.BANANA_AGENT_PORT || 3012);
    const host = resolveAgentBindHost(process.env.BANANA_AGENT_HOST);
    app.listen(port, host, (error) => {
        if (error)
            throw error;
        console.log(`Banana Slides Agent listening on http://${host}:${port}/mcp`);
    });
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
    void main();
}
