/**
 * Per-Session Banana Slides panel state shared by the transcript row and the
 * right details column. The tool.call.toolview row writes the current project
 * handle (via its inject-bound actions) and the details-column panel reads it,
 * so both stay in one Session scope.
 */
import { defineStore } from '@deepseek-ai/dsh-client-store';
/**
 * Create the Banana panel store handle.
 * @returns a handle instantiated once per rendered Session scope.
 */
export function createBananaPanelStore() {
    return defineStore({
        init: () => ({ projectHandle: null, projectId: null, projects: {}, previewTarget: { view: 'outline' }, previewRequest: 0, imageMode: 'best', styleMode: 'default', templateMode: 'default', userTemplate: null }),
        actions: {
            setProject: (draft, handle, projectId) => { draft.projectHandle = handle; draft.projectId = projectId || null; if (projectId)
                draft.projects[projectId] = handle; },
            openPreview: (draft, target) => { if (target.project_id) {
                const h = draft.projects[target.project_id];
                if (!h)
                    return;
                draft.projectHandle = h;
                draft.projectId = target.project_id;
            } draft.previewTarget = target; draft.previewRequest++; },
            setImageMode: (draft, mode) => { draft.imageMode = mode; },
            setStyleMode: (draft, mode) => { draft.styleMode = mode; },
            setTemplateMode: (draft, mode) => { draft.templateMode = mode; },
            setUserTemplate: (draft, selection) => { draft.userTemplate = selection; },
        },
    });
}
//# sourceMappingURL=store.js.map