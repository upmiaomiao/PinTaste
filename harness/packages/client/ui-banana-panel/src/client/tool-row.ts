/** Transcript rows project recognized operation states, never provider prose. */
import type { ToolCallViewProps } from '@deepseek-ai/dsh-client-ui-tool/client'
import type { BananaPanelKey } from './locales.ts'
import { toolJsonObjects } from './tool-json.ts'

/** Current Banana tools owned by this plugin instead of the generic argument renderer. */
const TOOL_TITLES = {
  create_presentation: 'row.tool.create',
  prepare_presentation_relayout: 'row.tool.relayout',
  open_presentation_panel: 'row.tool.open',
  get_presentation_workflow: 'row.tool.project',
  presentation_workflow_action: 'row.tool.workflow',
  wait_presentation_workflow: 'row.tool.wait',
  read_presentation_material: 'row.tool.readMaterial',
  search_presentation_web: 'row.tool.webSearch',
  fetch_presentation_webpage: 'row.tool.webFetch',
  list_presentation_templates: 'row.tool.templates',
  get_presentation_template: 'row.tool.templates',
  upload_presentation_material: 'row.tool.uploadMaterial',
  test_presentation_models: 'row.tool.models',
  get_presentation_generation_options: 'row.tool.generationOptions',
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
} as const satisfies Record<string, BananaPanelKey>

/** Exact keyed slot registrations for the current Banana server tool inventory. */
export const BANANA_TOOL_KEYS = Object.keys(TOOL_TITLES).map(name => `mcp__banana__${name}`)


type RowState = 'running' | 'failed' | 'completed'
type Row = { titleKey: BananaPanelKey; state: RowState }
type Json = Record<string, unknown>
const object = (value: unknown): Json => value && typeof value === 'object' && !Array.isArray(value) ? value as Json : {}
const WORKFLOW = new Set(['prepare_presentation_relayout','get_presentation_workflow', 'presentation_workflow_action', 'wait_presentation_workflow', 'open_presentation_panel'])
const POLLS = new Set(['get_presentation_workflow', 'wait_presentation_workflow', 'get_presentation', 'get_generation_task'])
/** States worth a row even when they come from a progress read: the user has something to do or to download. */
const DECISIONS = new Set<BananaPanelKey>(['row.event.templateChoice', 'row.event.imagesReview', 'row.event.readingReview',
  'row.event.exportReady', 'row.event.web.done', 'row.event.deep.done', 'row.event.web.failed', 'row.event.deep.failed', 'row.event.incomplete'])

/**
 * The assistant reads progress many times while it waits; one row per read only repeats
 * "本次要求已记录" down the conversation. A read is shown when it reports a failure or a point
 * where the user acts; the project link stays available from the other rows and the header.
 */
export function isQuietProgressRead(toolName: string, row: Row): boolean {
  return POLLS.has(toolName.replace(/^mcp__banana__/, '')) && row.state !== 'failed' && !DECISIONS.has(row.titleKey)
}

/** op181: guard denials the assistant recovers from by itself are not failures for the user. */
const RECOVERABLE_GUARD = /^(?:Error:\s*)?(?:PPT_(?:REQUIREMENTS_CONFIRMATION_PENDING|REQUIREMENTS_PLAN_PENDING|TEXT_MATERIAL_SAVED|TEXT_MATERIAL_REQUIRED)\b|项目版本已变化|The current requirements are saved but not synchronized|Read and bind this conversation|Save these document requirements|The page count is still undecided)/
function recoverableGuard(block: ToolCallViewProps['block']): boolean {
  if (!('kind' in block) || !block.isError) return false
  return RECOVERABLE_GUARD.test(block.content.filter(item => item.type === 'text').map(item => String(item.text ?? '')).join('\n').trim())
}
function resultState(block: ToolCallViewProps['block']): Json {
  if (!('kind' in block)) return {}
  const candidates = toolJsonObjects(block.content.filter(item => item.type === 'text').map(item => item.text))
    .flatMap(value => [value, object(value.structuredContent), object(value.data)])
  return candidates.find(value => value.conversation && typeof value.conversation === 'object')
    ?? candidates.find(value => typeof value.status === 'string' || value.job || value.project_id)
    ?? candidates[0] ?? {}
}

function actionOf(block: ToolCallViewProps['block']): string {
  const raw = 'kind' in block ? block.call?.argsRaw : block.argsRaw
  try {
    const parsed = object(JSON.parse(raw ?? '{}'))
    return typeof parsed.action === 'string' ? parsed.action : ''
  } catch { return '' /* Incomplete call arguments cannot select a completed state. */ }
}

function workflowRow(value: Json, action: string): Row {
  const research = object(value.research), reading = object(value.reading)
  const mode = research.mode === 'deep' ? 'deep' : research.mode === 'web' ? 'web' : null
  const stage = object(value.conversation).stage, job = object(value.job), task = object(value.task)
  const failedJob = ['failed', 'interrupted'].includes(String(job.status)) || value.status === 'failed' || task.status === 'FAILED'
  const queued = job.status === 'queued' || value.status === 'queued' || task.status === 'PENDING'
  const running = job.status === 'running' || value.status === 'running' || task.status === 'PROCESSING' || stage === 'working'
  // A retry owns its new job state; the prior incomplete draft remains visible
  // until its replacement is ready and must not turn an active retry into failure.
  if (!failedJob && (queued || running) && !(mode && ['queued', 'running'].includes(String(research.status)))) {
    return { titleKey: queued ? 'row.event.requestQueued' : 'row.event.requestRunning', state: 'running' }
  }
  if (mode && ['queued', 'running'].includes(String(research.status))) return { titleKey: `row.event.${mode}.running`, state: 'running' }
  if ((stage === 'failed' || stage === 'quota_exhausted') && research.status !== 'failed') return { titleKey: 'row.event.incomplete', state: 'failed' }
  if (mode && ['accepted', 'review_required'].includes(String(research.status))) return { titleKey: `row.event.${mode}.done`, state: 'completed' }
  if (mode && research.status === 'failed') return { titleKey: `row.event.${mode}.failed`, state: 'failed' }
  if (job.status === 'failed' || job.status === 'interrupted' || stage === 'failed' || stage === 'quota_exhausted') {
    return { titleKey: 'row.event.incomplete', state: 'failed' }
  }
  if (research.status === 'paused' || stage === 'research_paused') return { titleKey: 'row.event.researchPaused', state: 'completed' }
  if (research.status === 'failed' && research.required === true) return { titleKey: 'row.event.incomplete', state: 'failed' }
  if (stage === 'research_choice' || research.required === true && research.status === 'choice_required') {
    return { titleKey: 'row.event.searchOff', state: 'completed' }
  }
  if (research.status === 'queued') return { titleKey: 'row.event.researchQueued', state: 'running' }
  if (research.status === 'running' || stage === 'research') return { titleKey: 'row.event.researchRunning', state: 'running' }
  if (research.status === 'review_required' || stage === 'research_review') return { titleKey: 'row.event.researchReview', state: 'completed' }
  if (action === 'request_research') return { titleKey: research.status === 'accepted' ? 'row.event.researchAccepted' : 'row.event.searchOff', state: 'completed' }
  if (action === 'confirm_images') return { titleKey: 'row.event.imagesAccepted', state: 'completed' }
  if (action === 'update_brief') return { titleKey: 'row.event.requirementsSaved', state: 'completed' }
  if (action === 'update_outline') return { titleKey: 'row.event.outlineUpdated', state: 'completed' }
  if (reading.status === 'reading' || stage === 'reading') return { titleKey: 'row.event.reading', state: 'running' }
  if (reading.status === 'draft_incomplete') return { titleKey: 'row.event.incomplete', state: 'failed' }
  if (reading.status === 'draft_ready' || stage === 'draft_review') return { titleKey: 'row.event.readingReview', state: 'completed' }
  if (action === 'continue_from_draft' && reading.status === 'accepted') return { titleKey: 'row.event.readingAccepted', state: 'completed' }
  if (stage === 'template_selection') return { titleKey: 'row.event.templateChoice', state: 'completed' }
  if (stage === 'final_image_review' || stage === 'page_review' || stage === 'template_review') return { titleKey: 'row.event.imagesReview', state: 'completed' }
  if (stage === 'exported' || stage === 'delivered_images' || stage === 'delivered_partial') return { titleKey: 'row.event.exportReady', state: 'completed' }
  if (job.status === 'queued' || value.status === 'queued' || task.status === 'PENDING') return { titleKey: 'row.event.requestQueued', state: 'running' }
  if (job.status === 'running' || value.status === 'running' || task.status === 'PROCESSING' || stage === 'working') return { titleKey: 'row.event.requestRunning', state: 'running' }
  if (stage === 'tracing' && action === 'start_tracing') return { titleKey: 'row.event.tracing', state: 'running' }
  if (research.status === 'accepted' && !stage) return { titleKey: 'row.event.researchAccepted', state: 'completed' }
  return { titleKey: 'row.event.requestRecorded', state: 'completed' }
}

/**
 * Project one concrete state from a first-party Tool operation.
 * Returned asynchronous jobs remain queued/running; a transport return is not delivery.
 * @param toolName - keyed MCP wire name.
 * @param block - running or settled transcript node.
 * @returns a static locale key and state, never arguments or response prose.
 */
export function bananaToolRow(toolName: string, block: ToolCallViewProps['block']): Row {
  const name = toolName.replace(/^mcp__banana__/, '')
  if (!('kind' in block)) return {
    titleKey: name === 'create_presentation' ? 'row.event.creating'
      : name === 'presentation_workflow_action' ? 'row.event.saving'
      : Object.hasOwn(TOOL_TITLES, name) ? TOOL_TITLES[name as keyof typeof TOOL_TITLES] : 'row.event.requestRunning',
    state: 'running',
  }
  const value = resultState(block)
  if (recoverableGuard(block)) return { titleKey: 'row.event.adjusted', state: 'completed' }
  if (block.isError || value.success === false || value.status === 'failed') return { titleKey: 'row.event.incomplete', state: 'failed' }
  if (WORKFLOW.has(name)) return workflowRow(value, actionOf(block))
  if (['start_slide_generation', 'get_generation_task', 'regenerate_slide', 'export_presentation'].includes(name)) {
    const status = value.status ?? object(value.task).status
    if (['failed', 'FAILED', 'interrupted'].includes(String(status))) return { titleKey: 'row.event.incomplete', state: 'failed' }
    if (typeof value.download_url === 'string' && value.download_url) return { titleKey: 'row.event.exportReady', state: 'completed' }
    return workflowRow(value, actionOf(block))
  }
  const completed: Partial<Record<keyof typeof TOOL_TITLES, BananaPanelKey>> = {
    create_presentation: 'row.event.projectCreated',
    search_presentation_web: 'row.event.searchFinished',
    fetch_presentation_webpage: 'row.event.sourceRead',
    read_presentation_material: 'row.event.sourceRead',
    list_presentation_templates: 'row.event.templatesReady',
    get_presentation_template: 'row.event.templatesReady',
    list_user_templates: 'row.event.templatesReady',
    upload_presentation_material: 'row.event.materialSaved',
    upload_user_template: 'row.event.materialSaved',
    test_presentation_models: 'row.event.checksFinished',
    get_presentation_generation_options: 'row.event.detailsReady',
    get_presentation: 'row.event.detailsReady',
    update_outline_page: 'row.event.outlineUpdated',
    add_outline_page: 'row.event.outlineUpdated',
    delete_outline_page: 'row.event.outlineUpdated',
    reorder_outline_pages: 'row.event.outlineUpdated',
    confirm_outline: 'row.event.outlineReady',
    confirm_slides: 'row.event.imagesAccepted',
  }
  return { titleKey: completed[name as keyof typeof TOOL_TITLES] ?? 'row.event.requestRecorded', state: 'completed' }
}
