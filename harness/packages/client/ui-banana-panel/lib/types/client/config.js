/** Build-time Banana Slides MCP endpoint configuration (public artifact content). */
/** Streamable HTTP endpoint of the Banana Slides MCP server. */
export const BANANA_MCP_URL = process.env.DSH_CLIENT_BANANA_MCP_URL ?? 'https://banana.wuyullm.com/mcp';
/** Bearer token for the Banana Slides MCP endpoint, baked at build time. */
export const BANANA_MCP_TOKEN = process.env.DSH_CLIENT_BANANA_MCP_TOKEN ?? '';
/** UI resource uri of the Banana Slides project panel. */
export const PANEL_RESOURCE_URI = 'ui://banana-slides/project-panel.html';
//# sourceMappingURL=config.js.map