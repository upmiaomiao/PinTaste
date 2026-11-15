/** Build-time Banana Slides MCP endpoint configuration (public artifact content). */

/** Managed work always uses its current origin's authenticated tenant proxy. */
export const BANANA_MCP_URL = '/mcp'

/** The browser authenticates with its same-origin account cookie, never a build-time service bearer. */
export const BANANA_MCP_TOKEN = ''

/** UI resource uri of the Banana Slides project panel. */
export const PANEL_RESOURCE_URI = 'ui://banana-slides/project-panel.html'

/** Primary account, registration and settings origin for both product modes. */
export const WUYU_PLATFORM_URL = process.env.DSH_CLIENT_WUYU_PLATFORM_URL ?? 'https://ai.wuyullm.com/'
