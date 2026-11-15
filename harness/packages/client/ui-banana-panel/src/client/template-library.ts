/** Reviewed neutral-template HTTP reads and one conversation's explicit choice. */
import { createSnapshotStore, type SnapshotStore } from '@deepseek-ai/dsh-client-store'
import {templateActionFailure,templateActionFailureFromResponse,isTemplateActionFailure,type TemplateActionFailure,type TemplateActionOperation} from './template-action-error.ts'

/** Multi-axis classification is intentionally independent of the project topic. */
export const templateAxes = ['purpose', 'page_role', 'visual_style', 'information_density', 'layout_structure', 'industry_topic', 'template_category'] as const
export type TemplateAxis = typeof templateAxes[number]
export type TemplateTags = Record<TemplateAxis, string[]>
export interface NeutralTemplatePage { id: string; page_number: number; title: string; layout_family: string; image_url: string }
export interface NeutralTemplateCard {
  reference_scope?: 'single_image'
  id: string; title: string; source_type: 'local' | 'user' | 'website' | 'wechat'; review_status: 'approved' | 'pending' | 'rejected'
  neutralized: boolean; preview_kind: 'neutral_layout_redraw' | 'neutral_native_redraw' | 'private_generated_design' | 'private_uploaded_template' | 'original_template_render' | 'sanitized_original_render' | 'external_template_render'; preview_disclaimer: string
  cover_url: string | null; tags: TemplateTags; page_count: number; body_reference_count: number; reference_sufficient: boolean
  warnings: string[]; tagging_status: string; tag_source: string; pages?: NeutralTemplatePage[]; review_reason?: string | null
  recommendation_reason?: string
}
export interface TemplateFilters { scope?: 'all' | 'mine' | 'public'; purpose?: string; search?: string; visual_style?: string; layout_structure?: string; page_role?: string; information_density?: string; industry_topic?: string; template_category?: string; offset?: number; limit?: number; status?: 'pending' | 'approved' | 'rejected' }
export interface TemplateList { items: NeutralTemplateCard[]; total: number; facets: TemplateTags }
export interface TemplateMaterial { id: string; filename: string; ready: boolean; status?:string; pages?: {number: number; image_url:string}[] }
export class TemplateRequestError extends Error { constructor(readonly status = 503,readonly failure?:TemplateActionFailure) { super('TEMPLATE_SERVICE_UNAVAILABLE') } }
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const string = (value: unknown, max = 500): value is string => typeof value === 'string' && value.length <= max
const identifier = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(value)

/** Project approved library/private cards, never filesystem paths or raw excerpts. */
function parseCard(value: unknown, previewUrl: (value: string) => string): NeutralTemplateCard {
  const personal=object(value)&&typeof value.id==='string'&&/^personal:[0-9a-f-]{36}$/.test(value.id)&&value.preview_kind==='private_generated_design'&&value.neutralized===false&&value.source_type==='user'&&value.review_status==='approved'
  const uploaded=object(value)&&typeof value.id==='string'&&/^uploaded:[0-9a-f-]{36}$/.test(value.id)&&value.preview_kind==='private_uploaded_template'&&value.neutralized===false&&value.source_type==='user'&&value.review_status==='approved'
  const original=object(value)&&value.preview_kind==='original_template_render'&&value.neutralized===false&&value.source_type==='local'&&value.review_status==='approved'
  const sanitized=object(value)&&value.preview_kind==='sanitized_original_render'&&value.neutralized===true&&value.source_type==='local'&&value.review_status==='approved'
  const external=object(value)&&value.preview_kind==='external_template_render'&&value.neutralized===false&&['website','wechat'].includes(String(value.source_type))&&value.review_status==='approved'
  if (!object(value) || !identifier(value.id) || !string(value.title, 200) || !['local', 'user','website','wechat'].includes(String(value.source_type))
    || !['approved', 'pending', 'rejected'].includes(String(value.review_status)) || !uploaded&&!personal&&!original&&!sanitized&&!external&&(value.neutralized !== true || !['neutral_layout_redraw', 'neutral_native_redraw'].includes(String(value.preview_kind)))
    || !string(value.preview_disclaimer, 1000) || !object(value.tags) || !Number.isSafeInteger(value.page_count) || Number(value.page_count) < 1
    || !Number.isSafeInteger(value.body_reference_count) || Number(value.body_reference_count) < 0 || typeof value.reference_sufficient !== 'boolean'
    || !Array.isArray(value.warnings) || value.warnings.some(item => !string(item, 1000))) throw new TemplateRequestError(502)
  const tags = {} as TemplateTags
  for (const axis of templateAxes) {
    const values = value.tags[axis] ?? (axis === 'template_category' ? [] : undefined)
    if (!Array.isArray(values) || values.length > 20 || values.some(item => !string(item, 80))) throw new TemplateRequestError(502)
    tags[axis] = values as string[]
  }
  if (value.cover_url !== null && !string(value.cover_url, 500)) throw new TemplateRequestError(502)
  const card: NeutralTemplateCard = { id: value.id, title: value.title, source_type: value.source_type as NeutralTemplateCard['source_type'],
    review_status: value.review_status as NeutralTemplateCard['review_status'], neutralized: !uploaded&&!personal&&!original&&!external, preview_kind: value.preview_kind as NeutralTemplateCard['preview_kind'],
    preview_disclaimer: value.preview_disclaimer, cover_url: typeof value.cover_url === 'string' ? previewUrl(value.cover_url) : null,
    tags, page_count: Number(value.page_count), body_reference_count: Number(value.body_reference_count), reference_sufficient: value.reference_sufficient,
    warnings: value.warnings as string[], tagging_status: string(value.tagging_status, 80) ? value.tagging_status : '', tag_source: string(value.tag_source, 80) ? value.tag_source : '' }
  if (value.reference_scope === 'single_image') card.reference_scope = 'single_image'
  if (string(value.review_reason, 500)) card.review_reason = value.review_reason
  if (string(value.recommendation_reason, 500)) card.recommendation_reason = value.recommendation_reason
  if (value.pages !== undefined) {
    if (!Array.isArray(value.pages) || value.pages.length > 12) throw new TemplateRequestError(502)
    card.pages = value.pages.map(page => {
      if (!object(page) || !identifier(page.id) || !Number.isSafeInteger(page.page_number) || Number(page.page_number) < 1 || !string(page.title, 200) || !string(page.layout_family, 80) || !string(page.image_url, 500)) throw new TemplateRequestError(502)
      return { id: page.id, page_number: Number(page.page_number), title: page.title, layout_family: page.layout_family, image_url: previewUrl(page.image_url) }
    })
  }
  return card
}

/** Outcome of a confirmed material deletion; source contents never cross this result. */
export type MaterialDeletionResult = { ok: true; materials: { id: string; filename: string; bytes: number; role: import('./material-upload.ts').MaterialRole }[] } | { ok: false; code: string }

/** Cookie-authenticated API access. The deployment supplies its existing private API prefix. */
export class TemplateLibraryHttpClient {
  /** @param basePath - Same-origin path ending in /api/template-library. @param request - Browser fetch, replaced only by offline tests. */
  constructor(private readonly basePath: string, private readonly request: typeof fetch = (...args) => fetch(...args)) {
    if (!/^\/[A-Za-z0-9/_-]*\/api\/template-library$/.test(basePath) && basePath !== '/api/template-library') throw new TemplateRequestError(400)
  }
  private previewUrl = (path: string): string => {
    if (!/^\/api\/template-library\/[A-Za-z0-9_.:-]+\/previews\/[1-9][0-9]*\.png$/.test(path)) throw new TemplateRequestError(502)
    return this.basePath + path.slice('/api/template-library'.length)
  }
  private async call(suffix: string, init: RequestInit = {}): Promise<unknown> {
    const response = await this.request(this.basePath + suffix, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', ...init })
    const value: unknown = await response.json().catch(() => null)
    if (!response.ok || !object(value) || value.success !== true || !('data' in value)) throw new TemplateRequestError(response.status)
    return value.data
  }
  /** @param filters - Six-axis filters. @param submissions - Restricts ordinary users to their own submissions. @returns Safe neutral cards. */
  async list(filters: TemplateFilters = {}, submissions = false): Promise<TemplateList> {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(filters)) if (value !== undefined && value !== '') query.set(key, String(value))
    const raw = await this.call((submissions ? '/submissions' : '') + '?' + query.toString())
    if (!object(raw) || !Array.isArray(raw.items) || raw.items.length > 50 || !Number.isSafeInteger(raw.total) || Number(raw.total) < 0 || !object(raw.facets)) throw new TemplateRequestError(502)
    const facets = {} as TemplateTags
    for (const axis of templateAxes) {
      const values = raw.facets[axis] ?? (axis === 'template_category' ? [] : undefined)
      if (!Array.isArray(values) || values.some(v => !string(v, 80))) throw new TemplateRequestError(502)
      facets[axis] = values as string[]
    }
    return { items: raw.items.map(v => parseCard(v, this.previewUrl)), total: Number(raw.total), facets }
  }
  /** @param id - A catalog ID, never an arbitrary path. @returns Neutral representative pages. */
  async detail(id: string): Promise<NeutralTemplateCard> {
    if (!identifier(id)) throw new TemplateRequestError(400)
    return parseCard(await this.call('/' + encodeURIComponent(id)), this.previewUrl)
  }
  /** Read only task-matched, approved templates with at least four existing preview pages. */
  async recommend(projectId: string): Promise<NeutralTemplateCard[]> {
    if (!identifier(projectId)) throw new TemplateRequestError(400)
    const raw = await this.call('/recommendations?project_id=' + encodeURIComponent(projectId))
    if (!object(raw) || !Array.isArray(raw.items) || raw.items.length > 4) throw new TemplateRequestError(502)
    const cards = raw.items.map(item => parseCard(item, this.previewUrl))
    if (cards.some(card => card.review_status !== 'approved' || (card.pages?.length ?? 0) < (card.reference_scope==='single_image'||['private_generated_design','private_uploaded_template'].includes(card.preview_kind)?1:4))) throw new TemplateRequestError(502)
    return cards
  }
  /** Persist the explicit current template choice; no image-generation action is included. */
  async confirm(projectId: string, choice: {source_id: string} | {material_id: string}, revision: number, requestId: string): Promise<unknown> {
    if (!identifier(projectId) || !Number.isSafeInteger(revision) || revision < 0) throw new TemplateRequestError(400)
    const response = await this.request(this.projectPath(projectId) + '/action', {method:'POST',credentials:'same-origin',redirect:'error',headers:{'Content-Type':'application/json','Idempotency-Key':requestId,'X-Wuyu-Interaction':'template-confirmation'},body:JSON.stringify({action:'confirm_template_choice',payload:choice,expected_revision:revision})})
    const raw:unknown = await response.json().catch(() => null)
    if (!response.ok || !object(raw) || raw.success !== true || !object(raw.data)) throw new TemplateRequestError(response.status,templateActionFailureFromResponse(raw,response.status,'confirm'))
    return raw.data
  }
  /** @param projectId - Owned current project. @param materialId - Uploaded source to erase. @param revision - User-reviewed workflow revision. @param requestId - Stable deletion identity. @returns Current workflow or a fixed diagnostic code. */
  async deleteMaterial(projectId: string, materialId: string, revision: number, requestId: string): Promise<MaterialDeletionResult> {
    if (!identifier(projectId) || !identifier(materialId) || !Number.isSafeInteger(revision) || revision < 0) return { ok: false, code: 'INVALID_REQUEST' }
    const response = await this.request(this.projectPath(projectId) + '/materials/' + encodeURIComponent(materialId) + '/delete', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
      headers: { 'Content-Type': 'application/json', 'X-Wuyu-Interaction': 'material-deletion' },
      body: JSON.stringify({ confirmed: true, expected_revision: revision, request_id: requestId }),
    })
    const raw: unknown = await response.json().catch(() => null)
    if (!response.ok) {
      const code = object(raw) && object(raw.error) ? raw.error.code : undefined
      const allowed = ['MATERIAL_IN_USE', 'MATERIAL_DELETE_PENDING', 'MATERIAL_DELETE_FORBIDDEN', 'MATERIAL_DELETE_PATH_DENIED', 'REVISION_CONFLICT', 'MATERIAL_NOT_FOUND']
      return { ok: false, code: typeof code === 'string' && allowed.includes(code) ? code : 'MATERIAL_DELETE_UNAVAILABLE' }
    }
    if (!object(raw) || raw.success !== true || !object(raw.data) || raw.data.project_id !== projectId || raw.data.deleted_material_id !== materialId || !Number.isSafeInteger(raw.data.revision) || !object(raw.data.workflow) || !Array.isArray(raw.data.materials) || raw.data.materials.some(item => object(item) && item.id === materialId)) return { ok: false, code: 'MATERIAL_DELETE_UNAVAILABLE' }
    const materials = raw.data.materials.filter(item => object(item) && item.origin !== 'web_research')
    if (!materials.every(item => object(item) && identifier(item.id) && string(item.filename, 500) && ['material', 'template', 'logo'].includes(String(item.role ?? 'material')) && Number.isSafeInteger(item.bytes ?? 0) && Number(item.bytes ?? 0) >= 0)) return { ok: false, code: 'MATERIAL_DELETE_UNAVAILABLE' }
    return { ok: true, materials: materials.map(item => { const row = item as Record<string, unknown>; return { id: row.id as string, filename: row.filename as string, bytes: Number(row.bytes ?? 0), role: (row.role ?? 'material') as import('./material-upload.ts').MaterialRole } }) }
  }
  private projectPath(projectId:string):string { return this.basePath.replace(/\/api\/template-library$/, '/api/projects/' + encodeURIComponent(projectId) + '/workflow') }
  /** The picker and its separate Upload button authorize only private template parsing. */
  async upload(projectId: string, file: File, requestId: string): Promise<void> {
    if (!identifier(projectId)) throw new TemplateRequestError(400)
    const form = new FormData(); form.append('files', file); form.append('role', 'template')
    const response = await this.request(this.projectPath(projectId) + '/materials', {method:'POST',credentials:'same-origin',redirect:'error',headers:{'X-Upload-Id':requestId},body:form})
    const raw:unknown = await response.json().catch(() => null)
    if (!response.ok || !object(raw) || raw.success !== true) throw new TemplateRequestError(response.status,templateActionFailureFromResponse(raw,response.status,'upload'))
  }
  /** @param projectId - Owned project. @param materialId - Already parsed private template. @param requestId - Stable retry identity. @returns Pending, private neutral derivative. */
  async submit(projectId: string, materialId: string, requestId: string): Promise<NeutralTemplateCard> {
    return parseCard(await this.call('/submissions', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': requestId }, body: JSON.stringify({ project_id: projectId, material_id: materialId }) }), this.previewUrl)
  }
  /** @param id - Submission or builtin. @param decision - Administrator's explicit decision. @param reason - Audit reason. @param tags - Optional manual correction. @param requestId - Retry identity. @returns Reviewed derivative. */
  async review(id: string, decision: 'approve' | 'reject', reason: string, tags: TemplateTags | undefined, requestId: string): Promise<NeutralTemplateCard> {
    if (!identifier(id)) throw new TemplateRequestError(400)
    return parseCard(await this.call('/' + encodeURIComponent(id) + '/review', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': requestId }, body: JSON.stringify({ decision, reason, ...(tags ? { tags } : {}) }) }), this.previewUrl)
  }
  /** Explicit administrator action invokes at most one configured text-model classification. */
  async tag(id: string): Promise<void> {
    if (!identifier(id)) throw new TemplateRequestError(400)
    const result = await this.call('/tag-queue', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ template_id: id, max_items: 1, dry_run: false, retry_failed: true }) })
    if (!object(result) || !Array.isArray(result.items) || result.items.some(item => !object(item) || item.status !== 'completed')) throw new TemplateRequestError(502)
  }
}

export interface TemplateLibrarySnapshot {
  phase: 'idle' | 'loading' | 'ready' | 'error'; items: NeutralTemplateCard[]; total: number; facets: TemplateTags
  filters: TemplateFilters; selected: NeutralTemplateCard | null; detail: NeutralTemplateCard | null
  tab: 'library' | 'mine' | 'review'; admin: boolean; busy: boolean; actionError: boolean; projectId: string | null; materials: TemplateMaterial[]
  recommendationPhase:'idle'|'loading'|'ready'|'error'; recommended:NeutralTemplateCard[]
  confirmedChoice:{id:string;title:string;kind:'library'|'upload'}|null; maxTemplateBytes:number
  templateLimitsStatus:'idle'|'loading'|'ready'|'error';actionFailure:TemplateActionFailure|null
}
export interface TemplateLibraryDependencies {
  api: TemplateLibraryHttpClient
  identity(): Promise<{ admin: boolean }>
  workflow(handle: string): Promise<unknown>
  persistence?: { read(): string | null; write(value: string): void }
}
const emptyTags = (): TemplateTags => ({ purpose: [], page_role: [], visual_style: [], information_density: [], layout_structure: [], industry_topic: [], template_category: [] })

/** Only submission() makes a choice model-visible, through ordinary logged user text. */
export class TemplateLibraryController {
  readonly state: SnapshotStore<TemplateLibrarySnapshot>
  private disposed = false
  private readVersion = 0
  private detailVersion = 0
  private materialVersion = 0
  private uploadLimits:{handle:string;version:number;projectId:string;maxBytes:number}|null=null
  private choiceVersion = 0
  private hydration: Promise<void> | undefined
  private readonly submissionKeys = new Map<string, string>()
  private confirmedHandle: string | null = null
  private recommendationVersion = 0
  private readonly uploadKeys = new WeakMap<File, string>()
  /** @param dependencies - Verified API/identity access and Session-specific persistence. */
  constructor(private readonly dependencies: TemplateLibraryDependencies) {
    this.state = createSnapshotStore<TemplateLibrarySnapshot>({ phase: 'idle', items: [], total: 0, facets: emptyTags(), filters: {}, selected: null, detail: null, tab: 'library', admin: false, busy: false, actionError: false, projectId: null, materials: [], recommendationPhase:'idle',recommended:[],confirmedChoice:null,maxTemplateBytes:0,templateLimitsStatus:'idle',actionFailure:null })
  }
  /** Revalidate a saved ID once, without loading the whole library or any model. */
  hydrateOnce(): Promise<void> {
    if (this.hydration) return this.hydration
    let saved: unknown
    try { saved = JSON.parse(this.dependencies.persistence?.read() ?? 'null') } catch { /* Malformed browser state is not a selected template. */ }
    if (!object(saved) || !identifier(saved.id)) return this.hydration = Promise.resolve()
    const version = this.choiceVersion
    this.hydration = this.dependencies.api.detail(saved.id).then(chosen => {
      if (!this.disposed && version === this.choiceVersion && chosen.review_status === 'approved') this.state.update(s => { s.selected = chosen })
    }).catch(() => { /* A withdrawn saved template is not restored; ordinary generation remains available. */ })
    return this.hydration
  }
  /** @param filters - Next read-only filter. @param tab - Server checks all administrative operations independently. */
  async load(filters: TemplateFilters = this.state.getSnapshot().filters, tab = this.state.getSnapshot().tab): Promise<void> {
    if (this.disposed) return
    const version = ++this.readVersion
    this.state.update(s => { s.phase = 'loading'; s.filters = filters; s.tab = tab })
    try {
      const [identity, result] = await Promise.all([this.dependencies.identity(), this.dependencies.api.list({ ...filters, ...(tab === 'review' ? { status: 'pending' as const } : {}), limit: filters.limit ?? 50 }, tab !== 'library')])
      if (this.disposed || version !== this.readVersion) return
      const items = tab === 'review' ? result.items.filter(item => item.review_status === 'pending') : result.items
      this.state.update(s => { s.phase = 'ready'; s.items = items; s.total = result.total; s.facets = result.facets; s.admin = identity.admin; s.tab = tab === 'review' && !identity.admin ? 'mine' : tab })
      await this.hydrateOnce()
    } catch {
      if (!this.disposed && version === this.readVersion) this.state.update(s => { s.phase = 'error' })
    }
  }
  /** @param id - A neutral library card to inspect; inspection never starts a model. */
  async inspect(id: string): Promise<void> {
    const version = ++this.detailVersion
    try {
      const detail = await this.dependencies.api.detail(id)
      if (!this.disposed && version === this.detailVersion) this.state.update(s => { s.detail = detail; s.actionError = false })
    } catch { if (!this.disposed && version === this.detailVersion) this.state.update(s => { s.actionError = true }) }
  }
  /** @param card - Explicit approved choice, or null for automatic matching. Selection does not edit the draft. */
  choose(card: NeutralTemplateCard | null): void {
    if (this.disposed || card && card.review_status !== 'approved') return
    this.choiceVersion++
    this.state.update(s => { s.selected = card })
    try { this.dependencies.persistence?.write(JSON.stringify(card ? { id: card.id } : null)) } catch { /* Session-local storage can be blocked; in-memory selection remains usable. */ }
  }
  /** @param handle - Current owned workflow handle. @returns Only parsed template material names and IDs. */
  async materials(handle: string | null): Promise<void> {
    if (this.disposed) return
    const version = ++this.materialVersion
    this.uploadLimits=null
    this.state.update(s => { if(!s.actionFailure)s.actionError=false;s.templateLimitsStatus='loading' })
    if (!handle) { this.state.update(s => { s.materials = []; s.projectId = null;s.templateLimitsStatus='idle';s.maxTemplateBytes=0 }); return }
    try {
      const raw = await this.dependencies.workflow(handle)
      if (!object(raw) || !identifier(raw.project_id) || !Array.isArray(raw.materials)) throw new TemplateRequestError(502)
      const items: TemplateMaterial[] = raw.materials.filter(m => object(m) && m.role === 'template' && identifier(m.id) && string(m.filename, 300)).map(m => {
        const material = m as Record<string, unknown>
        const pages=Array.isArray(material.template_images)?material.template_images.filter(image=>object(image)&&Number.isSafeInteger(image.number)&&string(image.image_url,8192)&&/^(?:https?:\/\/|\/api\/projects\/)/.test(image.image_url)).map(image=>{const page=image as Record<string,unknown>;return {number:Number(page.number),image_url:page.image_url as string}}):[]
        return { id: material.id as string, filename: material.filename as string, ready: material.status === 'completed' && pages.length > 0,status:String(material.status||''),pages }
      })
      const limits=object(raw.material_limits)?raw.material_limits:{}
      const valid=Number.isSafeInteger(limits.max_file_bytes)&&Number(limits.max_file_bytes)>0
      if (!this.disposed && version === this.materialVersion){
        this.uploadLimits=valid?{handle,version,projectId:raw.project_id as string,maxBytes:Number(limits.max_file_bytes)}:null
        this.state.update(s => { if(s.projectId!==raw.project_id)s.confirmedChoice=null;s.materials=items;s.projectId=raw.project_id as string
          s.templateLimitsStatus=valid?'ready':'error';s.maxTemplateBytes=valid?Number(limits.max_file_bytes):0
          if(!s.actionFailure||valid&&s.actionFailure.code==='TEMPLATE_LIMITS_UNAVAILABLE'){s.actionError=false;s.actionFailure=null}
        })
      }
    } catch { if (!this.disposed && version === this.materialVersion){this.uploadLimits=null;this.state.update(s => {s.materials=[];s.projectId=null;s.templateLimitsStatus='error';s.maxTemplateBytes=0;s.actionError=true})} }
  }
  /** Load recommendations from this project's latest brief, preserving private uploaded originals. */
  async recommendations(handle:string):Promise<void>{
    const version=++this.recommendationVersion
    this.state.update(s=>{s.recommendationPhase='loading';s.actionError=false})
    try{
      await this.materials(handle)
      const id=this.state.getSnapshot().projectId
      if(!id)throw new TemplateRequestError(400)
      const cards=await this.dependencies.api.recommend(id)
      if(!this.disposed&&version===this.recommendationVersion)this.state.update(s=>{s.recommended=cards;s.recommendationPhase='ready';s.detail=cards.find(card=>card.id===s.selected?.id)??cards[0]??null})
    }catch{if(!this.disposed&&version===this.recommendationVersion)this.state.update(s=>{s.recommendationPhase='error'})}
  }
  /** Validate and store the actual choice without sending a prompt or calling an image model. */
  async confirmChoice(handle:string,choice:{source_id:string}|{material_id:string}):Promise<boolean>{
    if(this.disposed||this.state.getSnapshot().busy)return false
    await this.action(async()=>{
      const raw=await this.dependencies.workflow(handle)
      if(!object(raw)||!identifier(raw.project_id)||!Number.isSafeInteger(raw.revision))throw new TemplateRequestError(502)
      const key=raw.project_id+':choice:'+JSON.stringify(choice)+':'+String(raw.revision)
      const requestId=this.submissionKeys.get(key)??crypto.randomUUID();this.submissionKeys.set(key,requestId)
      this.confirmedHandle=handle
      const confirmed=await this.dependencies.api.confirm(raw.project_id,choice,Number(raw.revision),requestId)
      if(!object(confirmed)||!object(confirmed.template_selection)||confirmed.template_selection.confirmed!==true)throw new TemplateRequestError(502)
      const selection=confirmed.template_selection
      if(!identifier(selection.source_id)||!string(selection.title,300)||!['library','upload'].includes(String(selection.source_kind)))throw new TemplateRequestError(502)
      if(!this.disposed)this.state.update(s=>{s.confirmedChoice={id:selection.source_id as string,title:selection.title as string,kind:selection.source_kind as 'library'|'upload'}})
    },'confirm')
    return !this.disposed&&!this.state.getSnapshot().actionError&&!this.state.getSnapshot().actionFailure
  }
  /** @param handle - Mounted project capability. @param materialId - User-confirmed source ID. @param revision - Version displayed at confirmation. @param requestId - Deletion identity for safe retries. @returns Only the owned project's deletion outcome. */
  async deleteMaterial(handle: string, materialId: string, revision: number, requestId: string): Promise<MaterialDeletionResult> {
    if (this.disposed) return { ok: false, code: 'MATERIAL_DELETE_UNAVAILABLE' }
    try {
      const current = await this.dependencies.workflow(handle)
      if (this.disposed || !object(current) || !identifier(current.project_id) || !Number.isSafeInteger(current.revision)) return { ok: false, code: 'MATERIAL_DELETE_UNAVAILABLE' }
      if (Number(current.revision) !== revision) return { ok: false, code: 'REVISION_CONFLICT' }
      const result = await this.dependencies.api.deleteMaterial(current.project_id, materialId, revision, requestId)
      if (result.ok && !this.disposed) {
        this.materialVersion++; this.choiceVersion++
        this.state.update(state => { state.materials = state.materials.filter(item => item.id !== materialId); if (state.confirmedChoice?.id === materialId || state.confirmedChoice?.id === 'material:' + materialId) state.confirmedChoice = null })
      }
      return result
    } catch { return { ok: false, code: 'MATERIAL_DELETE_UNAVAILABLE' } }
  }
  /** Upload only after the user's separate Upload click; file selection itself stays local. */
  uploadTemplate(handle:string,file:File):Promise<void>{
    return this.action(async()=>{
      if(!/\.(pptx|pdf|png|jpe?g|webp)$/i.test(file.name)||file.size<1)throw new TemplateRequestError(400,templateActionFailure('TEMPLATE_UPLOAD_INVALID_FILE',400,'upload'))
      const version=this.materialVersion+1
      await this.materials(handle)
      const limits=this.uploadLimits,snapshot=this.state.getSnapshot()
      if(!limits||limits.handle!==handle||limits.version!==version||this.materialVersion!==version||snapshot.templateLimitsStatus!=='ready'||snapshot.projectId!==limits.projectId)throw new TemplateRequestError(503,templateActionFailure('TEMPLATE_LIMITS_UNAVAILABLE',503,'upload'))
      if(file.size>limits.maxBytes)throw new TemplateRequestError(413,templateActionFailure('MATERIAL_UPLOAD_LIMIT',413,'upload'))
      const key=this.uploadKeys.get(file)??crypto.randomUUID();this.uploadKeys.set(file,key)
      await this.dependencies.api.upload(limits.projectId,file,key);await this.materials(handle)
    },'upload')
  }
  private async action(operation: () => Promise<void>,kind?:TemplateActionOperation): Promise<void> {
    if (this.disposed || this.state.getSnapshot().busy) return
    this.state.update(s => {s.busy=true;s.actionError=false;s.actionFailure=null})
    try { await operation() } catch(error) { if (!this.disposed) this.state.update(s => {
      s.actionError=true
      s.actionFailure=kind?(error instanceof TemplateRequestError&&isTemplateActionFailure(error.failure,kind)?templateActionFailure(error.failure.code,error.failure.status,kind):templateActionFailure(undefined,error instanceof TemplateRequestError?error.status:503,kind)):null
    }) }
    finally { if (!this.disposed) this.state.update(s => { s.busy = false }) }
  }
  /** @param materialId - Current project's parsed private template. */
  submit(materialId: string): Promise<void> {
    return this.action(async () => {
      const snapshot = this.state.getSnapshot(), project = snapshot.projectId
      if (!project || !snapshot.materials.some(m => m.id === materialId && m.ready)) throw new TemplateRequestError(400)
      const key = project + ':' + materialId
      const requestId = this.submissionKeys.get(key) ?? crypto.randomUUID()
      this.submissionKeys.set(key, requestId)
      const created = await this.dependencies.api.submit(project, materialId, requestId)
      if (!this.disposed) { this.state.update(s => { s.detail = created }); await this.load({}, 'mine') }
    })
  }
  /** @param id - One explicit administrator-selected template; never a full-library paid batch. */
  tag(id: string): Promise<void> {
    return this.action(async () => {
      if (!this.state.getSnapshot().admin) throw new TemplateRequestError(403)
      await this.dependencies.api.tag(id)
      if (!this.disposed) { await this.inspect(id); await this.load() }
    })
  }
  /** @param id - Submission ID. @param decision - Explicit admin decision. @param reason - Audit comment. @param tags - Optional manual corrections. */
  review(id: string, decision: 'approve' | 'reject', reason: string, tags?: TemplateTags): Promise<void> {
    return this.action(async () => {
      if (!this.state.getSnapshot().admin) throw new TemplateRequestError(403)
      const reviewed = await this.dependencies.api.review(id, decision, reason, tags, crypto.randomUUID())
      if (!this.disposed) { this.state.update(s => { s.detail = reviewed }); await this.load() }
    })
  }
  /** @param text - User-authored body. @param summary - Localized wording. @returns Logged structured reference selection; no free-text permission grants. */
  /** Attach the template marker only while the server still holds that exact confirmation. */
  async currentSubmission(text: string, summary = '模板选择'): Promise<string> {
    const confirmed=this.state.getSnapshot().confirmedChoice
    if(!confirmed)return text
    let current=false
    try{
      const raw=this.confirmedHandle?await this.dependencies.workflow(this.confirmedHandle):null
      const selection=object(raw)&&object(raw.template_selection)?raw.template_selection as Record<string,unknown>:null
      current=!!selection&&selection.confirmed===true&&selection.source_id===confirmed.id
    }catch{}
    if(!current){if(!this.disposed)this.state.update(s=>{if(s.confirmedChoice?.id===confirmed.id)s.confirmedChoice=null});return text}
    return this.submission(text,summary)
  }
  submission(text: string, summary = '模板选择'): string {
    const confirmed=this.state.getSnapshot().confirmedChoice
    if(confirmed)return text+'\n\n'+summary+'：'+confirmed.title+'\n<wuyu-template-selection>'+JSON.stringify({source_id:confirmed.id,confirmed:true,source_kind:confirmed.kind})+'</wuyu-template-selection>'
    return text
  }
  /** Prevent late reads from mutating a closed conversation. */
  dispose(): void { this.disposed = true; this.readVersion++; this.detailVersion++; this.materialVersion++ }
}
