/** Session-owned files stay local until one explicit batch confirmation. */
import type { SnapshotStore } from '@deepseek-ai/dsh-client-store'
import type { MaterialBinding, MaterialRole, MaterialNotice } from './material-upload.ts'
import { MATERIAL_ACCEPT } from './material-upload.ts'
import { MaterialUploadError, type MaterialLimits, type BinaryUploadResult, type MaterialUploadDiagnostics } from './material-http.ts'

export type UploadPhase = 'staged' | 'queued' | 'uploading' | 'registering' | 'uploaded' | 'attached' | 'failed' | 'paused'
export interface UploadRow { id: string; filename: string; size: number; role: MaterialRole; phase: UploadPhase; submitted: boolean; percent: number; error: string | null; errorDetails?: MaterialUploadDiagnostics; sending?: boolean; sent?: boolean }
export interface UploadSnapshot { rows: readonly UploadRow[]; limits: MaterialLimits | null; busy: boolean; batchActive: boolean; intaking: boolean; syncing: boolean; syncFailed: boolean; intakeError: string | null }
export const initialUploadSnapshot = (): UploadSnapshot => ({ rows: [], limits: null, busy: false, batchActive: false, intaking: false, syncing: false, syncFailed: false, intakeError: null })
interface Project { handle: string; id?: string; materialCount: number }
interface QueueDeps {
  limits(): Promise<MaterialLimits>
  project(handle: string | null): Promise<Project>
  upload(file: File, input: { projectHandle: string; role: MaterialRole; uploadId: string; batchCount: number; batchBytes: number }, signal: AbortSignal, progress: (percent: number) => void): Promise<BinaryUploadResult>
  bind(binding: MaterialBinding): Promise<MaterialNotice>
  ready(project: Project): void
  persist?(draft: PreparedMaterialDraft): void
}
interface PrivateFile { file: File; handle: string | null; batchCount: number; batchBytes: number; attempted: boolean; project?: Project; ids?: string[] }
interface ConfirmedBatch { rows: string[]; binding?: MaterialBinding; project?: Project }
/** Only uploaded descriptors are recoverable; local file bytes are never serialized. */
export interface PreparedMaterialDraft { batches: { rows: UploadRow[]; binding: MaterialBinding }[] }

/** Partition metadata without reading file bodies or discarding later files. */
export function materialBatches(files: readonly File[], limits: MaterialLimits): File[][] {
  const batches: File[][] = []
  let current: File[] = [], bytes = 0
  for (const file of files) {
    if (current.length && (current.length >= limits.max_batch_files || bytes + file.size > limits.max_batch_bytes)) { batches.push(current); current = []; bytes = 0 }
    current.push(file); bytes += file.size
  }
  if (current.length) batches.push(current)
  return batches
}

/** op180: files of one confirmed batch upload in parallel, bounded by count and in-flight bytes. */
export const MATERIAL_UPLOAD_CONCURRENCY = 5, MATERIAL_UPLOAD_INFLIGHT_BYTES = 100 * 1024 * 1024
/** Own staged bytes, the confirmed cohort, retries and its single context notice. */
export class MaterialUploadQueue {
  private readonly files = new Map<string, PrivateFile>()
  private readonly projects = new Map<string, Promise<Project>>()
  private readonly scopes = new Map<string, string>()
  private readonly rowScopes = new Map<string, string | null>()
  private intake: Promise<void> = Promise.resolve()
  private intakePending = 0
  private batch: ConfirmedBatch | null = null
  private running = false
  private disposed = false
  private readonly activeUploads = new Map<string, AbortController>()
  private readonly prepared = new Map<string, { rows: string[]; binding: MaterialBinding; notice: MaterialNotice; sending: boolean }>()
  private readonly confirmed = new Map<string, { rows: string[]; binding: MaterialBinding }>()
  private restorePending: PreparedMaterialDraft | null = null
  constructor(readonly state: SnapshotStore<UploadSnapshot>, private readonly deps: QueueDeps) {}
  private set(patch: Partial<UploadSnapshot>) { this.state.set({ ...this.state.getSnapshot(), ...patch }) }
  private row(id: string, patch: Partial<UploadRow>) { this.set({ rows: this.state.getSnapshot().rows.map(row => row.id === id ? { ...row, ...patch } : row) }) }
  private scope(handle: string | null) { return handle ? this.scopes.get(handle) || handle : 'new' }
  private persist() {
    try { this.deps.persist?.({ batches: [...this.confirmed.values()].map(batch => ({ rows: this.state.getSnapshot().rows.filter(row => batch.rows.includes(row.id)).map(row => ({ ...row, sending: false })), binding: batch.binding })) }) }
    catch { /* Storage availability cannot undo accepted uploads or a submitted message. */ }
  }
  /** Count only prepared material that an explicit send has not already claimed. */
  submissionCount(): number {
    return [...this.prepared.values()].filter(batch => !batch.sending).reduce((sum, batch) => sum + batch.rows.length, 0)
  }
  /** Freeze pending batches into one ordinary user message; settle only those exact batches. */
  prepareSubmission(text: string): string | { text: string; settled(success: boolean): void } {
    if (this.blocksSubmission()) throw new Error('MATERIALS_NOT_PREPARED')
    const batches = [...this.prepared.values()].filter(batch => !batch.sending)
    if (!batches.length) return text
    const ids = new Set(batches.flatMap(batch => batch.rows))
    for (const batch of batches) batch.sending = true
    this.set({ rows: this.state.getSnapshot().rows.map(row => ids.has(row.id) ? { ...row, sending: true } : row) })
    const summary = batches.map(batch => batch.notice.summary).join('\n')
    const context = batches.map(batch => '<wuyu-material-context>' + batch.notice.context + '</wuyu-material-context>').join('\n')
    let settled = false
    return { text: (text.trim() ? text : summary) + '\n\n' + context, settled: success => {
      if (settled || this.disposed) return
      settled = true
      for (const batch of batches) {
        if (success) {
          this.prepared.delete(batch.binding.requestId)
          this.confirmed.delete(batch.binding.requestId)
          for (const id of batch.rows) this.files.delete(id)
        }
        else batch.sending = false
      }
      this.set({ rows: this.state.getSnapshot().rows.map(row => ids.has(row.id) ? { ...row, sending: false, sent: success } : row) })
      this.persist()
    } }
  }
  /** Revalidate uploaded descriptors after reload without creating model input or restoring local bytes. */
  async restore(value: unknown): Promise<void> {
    if (this.disposed || this.state.getSnapshot().rows.length || !value || typeof value !== 'object') return
    const batches = (value as Partial<PreparedMaterialDraft>).batches
    if (!Array.isArray(batches) || batches.length > 100) return
    for (const batch of batches) {
      if (!batch || !Array.isArray(batch.rows) || !batch.rows.length || batch.rows.length > 100 || !batch.binding || typeof batch.binding.projectHandle !== 'string' || typeof batch.binding.requestId !== 'string' || !Array.isArray(batch.binding.materialIds) || batch.binding.materialIds.some(id => typeof id !== 'string') || batch.rows.some(row => !row || typeof row.id !== 'string' || typeof row.filename !== 'string' || !Number.isSafeInteger(row.size) || row.size < 0 || !['material','template','logo'].includes(row.role))) return
    }
    const ids = batches.flatMap(batch => batch.rows.map(row => row.id))
    if (ids.length > 100 || new Set(ids).size !== ids.length || new Set(batches.map(batch => batch.binding.requestId)).size !== batches.length) return
    this.restorePending = { batches }
    this.set({ rows: batches.flatMap(batch => batch.rows.map(row => ({ id:row.id,filename:row.filename,size:row.size,role:row.role,phase:'uploaded' as const,submitted:true,percent:100,error:null }))) })
    await this.restoreSaved()
  }
  private async restoreSaved(): Promise<void> {
    const savedDraft = this.restorePending
    if (!savedDraft || this.disposed || this.state.getSnapshot().syncing) return
    this.set({ syncing: true, syncFailed: false })
    try {
      const verified = []
      for (const saved of savedDraft.batches) {
        const notice = await this.deps.bind(saved.binding)
        if (this.disposed) return
        verified.push({ saved, notice })
      }
      const restoredIds = new Set(savedDraft.batches.flatMap(batch => batch.rows.map(row => row.id)))
      const restoredRows: UploadRow[] = []
      for (const { saved, notice } of verified) {
        const rows = saved.rows.map(row => ({ id: row.id, filename: row.filename, size: row.size, role: row.role, phase: 'attached' as const, submitted: true, percent: 100, error: null, sent: notice.submitted }))
        const batch = { rows: rows.map(row => row.id), binding: saved.binding }
        for (const row of rows) this.rowScopes.set(row.id,saved.binding.projectHandle)
        if (!notice.submitted) {
          this.confirmed.set(saved.binding.requestId,batch)
          this.prepared.set(saved.binding.requestId, { ...batch, notice, sending: false })
        }
        restoredRows.push(...rows)
        this.deps.ready({ handle: saved.binding.projectHandle, id: notice.project_id, materialCount: saved.binding.materialIds.length })
      }
      this.restorePending = null
      this.set({ rows: [...restoredRows, ...this.state.getSnapshot().rows.filter(row => !restoredIds.has(row.id))], intakeError: null })
      this.persist()
    } catch { if (!this.disposed) this.set({ syncFailed: true }) }
    finally { if (!this.disposed) this.set({ syncing: false }) }
  }
  private validate(ids: readonly string[], limits: MaterialLimits) {
    const selected = new Set(ids), occupied = new Map<string, number>(), rows = [...this.state.getSnapshot().rows]
    for (const row of rows) {
      const item = this.files.get(row.id)
      const handle = item?.handle ?? this.rowScopes.get(row.id)
      if (handle !== undefined && !selected.has(row.id) && !['failed', 'paused'].includes(row.phase)) occupied.set(this.scope(handle), (occupied.get(this.scope(handle)) || 0) + 1)
    }
    const next = rows.map(row => {
      const item = this.files.get(row.id)
      if (!selected.has(row.id) || row.submitted || !item) return row
      const extension = item.file.name.includes('.') ? '.' + item.file.name.split('.').at(-1)!.toLowerCase() : '', key = this.scope(item.handle)
      const error = !item.file.size ? 'UPLOAD_EMPTY' : item.file.size > limits.max_file_bytes ? 'MATERIAL_UPLOAD_LIMIT' : !MATERIAL_ACCEPT[row.role].split(',').includes(extension) ? 'UPLOAD_TYPE_UNSUPPORTED' : (occupied.get(key) || 0) >= limits.max_project_files ? 'MATERIAL_COUNT_LIMIT' : null
      if (!error) occupied.set(key, (occupied.get(key) || 0) + 1)
      return { ...row, phase: error ? 'failed' as const : 'staged' as const, error, errorDetails: undefined, percent: 0 }
    })
    this.set({ rows: next, limits, intakeError: null })
  }
  private validateLater(ids: readonly string[]): Promise<void> {
    this.intakePending++; this.set({ intaking: true })
    this.intake = this.intake.then(async () => {
      if (this.disposed) return
      try {
        const limits = this.state.getSnapshot().limits || await this.deps.limits()
        if (!this.disposed) this.validate(ids, limits)
      } catch (error) {
        const code = error instanceof MaterialUploadError ? error.code : 'UPLOAD_SERVICE_UNAVAILABLE'
        if (!this.disposed) this.set({ rows: this.state.getSnapshot().rows.map(row => ids.includes(row.id) && !row.submitted ? { ...row, phase: 'failed', error: code, errorDetails: error instanceof MaterialUploadError ? { status: error.status, requestId: error.requestId, stage: error.stage } : undefined } : row), intakeError: code })
      }
    }).finally(() => { this.intakePending--; if (!this.disposed) this.set({ intaking: this.intakePending > 0 }) })
    return this.intake
  }
  /** Append picker, pasted or dropped files locally; only read-only limits may be requested. */
  add(input: ArrayLike<File>, role: MaterialRole, handle: string | null): Promise<void> {
    if (this.disposed) return Promise.resolve()
    const rows = [...this.state.getSnapshot().rows], ids: string[] = []
    for (const file of Array.from(input)) {
      if (rows.some(row => { const old = this.files.get(row.id); return old?.file === file && row.role === role && this.scope(old.handle) === this.scope(handle) })) continue
      const id = crypto.randomUUID()
      this.rowScopes.set(id,handle)
      this.files.set(id, { file, handle, batchCount: 1, batchBytes: file.size, attempted: false })
      rows.push({ id, filename: file.name, size: file.size, role, phase: 'staged', submitted: false, percent: 0, error: null }); ids.push(id)
    }
    if (!ids.length) return this.intake
    this.set({ rows })
    const limits = this.state.getSnapshot().limits
    if (limits) { this.validate(ids, limits); return this.intake }
    return this.validateLater(ids)
  }
  /** Replace an unconfirmed local file with a fresh upload identity. */
  replace(id: string, file: File): Promise<void> {
    const row = this.state.getSnapshot().rows.find(row => row.id === id), item = this.files.get(id)
    if (this.disposed || !row || !item || row.submitted) return Promise.resolve()
    const { role } = row, { handle } = item
    this.remove(id)
    return this.add([file], role, handle)
  }
  /** op179: number of selected files that still wait for confirmation. */
  stagedCount(): number { return this.state.getSnapshot().rows.filter(row => !row.submitted && row.phase === 'staged').length }
  /** op179: start the pending selection exactly like 确认本批材料 (right after an in-flight limits check).
   *  Returns true when a batch is (or will be) uploading, so a send can wait for it. */
  startPendingConfirm(): boolean {
    const confirmable = () => { const rows = this.state.getSnapshot().rows.filter(row => !row.submitted); return !this.disposed && rows.length > 0 && rows.every(row => row.phase === 'staged') && !this.batch && !this.intakePending }
    if (this.intakePending > 0) { void Promise.resolve(this.intake).then(() => { if (confirmable()) void this.confirm() }); return true }
    if (confirmable()) { void this.confirm(); return true }
    return this.running || this.batch !== null
  }
  /** op179: a confirmed selection stopped on a failed or paused file and needs the user. */
  selectionStalled(): boolean { return !this.running && this.state.getSnapshot().rows.some(row => !row.sent && (row.phase === 'failed' || row.phase === 'paused')) }
  /** A selection blocks submission immediately, including asynchronous limits validation. */
  blocksSubmission(): boolean {
    if(this.running)return true
    return !this.disposed && (this.intakePending > 0 || this.batch !== null || this.state.getSnapshot().syncing || this.state.getSnapshot().rows.some(row => row.phase !== 'attached'))
  }
  /** Freeze exactly the current valid selection before the first asynchronous upload. */
  confirm(): Promise<boolean> {
    const snapshot = this.state.getSnapshot(), selected = snapshot.rows.filter(row => !row.submitted)
    if (this.disposed || this.batch || this.restorePending || this.intakePending || snapshot.syncing || !snapshot.limits || !selected.length || selected.some(row => row.phase !== 'staged')) return Promise.resolve(false)
    if (new Set(selected.map(row => this.scope(this.files.get(row.id)!.handle))).size !== 1) { this.set({ intakeError: 'PROJECT_CONTEXT_DENIED' }); return Promise.resolve(false) }
    const ids = selected.map(row => row.id)
    let cursor = 0
    for (const group of materialBatches(ids.map(id => this.files.get(id)!.file), snapshot.limits)) {
      const bytes = group.reduce((sum, file) => sum + file.size, 0)
      for (const _file of group) { const item = this.files.get(ids[cursor++]!)!; item.batchCount = group.length; item.batchBytes = bytes }
    }
    this.batch = { rows: ids }
    this.set({ rows: snapshot.rows.map(row => ids.includes(row.id) ? { ...row, submitted: true, phase: 'queued' } : row), batchActive: true, syncFailed: false, intakeError: null })
    return this.run().then(() => true)
  }
  private resolveProject(handle: string | null) {
    const key = this.scope(handle)
    let project = this.projects.get(key)
    if (!project) {
      project = this.deps.project(handle).then(value => { this.scopes.set(value.handle, key); return value }).catch(error => { this.projects.delete(key); throw error })
      this.projects.set(key, project)
    }
    return project
  }
  private async run(): Promise<void> {
    if (this.running || this.disposed || !this.batch) return
    this.running = true; this.set({ busy: true })
    try {
      const batch = this.batch, claimed = new Set<string>(), inflight = new Map<string, number>()
      await new Promise<void>(resolve => {
        const pump = () => {
          while (!this.disposed && this.batch === batch && inflight.size < MATERIAL_UPLOAD_CONCURRENCY) {
            const row = this.state.getSnapshot().rows.find(row => batch.rows.includes(row.id) && row.phase === 'queued' && !claimed.has(row.id))
            const item = row && this.files.get(row.id)
            if (!row || !item) break
            let bytes = 0
            for (const value of inflight.values()) bytes += value
            if (inflight.size && bytes + item.file.size > MATERIAL_UPLOAD_INFLIGHT_BYTES) break
            claimed.add(row.id); inflight.set(row.id, item.file.size)
            void this.uploadOne(batch, row, item).finally(() => { inflight.delete(row.id); pump() })
          }
          if (!inflight.size) resolve()
        }
        pump()
      })
      if (this.disposed || this.batch !== batch) return
      await this.sync()
    } finally {
      this.running = false
      if (!this.disposed) {
        this.set({ busy: false })
        if (this.batch && this.state.getSnapshot().rows.some(row => this.batch!.rows.includes(row.id) && row.phase === 'queued')) void this.run()
      }
    }
  }
  /** op180: one file of the running batch; never rejects. */
  private async uploadOne(batch: ConfirmedBatch, row: UploadRow, item: PrivateFile): Promise<void> {
    const abort = new AbortController()
    this.activeUploads.set(row.id, abort)
    try {
      item.project = await this.resolveProject(item.handle)
      if (this.disposed || this.batch !== batch || !batch.rows.includes(row.id) || abort.signal.aborted) throw new MaterialUploadError('UPLOAD_ABORTED')
      this.row(row.id, { phase: 'uploading', error: null, errorDetails: undefined }); item.attempted = true
      const result = await this.deps.upload(item.file, { projectHandle: item.project.handle, role: row.role, uploadId: row.id, batchCount: item.batchCount, batchBytes: item.batchBytes }, abort.signal,
        percent => { if (!this.disposed && this.batch === batch) this.row(row.id, { percent, phase: percent === 100 ? 'registering' : 'uploading' }) })
      if (this.disposed || this.batch !== batch) return
      item.ids = result.uploaded_material_ids; item.project.materialCount += item.ids.length
      this.row(row.id, { phase: 'uploaded', percent: 100, error: null, errorDetails: undefined })
    } catch (error) {
      if (!this.disposed && this.batch === batch) { const code = error instanceof MaterialUploadError ? error.code : 'UPLOAD_SERVICE_UNAVAILABLE'; this.row(row.id, { phase: code === 'UPLOAD_ABORTED' ? 'paused' : 'failed', error: code, errorDetails: error instanceof MaterialUploadError ? { status: error.status, requestId: error.requestId, stage: error.stage } : undefined }) }
    } finally { this.activeUploads.delete(row.id) }
  }
  /** Attach one complete confirmed batch; retries retain the exact notice identity. */
  async sync(): Promise<void> {
    if (this.restorePending) { await this.restoreSaved(); return }
    const batch = this.batch
    if (this.disposed || !batch || this.state.getSnapshot().syncing || !batch.rows.length) return
    const rows = batch.rows.map(id => this.state.getSnapshot().rows.find(row => row.id === id))
    if (rows.some(row => !row || row.phase !== 'uploaded')) return
    if (!batch.binding) {
      const files = batch.rows.map(id => this.files.get(id)!), project = files[0]!.project!
      if (files.some(file => !file.project || file.project.handle !== project.handle || !file.ids?.length)) { this.set({ syncFailed: true, intakeError: 'PROJECT_CONTEXT_DENIED' }); return }
      batch.project = project; batch.binding = { projectHandle: project.handle, materialIds: [...new Set(files.flatMap(file => file.ids!))], requestId: crypto.randomUUID() }
    }
    this.set({ syncing: true, syncFailed: false })
    try {
      const notice = await this.deps.bind(batch.binding)
      if (this.disposed || this.batch !== batch) return
      this.prepared.set(batch.binding.requestId, { rows: [...batch.rows], binding: batch.binding, notice, sending: false })
      this.confirmed.set(batch.binding.requestId, { rows: [...batch.rows], binding: batch.binding })
      this.set({ rows: this.state.getSnapshot().rows.map(row => batch.rows.includes(row.id) ? { ...row, phase: 'attached' } : row), batchActive: false })
      this.batch = null
      this.persist()
      // A local observer failure cannot undo the server's accepted context notice.
      try { this.deps.ready(batch.project!) } catch { /* Keep the committed batch attached. */ }
    } catch { if (!this.disposed) this.set({ syncFailed: true }) }
    finally { if (!this.disposed) this.set({ syncing: false }) }
  }
  /** Retry confirmed failures in place; unconfirmed failures return only to local staging. */
  retry(id?: string): void {
    if (this.disposed) return
    const local: string[] = []
    this.set({ rows: this.state.getSnapshot().rows.map(row => {
      if ((id && row.id !== id) || !['failed', 'paused'].includes(row.phase)) return row
      if (!row.submitted) { local.push(row.id); return row }
      return { ...row, phase: 'queued', error: null, errorDetails: undefined, percent: 0 }
    }) })
    if (local.length) {
      const limits = this.state.getSnapshot().limits
      if (limits) this.validate(local, limits)
      else void this.validateLater(local)
    }
    if (this.batch) void this.run()
  }
  /** Pause a pending or active confirmed file without attaching a partial batch. */
  pause(id: string): void {
    const row = this.state.getSnapshot().rows.find(row => row.id === id)
    if (this.disposed || !row || !['staged', 'queued', 'uploading', 'registering'].includes(row.phase)) return
    const upload = this.activeUploads.get(id)
    if (upload) upload.abort()
    else this.row(id, { phase: 'paused' })
  }
  /** Remove local or never-sent rows; accepted server materials are never deleted here. */
  remove(id: string): void {
    const row = this.state.getSnapshot().rows.find(row => row.id === id), item = this.files.get(id)
    if (this.disposed || !row || !item || row.phase === 'attached' || ['uploading', 'registering', 'uploaded'].includes(row.phase) || this.batch?.rows.includes(id) && item.attempted) return
    this.activeUploads.get(id)?.abort()
    if (this.batch) { this.batch.rows = this.batch.rows.filter(value => value !== id); if (!this.batch.rows.length) this.batch = null }
    this.files.delete(id); this.rowScopes.delete(id); this.set({ rows: this.state.getSnapshot().rows.filter(row => row.id !== id), batchActive: !!this.batch })
    if (this.batch) void this.run()
  }
  /** @param operation - Confirmed server material mutation. @returns Its outcome after releasing the queue's existing I/O ownership. */
  async manageProjectMaterials<T>(operation: () => Promise<T>): Promise<T> {
    const snapshot=this.state.getSnapshot()
    if(this.disposed||this.running||snapshot.syncing||snapshot.intaking||snapshot.rows.some(row=>row.sending))throw new MaterialUploadError('MATERIAL_IN_USE')
    this.running=true;this.set({busy:true})
    try{return await operation()}
    finally{this.running=false;if(!this.disposed){this.set({busy:false});if(this.batch)void this.run()}}
  }
  /** @param handle - Mutated project. @param materials - Remaining canonical uploaded descriptors. @returns Settlement of a replacement unsent context notice; no file upload or model call. */
  async reconcileProjectMaterials(handle:string,materials:readonly {id:string;filename:string;bytes:number;role:MaterialRole}[]):Promise<void>{
    if(this.disposed)return
    const scope=this.scope(handle),remaining=new Map(materials.map(item=>[item.id,item]))
    const retainedIds=[...new Set([...this.prepared.values()].filter(batch=>batch.binding.projectHandle===handle).flatMap(batch=>batch.binding.materialIds))].filter(id=>remaining.has(id))
    const removeRows=new Set(this.state.getSnapshot().rows.filter(row=>this.scope(this.rowScopes.get(row.id)??null)===scope&&['attached','uploaded'].includes(row.phase)).map(row=>row.id))
    for(const [key,batch]of this.prepared)if(batch.binding.projectHandle===handle)this.prepared.delete(key)
    for(const [key,batch]of this.confirmed)if(batch.binding.projectHandle===handle)this.confirmed.delete(key)
    for(const id of removeRows){this.files.delete(id);this.rowScopes.delete(id)}
    this.projects.delete(scope)
    for(const item of this.files.values())if(this.scope(item.handle)===scope)delete item.project
    const otherBatches=(this.restorePending?.batches??[]).filter(batch=>batch.binding.projectHandle!==handle)
    const rows:UploadRow[]=retainedIds.map(id=>{const item=remaining.get(id)!;return {id:crypto.randomUUID(),filename:item.filename,size:item.bytes,role:item.role,phase:'uploaded',submitted:true,percent:100,error:null}})
    if(rows.length)otherBatches.push({rows,binding:{projectHandle:handle,materialIds:retainedIds,requestId:crypto.randomUUID()}})
    this.restorePending=otherBatches.length?{batches:otherBatches}:null
    this.set({rows:[...this.state.getSnapshot().rows.filter(row=>!removeRows.has(row.id)),...rows],syncFailed:false})
    if(this.restorePending)await this.restoreSaved();else this.persist()
  }
  /** Release browser references and abort the owned stream without a late context notice. */
  dispose(): void {
    this.disposed = true; for (const upload of this.activeUploads.values()) upload.abort(); this.files.clear(); this.rowScopes.clear(); this.batch = null
    this.set({ rows: [], busy: false, batchActive: false, intaking: false, syncing: false })
  }
}

