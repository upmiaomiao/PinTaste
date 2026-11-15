/** Composer choices are copied into ordinary logged user text at submission. */
import { zh, en, type BananaPanelKey } from './locales.ts'

/** Locale-owned wording for the user-visible submission summary. */
export type GenerationTextTranslator = (key: BananaPanelKey) => string
export interface GenerationSettings {
  creation_kind: 'deck' | 'pages' | 'asset'
  output_preference: 'image' | 'editable'
  image_profile: 'default' | 'gpt' | 'gemini'
  page_count: number | null
  asset_kind: 'diagram' | 'illustration' | 'icon'
  review_policy: 'guided' | 'template_only' | 'template_and_final'
}
/** Safe image-profile information returned by the public options tool. */
export interface GenerationProfile {
  id: 'gpt' | 'gemini'
  label: string
  model: string
  available: boolean
}
/** The options response contains no endpoint or credential configuration. */
export interface GenerationOptions {
  profiles: GenerationProfile[]
  default_profile: 'gpt' | 'gemini'
}
/** @returns Existing full-deck defaults; an absent count leaves planning automatic. */
export function defaultGenerationSettings(): GenerationSettings {
  return { creation_kind: 'deck', output_preference: 'editable', image_profile: 'default', page_count: null, asset_kind: 'diagram', review_policy: 'template_only' }
}
/** @param current - Prior choices. @param patch - Explicit user changes. @returns Mode-compatible choices. */
export function updateGenerationSettings(current: GenerationSettings, patch: Partial<GenerationSettings>): GenerationSettings {
  const next = { ...current, ...patch }
  if (patch.creation_kind && patch.creation_kind !== current.creation_kind) {
    if (patch.output_preference === undefined) next.output_preference = patch.creation_kind === 'deck' ? 'editable' : 'image'
    if (patch.page_count === undefined) next.page_count = patch.creation_kind === 'deck' ? null : 1
  }
  if (next.creation_kind === 'asset') next.page_count = 1
  else if (next.page_count !== null) next.page_count = Math.min(next.creation_kind === 'pages' ? 8 : 80, Math.max(next.creation_kind === 'pages' ? 1 : 2, Math.round(next.page_count)))
  if (next.creation_kind !== 'deck') next.review_policy = 'template_only'
  return next
}
/** Whether a count token is one canonical integer with the locale-owned unit. */
function summaryCountIsValid(value: string | undefined, unit: string, min: number, max: number): boolean {
  if (value === undefined || !value.endsWith(unit)) return false
  const digits = value.slice(0, -unit.length)
  return /^[1-9]\d*$/.test(digits) && Number(digits) >= min && Number(digits) <= max
}
/** Recognize only the complete finite-choice summaries emitted by this composer. */
function isGenerationSummaryLine(line: string): boolean {
  return [zh, en].some(dictionary => {
    const header = dictionary['generation.requestHeader']
    if (!line.startsWith(header)) return false
    const cells = line.slice(header.length).split(' · ')
    if (![dictionary['generation.modelDefault'], dictionary['generation.gpt'], dictionary['generation.gemini']].includes(cells[2] ?? '')) return false
    if (![dictionary['generation.image'], dictionary['generation.editable']].includes(cells[3] ?? '')) return false
    if (cells[0] === dictionary['generation.deck']) {
      return cells.length === 6 && cells[5] === '16:9'
        && (cells[1] === dictionary['generation.autoCount'] || summaryCountIsValid(cells[1], dictionary['generation.pageUnit'], 2, 80))
        && [dictionary['generation.reviewTemplate'], dictionary['generation.reviewFinal'], dictionary['generation.reviewGuided']].includes(cells[4] ?? '')
    }
    if (cells.length !== 5 || cells[4] !== '16:9') return false
    if (cells[0] === dictionary['generation.pagesSummary']) return summaryCountIsValid(cells[1], dictionary['generation.pageUnit'], 1, 8)
    return cells[0] === dictionary['generation.asset'] && [dictionary['generation.diagram'], dictionary['generation.illustration'], dictionary['generation.icon']].includes(cells[1] ?? '')
  })
}
/** Keep both historical prefix and current suffix messages byte-for-byte on resend. */
function hasGenerationSummary(text: string): boolean {
  const firstBreak = text.indexOf('\n\n')
  if (firstBreak >= 0 && isGenerationSummaryLine(text.slice(0, firstBreak))) return true
  const tail = text.trimEnd()
  const lastBreak = tail.lastIndexOf('\n\n')
  return isGenerationSummaryLine(lastBreak < 0 ? tail : tail.slice(lastBreak + 2))
}
/** @param text - Submitted text, kept first for title derivation and explicit instructions. @param settings - Synchronous choice snapshot. @param t - Current locale dictionary. @returns Human-readable logged choices after the original body, preserving valid historical summaries on resend. */
export function generationRequestText(text: string, settings: GenerationSettings, t: GenerationTextTranslator = key => zh[key]): string {
  if (hasGenerationSummary(text)) return text
  const parts = [t(settings.creation_kind === 'deck' ? 'generation.deck' : settings.creation_kind === 'pages' ? 'generation.pagesSummary' : 'generation.asset')]
  if (settings.creation_kind === 'asset') parts.push(t(settings.asset_kind === 'diagram' ? 'generation.diagram' : settings.asset_kind === 'illustration' ? 'generation.illustration' : 'generation.icon'))
  else parts.push(settings.page_count === null ? t('generation.autoCount') : settings.page_count + t('generation.pageUnit'))
  parts.push(t(settings.image_profile === 'default' ? 'generation.modelDefault' : settings.image_profile === 'gpt' ? 'generation.gpt' : 'generation.gemini'))
  parts.push(t(settings.output_preference === 'editable' ? 'generation.editable' : 'generation.image'))
  if (settings.creation_kind === 'deck') parts.push(t(settings.review_policy === 'guided' ? 'generation.reviewGuided' : settings.review_policy === 'template_and_final' ? 'generation.reviewFinal' : 'generation.reviewTemplate'))
  parts.push('16:9')
  return text + '\n\n' + t('generation.requestHeader') + parts.join(' · ')
}
/** @param value - Untrusted MCP structured content. @returns Only supported public fields; rejects incomplete profiles. */
export function parseGenerationOptions(value: unknown): GenerationOptions {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('GENERATION_OPTIONS_INVALID')
  const item = value as Record<string, unknown>
  if (!['gpt', 'gemini'].includes(String(item.default_profile)) || !Array.isArray(item.profiles) || item.profiles.length !== 2) throw new Error('GENERATION_OPTIONS_INVALID')
  const profiles: GenerationProfile[] = item.profiles.map((raw: unknown) => {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('GENERATION_OPTIONS_INVALID')
    const p = raw as Record<string, unknown>
    if (!['gpt', 'gemini'].includes(String(p.id)) || typeof p.label !== 'string' || typeof p.model !== 'string' || typeof p.available !== 'boolean' || !p.label || p.label.length > 80 || p.model.length > 120) throw new Error('GENERATION_OPTIONS_INVALID')
    return { id: p.id as GenerationProfile['id'], label: p.label, model: p.model, available: p.available }
  })
  if (new Set(profiles.map(profile => profile.id)).size !== profiles.length) throw new Error('GENERATION_OPTIONS_INVALID')
  return { default_profile: item.default_profile as GenerationOptions['default_profile'], profiles }
}
/** @param summary - The current Session summary. @returns Work posture, including the blank Session before its deployment-default preset is projected. */
export function isGenerationWorkSession(summary: { blank?: boolean; projectionValues?: { agentPreset?: unknown } } | undefined): boolean {
  const preset = summary?.projectionValues?.agentPreset
  return preset === 'wuyu-work' || summary?.blank === true && (preset === undefined || preset === null)
}
/** @param raw - Untrusted per-Session browser storage. @returns Only finite supported settings, or undefined for incomplete or stale data. */
export function restoreGenerationSettings(raw: string | null): GenerationSettings | undefined {
  if (!raw || raw.length > 2048) return undefined
  let value: unknown
  try { value = JSON.parse(raw) } catch { return undefined }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const item = value as Record<string, unknown>
  if (!['deck', 'pages', 'asset'].includes(String(item.creation_kind)) || !['image', 'editable'].includes(String(item.output_preference)) || !['default', 'gpt', 'gemini'].includes(String(item.image_profile)) || !['diagram', 'illustration', 'icon'].includes(String(item.asset_kind)) || !['guided', 'template_only', 'template_and_final'].includes(String(item.review_policy))) return undefined
  const count = item.page_count
  if (item.creation_kind === 'deck') {
    if (count !== null && (!Number.isInteger(count) || Number(count) < 2 || Number(count) > 80)) return undefined
  } else if (item.creation_kind === 'pages') {
    if (!Number.isInteger(count) || Number(count) < 1 || Number(count) > 8 || item.review_policy !== 'template_only') return undefined
  } else if (count !== 1 || item.review_policy !== 'template_only') return undefined
  return { creation_kind: item.creation_kind as GenerationSettings['creation_kind'], output_preference: item.output_preference as GenerationSettings['output_preference'], image_profile: item.image_profile as GenerationSettings['image_profile'], page_count: count as number | null, asset_kind: item.asset_kind as GenerationSettings['asset_kind'], review_policy: item.review_policy as GenerationSettings['review_policy'] }
}
