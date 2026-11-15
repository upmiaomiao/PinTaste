/** Current Banana tools owned by this plugin instead of the generic argument renderer. */
const TOOL_TITLES = {
    create_presentation: 'row.tool.create',
    open_presentation_panel: 'row.tool.open',
    get_presentation_workflow: 'row.tool.project',
    presentation_workflow_action: 'row.tool.workflow',
    wait_presentation_workflow: 'row.tool.wait',
    read_presentation_material: 'row.tool.readMaterial',
    list_presentation_templates: 'row.tool.templates',
    get_presentation_template: 'row.tool.templates',
    upload_presentation_material: 'row.tool.uploadMaterial',
    test_presentation_models: 'row.tool.models',
    list_user_templates: 'row.tool.templates',
    upload_user_template: 'row.tool.uploadTemplate',
    get_presentation: 'row.tool.project',
    update_outline_page: 'row.tool.outline',
    add_outline_page: 'row.tool.outline',
    delete_outline_page: 'row.tool.outline',
    reorder_outline_pages: 'row.tool.outline',
    confirm_outline: 'row.tool.outline',
    start_slide_generation: 'row.tool.slides',
    get_generation_task: 'row.tool.progress',
    regenerate_slide: 'row.tool.slides',
    confirm_slides: 'row.tool.approve',
    export_presentation: 'row.tool.export',
};
/** Exact keyed slot registrations for the current Banana server tool inventory. */
export const BANANA_TOOL_KEYS = Object.keys(TOOL_TITLES).map(name => `mcp__banana__${name}`);
/**
 * Project a localized action and transport status without exposing tool payloads.
 * Completion means the tool returned; it does not assert provider connectivity or job success.
 * @param toolName - keyed MCP wire name.
 * @param block - the original running or settled transcript node.
 * @returns locale keys and a presentation state; no arguments or output text.
 */
export function bananaToolRow(toolName, block) {
    const name = toolName.replace(/^mcp__banana__/, '');
    const titleKey = Object.hasOwn(TOOL_TITLES, name)
        ? TOOL_TITLES[name] : 'row.tool.generic';
    const state = !('kind' in block) ? 'running' : block.isError ? 'failed' : 'completed';
    const statusKey = `row.status.${state}`;
    return { titleKey, statusKey, state };
}
//# sourceMappingURL=tool-row.js.map