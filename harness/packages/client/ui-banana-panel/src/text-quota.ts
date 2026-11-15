/** Server-only text reservations around the supported LLM stream extension. */
import type { Context } from '@deepseek-ai/cordis'
import { randomUUID } from 'node:crypto'
import { readFileSync, statSync } from 'node:fs'
import { isAbsolute } from 'node:path'
import { LlmError, type GenerateOptions, type StreamChunk, type TokenUsage } from '@deepseek-ai/dsh-llm'

export interface TextQuotaConfig {
  backendUrl: string
  actorTokenPath: string
  internalKeyPath: string
  commandTimeoutMs?: number
  commandAttempts?: number
}
type CommandBody = { action: 'reserve'; operation_key: string; input_tokens: number; output_tokens: number }
  | { action: 'settle'; reservation_id: string; input_tokens: number; output_tokens: number }
  | { action: 'refund'; reservation_id: string }
type Command = (body: CommandBody) => Promise<Record<string, unknown>>
const fail = (code = 'QUOTA_SERVICE_UNAVAILABLE') => new LlmError(code === 'QUOTA_EXCEEDED' ? '账户额度不足，请查看账户用量或联系管理员增额。' : '账户额度暂时无法确认，本次请求已停止，请稍后重试。', code)
const natural = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
function secret(path: string): string {
  try {
    if (!isAbsolute(path) || !statSync(path).isFile() || statSync(path).size > 16384) throw fail()
    const value = readFileSync(path, 'utf8').trim()
    if (!value || /[\r\n\s]/.test(value)) throw fail()
    return value
  } catch { throw fail('QUOTA_CONFIGURATION_INVALID') }
}
function usageCounts(usage: TokenUsage | undefined): { input_tokens: number; output_tokens: number } | undefined {
  if (!usage || !natural(usage.inputTokens) || !natural(usage.outputTokens)
    || (usage.cacheReadTokens !== undefined && !natural(usage.cacheReadTokens))
    || (usage.cacheWriteTokens !== undefined && !natural(usage.cacheWriteTokens))) return undefined
  const input = usage.inputTokens + (usage.cacheReadTokens ?? 0) + (usage.cacheWriteTokens ?? 0)
  return natural(input) ? { input_tokens: input, output_tokens: usage.outputTokens } : undefined
}

/** Reserve before dispatch and settle only complete, nonempty provider responses. */
export async function* meterTextStream(options: GenerateOptions, next: () => AsyncIterable<StreamChunk>, command: Command): AsyncIterable<StreamChunk> {
  options.signal?.throwIfAborted()
  if (!Number.isSafeInteger(options.maxTokens) || Number(options.maxTokens) <= 0 || Number(options.maxTokens) > 2000000) throw fail('QUOTA_OUTPUT_BOUND_REQUIRED')
  const serialized = JSON.stringify({ messages: options.messages, system: options.system, tools: options.tools })
  const input = Buffer.byteLength(serialized, 'utf8') + (serialized.match(/"type":"image"/g)?.length ?? 0) * 65536
  if (!natural(input) || input > 2000000) throw fail('QUOTA_INPUT_BOUND_INVALID')
  const reserved = await command({ action: 'reserve', operation_key: randomUUID(), input_tokens: input, output_tokens: options.maxTokens! })
  if (!record(reserved.reservation) || typeof reserved.reservation.id !== 'string' || !reserved.reservation.id || !natural(reserved.reservation.held_points)) throw fail()
  const id = reserved.reservation.id
  let keepHold = false, settled = false, content = false, usage: TokenUsage | undefined
  try {
    options.signal?.throwIfAborted()
    for await (const chunk of next()) {
      if (chunk.type === 'text-delta' && chunk.text.trim()) content = true
      if (chunk.type === 'tool-call-delta' && (chunk.name || chunk.argumentsDelta)) content = true
      if (chunk.type === 'block-end' && ((chunk.block.type === 'text' && chunk.block.text.trim()) || chunk.block.type === 'tool-call')) content = true
      if (chunk.type === 'usage') usage = chunk.usage
      if (chunk.type === 'finish' && (chunk.reason.kind === 'stop' || chunk.reason.kind === 'tool-calls') && content && !options.signal?.aborted) {
        // A successful provider request must never become free when settlement
        // is uncertain. Its durable reservation remains available for recovery.
        keepHold = true
        const counts = usageCounts(usage)
        if (!counts) throw fail('QUOTA_USAGE_UNAVAILABLE')
        await command({ action: 'settle', reservation_id: id, ...counts })
        settled = true
      }
      yield chunk
    }
  } finally {
    if (!settled && !keepHold) await command({ action: 'refund', reservation_id: id })
  }
}

/** Install per-process accounting only with a complete validated configuration. */
export function installTextQuota(ctx: Context, config?: TextQuotaConfig): void {
  if (config === undefined) return
  if (!record(config) || typeof config.backendUrl !== 'string' || typeof config.actorTokenPath !== 'string' || typeof config.internalKeyPath !== 'string') throw fail('QUOTA_CONFIGURATION_INVALID')
  let url: URL
  try { url = new URL(config.backendUrl) } catch { throw fail('QUOTA_CONFIGURATION_INVALID') }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw fail('QUOTA_CONFIGURATION_INVALID')
  const attempts = config.commandAttempts ?? 3, timeout = config.commandTimeoutMs ?? 10000
  if (!Number.isSafeInteger(attempts) || attempts < 1 || attempts > 5 || !Number.isSafeInteger(timeout) || timeout < 100 || timeout > 30000) throw fail('QUOTA_CONFIGURATION_INVALID')
  secret(config.actorTokenPath); secret(config.internalKeyPath)
  const endpoint = url.href.replace(/\/$/, '') + '/api/internal/quota/command'
  const command: Command = async body => {
    for (let attempt = 0; attempt < attempts; attempt++) {
      try {
        const response = await fetch(endpoint, { method: 'POST', redirect: 'error', signal: AbortSignal.timeout(timeout),
          headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + secret(config.actorTokenPath), 'X-Wuyu-Internal-Key': secret(config.internalKeyPath) }, body: JSON.stringify(body) })
        const reader = response.body?.getReader(), chunks: Uint8Array[] = []
        let bytes = 0
        if (reader) try {
          while (true) {
            const part = await reader.read()
            if (part.done) break
            bytes += part.value.byteLength
            if (bytes > 65536) { await reader.cancel(); throw fail() }
            chunks.push(part.value)
          }
        } finally { reader.releaseLock() }
        const raw = Buffer.concat(chunks).toString('utf8')
        let value: unknown
        try { value = JSON.parse(raw) } catch { throw fail() }
        if (response.ok && record(value) && value.success === true && record(value.data)) return value.data
        if (response.status === 429) throw fail('QUOTA_EXCEEDED')
        if (response.status >= 400 && response.status < 500) throw fail('QUOTA_REQUEST_REJECTED')
      } catch (error) {
        if (error instanceof LlmError && ['QUOTA_EXCEEDED', 'QUOTA_REQUEST_REJECTED', 'QUOTA_CONFIGURATION_INVALID'].includes(error.code)) throw error
      }
    }
    throw fail()
  }
  ctx.on('llm/stream', (options, next) => meterTextStream(options, next, command), { global: true })
}
