/** Check Wuyu against the exact MCP SDK installed for the upstream bridge, without a model request. */
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const bridgeRequire = createRequire(join(projectRoot, 'deepseek-harness', 'packages', 'mcp', 'mcp-client', 'package.json'))
const { Client } = await import(pathToFileURL(bridgeRequire.resolve('@modelcontextprotocol/client')).href)
const { StdioClientTransport } = await import(pathToFileURL(bridgeRequire.resolve('@modelcontextprotocol/client/stdio')).href)
const python = process.env.WUYU_PYTHON || join(projectRoot, '.venv', 'Scripts', 'python.exe')
const client = new Client({ name: 'wuyu-connectivity-check', version: '0.1.0' })
const transport = new StdioClientTransport({
  command: python,
  args: ['-m', 'wuyu_harness.mcp'],
  cwd: projectRoot,
  env: {
    ...Object.fromEntries(Object.entries(process.env).filter(([name]) => !/KEY|PASSWORD|SECRET|TOKEN/i.test(name) && !name.startsWith('DSH_'))),
    ...Object.fromEntries(Object.entries(process.env).filter(([name]) => /^(WUYU_|DEEPSEEK_|METASO_)/.test(name))),
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    WUYU_OUTPUT_DIR: join(projectRoot, 'outputs', 'wuyu-mcp-smoke'),
  },
})
try {
  await client.connect(transport)
  const { tools } = await client.listTools()
  const status = await client.callTool({ name: 'wuyu_status', arguments: {} })
  if (status.isError) throw new Error('wuyu_status returned an MCP error')
  console.log(JSON.stringify({ connected: true, tools: tools.map(tool => tool.name), status }, null, 2))
} finally {
  await client.close()
}
