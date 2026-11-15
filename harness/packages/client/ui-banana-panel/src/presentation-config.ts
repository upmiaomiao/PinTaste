/** Validated preferences for future PPT requests; these values never grant generation approval. */
export interface PresentationConfig {
  search_mode: 'off' | 'web' | 'deep'
  task_mode: 'create' | 'relayout'
  creation_kind: 'deck' | 'pages' | 'asset'
  page_count: number | null
  output_preference: 'editable' | 'image'
  language: 'zh' | 'en'
}

/** One account-owned conversation's persisted preference revision. */
export interface PresentationConfigRecord {
  revision: number
  config: PresentationConfig
}

/** Resolve the current composer's defaults without changing any project. */
export function defaultPresentationConfig(): PresentationConfig {
  return { search_mode: 'off', task_mode: 'create', creation_kind: 'deck', page_count: null, output_preference: 'editable', language: 'zh' }
}

/** Validate the complete JSON preference object; unknown options cannot imply approval. */
export function parsePresentationConfig(value: unknown): PresentationConfig {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('PPT_CONFIG_INVALID')
  const v = value as Record<string, unknown>
  if (Object.keys(v).some(key => !['search_mode','task_mode','creation_kind','language','output_preference','page_count'].includes(key)) || !['language','output_preference','page_count'].every(key => key in v) ||
    v.search_mode !== undefined && !['off','web','deep'].includes(String(v.search_mode)) ||
    v.task_mode !== undefined && !['create','relayout'].includes(String(v.task_mode)) ||
    v.creation_kind !== undefined && !['deck','pages','asset'].includes(String(v.creation_kind)) ||
    v.page_count !== null && (typeof v.page_count !== 'number' || !Number.isInteger(v.page_count) || v.page_count < 1 || v.page_count > 80) ||
    !['image', 'editable'].includes(String(v.output_preference)) || !['zh', 'en'].includes(String(v.language))) throw new Error('PPT_CONFIG_INVALID')
  const selected = (v.creation_kind ?? 'deck') as PresentationConfig['creation_kind']
  const creation = selected === 'deck' && v.page_count === 1 ? 'pages' : selected
  if (creation === 'asset' && v.page_count !== 1 || creation === 'pages' && (typeof v.page_count !== 'number' || v.page_count > 8)) throw new Error('PPT_CONFIG_INVALID')
  return { search_mode: (v.search_mode ?? 'off') as PresentationConfig['search_mode'], task_mode: (v.task_mode ?? 'create') as PresentationConfig['task_mode'], creation_kind: creation, page_count: v.page_count as number | null, output_preference: v.output_preference as PresentationConfig['output_preference'], language: v.language as PresentationConfig['language'] }
}

/** Validate the server response before publishing it to the composer. */
export function parsePresentationRecord(value: unknown): PresentationConfigRecord {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('PPT_CONFIG_INVALID')
  const v = value as Record<string, unknown>
  if (!Number.isSafeInteger(v.revision) || Number(v.revision) < 0) throw new Error('PPT_CONFIG_INVALID')
  return { revision: Number(v.revision), config: parsePresentationConfig(v.config) }
}

/** Compare complete normalized preferences without depending on JSON property ordering. */
export function samePresentationConfig(a: PresentationConfig, b: PresentationConfig): boolean {
  return a.search_mode === b.search_mode && a.task_mode === b.task_mode && a.creation_kind === b.creation_kind && a.page_count === b.page_count && a.output_preference === b.output_preference && a.language === b.language
}

/** Current editable requirements; automatic values stay unresolved until a plan or user instruction supplies them. */
export interface RequirementValues {
  task_mode: 'auto' | 'create' | 'relayout'
  creation_kind: 'auto' | 'deck' | 'pages' | 'asset'
  page_count: number | null
  language: 'auto' | 'zh' | 'en'
  topic: string
  audience: string
  goal: string
  style: string
  requirements: string
  content_policy: 'auto' | 'preserve' | 'reorganize'
  search_mode: 'off' | 'web' | 'deep'
  output_preference: 'auto' | 'image' | 'editable'
}
export type RequirementField = keyof RequirementValues
export type RequirementSource = 'user' | 'inherited' | 'suggested' | 'default'
export type RequirementSources = Record<RequirementField, RequirementSource>
/** Ownership-checked target and the last configuration revision observed in its brief. */
export interface RequirementBinding {
  project_handle: string
  project_id: string
  applied_revision: number | null
  project_revision?: number
  template?: {source_id:string;title:string;signature:string}
}
/** One conversation's authoritative requirements and the state of its execution projection. */
export interface CurrentRequirements {
  revision: number
  values: RequirementValues
  sources: RequirementSources
  binding: RequirementBinding | null
  projection_status: 'unbound' | 'pending' | 'applied' | 'conflict'
  source_message_id: string | null
}
/** Version-four response; config is derived solely for existing clients. */
export interface CurrentPresentationRecord extends PresentationConfigRecord {
  current_requirements: CurrentRequirements
}

export const requirementFields = ['task_mode','creation_kind','page_count','language','topic','audience','goal','style','requirements','content_policy','search_mode','output_preference'] as const satisfies readonly RequirementField[]
const proseLimits = { topic:500, audience:500, goal:2000, style:2000, requirements:12000 } as const
/** Resolve unspecified requirements without choosing a page count, template, content language or output for the user. */
export function defaultRequirementValues(): RequirementValues {
  return { task_mode:'auto', creation_kind:'auto', page_count:null, language:'auto', topic:'', audience:'', goal:'', style:'', requirements:'', content_policy:'auto', search_mode:'off', output_preference:'auto' }
}
/** Construct field provenance for a newly resolved record. */
export function defaultRequirementSources(): RequirementSources {
  return Object.fromEntries(requirementFields.map(field=>[field,'default'])) as RequirementSources
}
/** Validate a partial model/HTTP/file update; approval, credentials and runtime controls are not requirement fields. */
export function parseRequirementPatch(value: unknown): Partial<RequirementValues> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('PPT_REQUIREMENTS_INVALID')
  const raw = value as Record<string, unknown>
  if (Object.keys(raw).some(key=>!requirementFields.includes(key as RequirementField))) throw new Error('PPT_REQUIREMENTS_INVALID')
  const enums: Record<string, readonly string[]> = { task_mode:['auto','create','relayout'], creation_kind:['auto','deck','pages','asset'], language:['auto','zh','en'], content_policy:['auto','preserve','reorganize'], search_mode:['off','web','deep'], output_preference:['auto','image','editable'] }
  for (const [key,item] of Object.entries(raw)) {
    if (key==='page_count') { if(item!==null && (typeof item!=='number'||!Number.isInteger(item)||item<1||item>80)) throw new Error('PPT_REQUIREMENTS_INVALID') }
    else if (key in proseLimits) { if(typeof item!=='string'||item.length>proseLimits[key as keyof typeof proseLimits]||/\u0000/.test(item)) throw new Error('PPT_REQUIREMENTS_INVALID') }
    else if (typeof item!=='string'||!enums[key]?.includes(item)) throw new Error('PPT_REQUIREMENTS_INVALID')
  }
  return raw as Partial<RequirementValues>
}
/** Validate durable/current JSON before exposing it as a readback. */
export function parseCurrentRequirements(value: unknown): CurrentRequirements {
  if (!value || typeof value!=='object'||Array.isArray(value)) throw new Error('PPT_REQUIREMENTS_INVALID')
  const raw=value as Record<string,unknown>, values=parseRequirementPatch(raw.values)
  if(!Number.isSafeInteger(raw.revision)||Number(raw.revision)<0||requirementFields.some(field=>!(field in values))||!raw.sources||typeof raw.sources!=='object'||Array.isArray(raw.sources)||!['unbound','pending','applied','conflict'].includes(String(raw.projection_status))||(raw.source_message_id!==null&&(typeof raw.source_message_id!=='string'||raw.source_message_id.length>200))) throw new Error('PPT_REQUIREMENTS_INVALID')
  const sources=raw.sources as Record<string,unknown>
  if(requirementFields.some(field=>!['user','inherited','suggested','default'].includes(String(sources[field])))||Object.keys(sources).some(field=>!requirementFields.includes(field as RequirementField))) throw new Error('PPT_REQUIREMENTS_INVALID')
  let binding:RequirementBinding|null=null
  if(raw.binding!==null){
    if(!raw.binding||typeof raw.binding!=='object'||Array.isArray(raw.binding))throw new Error('PPT_REQUIREMENTS_INVALID')
    const b=raw.binding as Record<string,unknown>
    if(typeof b.project_handle!=='string'||b.project_handle.length<20||b.project_handle.length>8192||typeof b.project_id!=='string'||!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(b.project_id)||(b.applied_revision!==null&&(!Number.isSafeInteger(b.applied_revision)||Number(b.applied_revision)<0)))throw new Error('PPT_REQUIREMENTS_INVALID')
    if(b.project_revision!==undefined&&(!Number.isSafeInteger(b.project_revision)||Number(b.project_revision)<0))throw new Error('PPT_REQUIREMENTS_INVALID')
    binding={project_handle:b.project_handle,project_id:b.project_id,applied_revision:b.applied_revision as number|null,...(b.project_revision===undefined?{}:{project_revision:Number(b.project_revision)})}
    if(b.template!==undefined){
      if(!b.template||typeof b.template!=='object'||Array.isArray(b.template))throw new Error('PPT_REQUIREMENTS_INVALID')
      const template=b.template as Record<string,unknown>
      if(typeof template.source_id!=='string'||!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(template.source_id)||typeof template.title!=='string'||template.title.length>300||typeof template.signature!=='string'||!/^[a-f0-9]{64}$/.test(template.signature))throw new Error('PPT_REQUIREMENTS_INVALID')
      binding.template={source_id:template.source_id,title:template.title,signature:template.signature}
    }
  }
  return {revision:Number(raw.revision),values:values as RequirementValues,sources:sources as RequirementSources,binding,projection_status:raw.projection_status as CurrentRequirements['projection_status'],source_message_id:raw.source_message_id as string|null}
}
/** Derive the old six-field response; automatic values never become explicit requirements. */
export function legacyPresentationConfig(values: RequirementValues): PresentationConfig {
  const count=values.creation_kind==='asset'?1:values.page_count
  const kind=values.creation_kind==='auto'?(count===1?'pages':'deck'):values.creation_kind
  return {task_mode:values.task_mode==='relayout'?'relayout':'create',creation_kind:kind,page_count:kind==='pages'&&(count===null||count>8)?2:count,language:values.language==='en'?'en':'zh',search_mode:values.search_mode,output_preference:values.output_preference==='image'?'image':'editable'}
}
/** Read version-four data while retaining the previous response for existing consumers. */
export function parseCurrentPresentationRecord(value: unknown): CurrentPresentationRecord {
  const legacy=parsePresentationRecord(value)
  const current=parseCurrentRequirements((value as Record<string,unknown>).current_requirements)
  if(current.revision!==legacy.revision)throw new Error('PPT_REQUIREMENTS_INVALID')
  return {...legacy,current_requirements:current}
}
