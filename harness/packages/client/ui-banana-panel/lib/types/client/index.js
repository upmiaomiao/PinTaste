import { BananaPanelRow } from "./BananaPanelRow.js";
import { BANANA_TOOL_KEYS } from "./tool-row.js";
import { BananaDetailsPanel } from "./BananaDetailsPanel.js";
import { WuyuBrandName, WuyuBrandMark } from "./BrandName.js";
import { ComposerMaterials } from "./ComposerMaterials.js";
import { bindMaterialContext, materialIds } from "./material-upload.js";
import { createSnapshotStore } from '@deepseek-ai/dsh-client-store';
import { MaterialUploadQueue, initialUploadSnapshot } from "./material-queue.js";
import { readMaterialLimits, uploadBinaryMaterial, MaterialUploadError } from "./material-http.js";
import { connectBananaClient } from "./banana-mcp.js";
import { BANANA_MCP_URL, BANANA_MCP_TOKEN } from "./config.js";
import { createBananaPanelStore } from "./store.js";
import { en, NS, zh } from "./locales.js";
/** Required services: the slot registry, the locale registry, and the layout face. */
export const inject = ['slots', 'locale', 'layout', 'sessions', 'conversation'];
/**
 * Client plugin body: register the banana deck dictionary, the transcript rows
 * that route the current project into the right column, and the right-column
 * deck panel itself.
 * @param ctx - client root context.
 */
export function apply(ctx) {
    ctx.effect(() => ctx.locale.register(NS, { zh, en }), 'ui-banana-panel: dictionaries');
    const store = createBananaPanelStore();
    const uploads = new Map();
    const translate = ctx.locale.bind(NS);
    ctx.effect(() => () => { for (const queue of uploads.values())
        queue.dispose(); uploads.clear(); }, 'ui-banana-panel: upload queues');
    // Brand the sidebar with this PPT service's name.
    ctx.slots.inject('sidebar.brand.name', () => ctx.slots.register({ name: 'sidebar.brand.name' }, WuyuBrandName));
    ctx.slots.inject('sidebar.brand.mark', () => ctx.slots.register({ name: 'sidebar.brand.mark', priority: -5 }, WuyuBrandMark));
    ctx.slots.inject('conversation.hero.brand.mark', () => ctx.slots.register({ name: 'conversation.hero.brand.mark', priority: -5 }, WuyuBrandMark));
    ctx.slots.inject('tool.call.toolview', function* () {
        for (const key of BANANA_TOOL_KEYS) {
            yield ctx.slots.register({
                name: 'tool.call.toolview',
                key,
                locale: NS,
                store,
                inject: (_sessionId, actions) => ({
                    rememberProject: (handle, projectId) => actions.setProject(handle, projectId),
                    openPanel: (handle, projectId, target) => {
                        actions.setProject(handle, projectId);
                        if (target)
                            actions.openPreview(target);
                        ctx.layout.openDetails();
                    },
                }),
            }, BananaPanelRow);
        }
    });
    // A lower priority than the generic details panel (default 0) renders for a
    // `single` slot, so the deck panel owns the right column.
    ctx.slots.inject('details', () => ctx.slots.register({
        name: 'details',
        locale: NS,
        priority: -1,
        store,
        inject: (sessionId) => ({
            closeDetails: () => ctx.layout.closeDetails(),
            writeToConversation: async (text) => {
                const actx = ctx.sessions.scope(sessionId);
                if (!actx)
                    throw new Error('Session unavailable');
                const input = ctx.conversation.input.for(actx);
                const draft = input.state.getSnapshot().draft;
                input.setDraft(draft ? draft + '\n\n' + text : text);
            },
        }),
    }, BananaDetailsPanel));
    // Attachments and preview navigation belong to the current conversation scope.
    ctx.slots.inject('conversation.input.left', () => ctx.slots.register({
        name: 'conversation.input.left', id: 'wuyu-ppt-materials', locale: NS, store,
        inject: (sessionId, actions) => {
            let queue = uploads.get(sessionId);
            if (!queue) {
                queue = new MaterialUploadQueue(createSnapshotStore(initialUploadSnapshot()), {
                    limits: () => readMaterialLimits(BANANA_MCP_URL, BANANA_MCP_TOKEN),
                    project: async (handle) => {
                        const client = await connectBananaClient(BANANA_MCP_URL, BANANA_MCP_TOKEN);
                        try {
                            const result = await client.callTool(handle ? 'get_presentation_workflow' : 'create_presentation', handle ? { project_handle: handle } : { idea_prompt: translate('material.projectIdea') });
                            const value = result.structuredContent;
                            if (result.isError || !value || typeof value !== 'object' || Array.isArray(value))
                                throw new MaterialUploadError('PROJECT_CONTEXT_DENIED');
                            const data = value;
                            const resolved = typeof data.project_handle === 'string' ? data.project_handle : handle;
                            if (!resolved)
                                throw new MaterialUploadError('PROJECT_CONTEXT_DENIED');
                            const id = typeof data.project_id === 'string' ? data.project_id : undefined;
                            actions.setProject(resolved, id);
                            return { handle: resolved, ...(id ? { id } : {}), materialCount: materialIds(data).length };
                        }
                        finally {
                            client.dispose();
                        }
                    },
                    upload: (file, input, signal, progress) => uploadBinaryMaterial(BANANA_MCP_URL, BANANA_MCP_TOKEN, file, input, signal, progress),
                    bind: async (binding) => { await bindMaterialContext(sessionId, binding); },
                    ready: project => { actions.setProject(project.handle, project.id); actions.openPreview({ view: 'materials' }); ctx.layout.openDetails(); },
                });
                uploads.set(sessionId, queue);
            }
            const controller = queue;
            return {
                hooks: { materialUploads: controller.state },
                intakeFiles: (files, role, handle) => controller.add(files, role, handle),
                retryUpload: (id) => controller.retry(id),
                pauseUpload: (id) => controller.pause(id),
                removeUpload: (id) => controller.remove(id),
                syncUploads: () => controller.sync(),
                openDetails: () => ctx.layout.openDetails(),
            };
        },
    }, ComposerMaterials));
}
//# sourceMappingURL=index.js.map