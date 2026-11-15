/** Conversation preference drafts and server acknowledgements, independent of project execution. */
import { createSnapshotStore } from '@deepseek-ai/dsh-client-store'
import { defaultPresentationConfig, samePresentationConfig, type PresentationConfig, type CurrentPresentationRecord, type CurrentRequirements, parseCurrentPresentationRecord } from '../presentation-config.ts'
import type { BananaPanelKey } from './locales.ts'

type Status = 'loading' | 'ready' | 'dirty' | 'saving' | 'error' | 'conflict'
export interface PreferencesSnapshot {
  status: Status
  revision: number
  boundTaskMode: PresentationConfig['task_mode'] | null
  searchMode: PresentationConfig['search_mode']
  taskMode: PresentationConfig['task_mode']
  creationKind: PresentationConfig['creation_kind']
  countDraft: string
  fixed: boolean
  output: PresentationConfig['output_preference']
  language: PresentationConfig['language']
  currentRequirements: CurrentRequirements | null
}
type Transport = (request: Record<string, unknown>) => Promise<CurrentPresentationRecord>
type Translate = (key: BananaPanelKey, params?: Record<string, string>) => string

function chineseCount(text: string): number {
  if (/^[1-9]\d?$/.test(text)) return Number(text)
  const digits = '零一二三四五六七八九'
  if (text === '两') return 2
  if (text.includes('十')) { const [tens, ones] = text.split('十'); return (tens ? digits.indexOf(tens) : 1) * 10 + (ones ? digits.indexOf(ones) : 0) }
  return digits.indexOf(text)
}

/** Recognize only a single explicit, unquoted preference instruction; questions and examples stay ordinary text. */
export function spokenPreferences(text: string): Partial<PresentationConfig> {
  const lines = text.split('\n').filter(line => !/^\s*(>|```|【|\[)/.test(line))
  const plain = lines.join('\n')
  if (/[？?]|(?:比如|例如|假如|如果|不要|不是|还是)|[“”「」]/.test(plain)) return {}
  const patch: Partial<PresentationConfig> = {}
  const numbers = [...plain.matchAll(/(?:改成|改为|设为|设置为|生成|制作|做)\s*(?:一份|一套)?\s*([1-9]\d*|[一二两三四五六七八九十]{1,3})\s*页/g)].map(m => chineseCount(m[1]!))
  if (numbers.length === 1) {
    const count = numbers[0]!
    if (!Number.isInteger(count) || count < 1 || count > 80) throw new Error('PPT_PAGE_COUNT_INVALID')
    patch.page_count = count
  }
  if (/(?:页数|页数设置)(?:改成|改为|设为|设置为)?\s*(?:按需求|自动)/.test(plain)) patch.page_count = null
  // Explicit output requests remain ordinary chat instructions, never saved defaults.
  const english = /(?:内容语言|PPT语言|改成|改为|使用|用)\s*英文/.test(plain)
  const chinese = /(?:内容语言|PPT语言|改成|改为|使用|用)\s*中文/.test(plain)
  if (english !== chinese) patch.language = english ? 'en' : 'zh'
  return patch
}

/** POST through the account Router; redirects and failed saves never count as acknowledgement. */
export function presentationPreferencesTransport(sessionId: string): Transport {
  return async request => {
    const response = await fetch('/api/wuyu/presentation-settings', { method: 'POST', credentials: 'same-origin', redirect: 'error', cache: 'no-store',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...request, session_id: sessionId, config_version: 4 }), signal: AbortSignal.timeout(15000) })
    if (response.status === 409) throw new Error('PPT_CONFIG_CONFLICT')
    if (!response.ok) throw new Error('PPT_CONFIG_UNAVAILABLE')
    return parseCurrentPresentationRecord(await response.json())
  }
}

/** One scoped controller; saved preferences are only published after the Host acknowledges them. */
export class PresentationPreferencesController {
  readonly state = createSnapshotStore<PreferencesSnapshot>({ status:'loading', revision:0, boundTaskMode:null, searchMode:'off', taskMode:'create', creationKind:'deck', fixed:false, countDraft:'12', output:'editable', language:'zh', currentRequirements:null })
  private saved:PresentationConfig=defaultPresentationConfig()
  private disposed=false
  private version=0
  private binding:Promise<void>|undefined
  private requestId:string|undefined
  constructor(private readonly transport:Transport){}
  private accept(record:CurrentPresentationRecord):void{
    this.saved=record.config;this.requestId=undefined
    this.state.update(s=>{s.searchMode=record.config.search_mode;s.taskMode=record.config.task_mode;s.creationKind=record.config.creation_kind;s.revision=record.revision;s.fixed=record.config.page_count!==null;s.countDraft=String(record.config.page_count??12);s.output=record.config.output_preference;s.language=record.config.language;s.currentRequirements=record.current_requirements;s.boundTaskMode=record.current_requirements.binding?record.config.task_mode:null;s.status='ready'})
  }
  /** Resolve an observed project through the owned Host before using its conversation binding. */
  bindProject(projectHandle:string,_mode?:PresentationConfig['task_mode'],inheritExisting=true,observe=false):Promise<void>{
    if(!observe&&this.state.getSnapshot().currentRequirements?.binding?.project_handle===projectHandle)return Promise.resolve()
    const operation=(this.binding??Promise.resolve()).catch(()=>{}).then(async()=>{
      if(this.disposed)return
      if(this.state.getSnapshot().status!=='ready')await this.load()
      const requestId=crypto.randomUUID()
      for(let attempt=0;attempt<2;attempt++){
        const version=++this.version
        try{const record=await this.transport({operation:'bind',project_handle:projectHandle,inherit_existing:inheritExisting,expected_revision:this.state.getSnapshot().revision,request_id:requestId});if(!this.disposed&&version===this.version)this.accept(record);return}
        catch(error){if(attempt===0&&error instanceof Error&&error.message==='PPT_CONFIG_CONFLICT'){await this.load();continue}throw error}
      }
    })
    this.binding=operation;return operation
  }
  /** Existing material preparation can read this projection; it is not a saved user choice. */
  effectiveTaskMode():PresentationConfig['task_mode']{return this.state.getSnapshot().boundTaskMode??this.state.getSnapshot().taskMode}
  /** Explicitly reload a failed/conflicting read without sending a user message. */
  async load():Promise<void>{
    const version=++this.version;this.state.update(s=>{s.status='loading'})
    try{const record=await this.transport({operation:'read'});if(!this.disposed&&version===this.version)this.accept(record)}
    catch(error){if(!this.disposed&&version===this.version)this.state.update(s=>{s.status='error'});throw error}
  }
  /** Refresh a displayed readback without replacing local edits or an acknowledged newer save. */
  async refresh():Promise<void>{
    if(this.disposed||this.state.getSnapshot().status!=='ready')return
    const version=this.version
    try{const record=await this.transport({operation:'read'});if(!this.disposed&&version===this.version&&this.state.getSnapshot().status==='ready'&&record.revision>=this.state.getSnapshot().revision)this.accept(record)}
    catch{/* A transient readback failure preserves the last acknowledged requirements. */}
  }
  /** Retain explicit legacy UI edits; conversation text is interpreted only after message acceptance on the Host. */
  edit(patch:Partial<Pick<PreferencesSnapshot,'searchMode'|'taskMode'|'creationKind'|'fixed'|'countDraft'|'output'|'language'>>):void{
    this.version++;this.requestId=undefined;this.state.update(s=>{Object.assign(s,patch);s.status='dirty'})
  }
  /** Return the old bounded projection for existing upload and search consumers. */
  selected():PresentationConfig{
    const s=this.state.getSnapshot()
    if(s.fixed&&(!/^[1-9]\d?$/.test(s.countDraft)||Number(s.countDraft)>80))throw new Error('PPT_PAGE_COUNT_INVALID')
    const count=s.creationKind==='asset'?1:s.fixed?Number(s.countDraft):null
    if(s.creationKind==='pages'&&(count===null||count>8))throw new Error('PPT_PAGE_COUNT_INVALID')
    return{search_mode:s.searchMode,task_mode:s.taskMode,creation_kind:s.creationKind==='deck'&&count===1?'pages':s.creationKind,page_count:count,output_preference:s.output,language:s.language}
  }
  /** A direct search-button click changes only the canonical preference, never an execution permit. */
  async toggleSearch(mode:'web'|'deep'):Promise<void>{
    const s=this.state.getSnapshot();if(s.status!=='ready')throw new Error('PPT_CONFIG_NOT_READY')
    const version=++this.version;this.state.update(v=>{v.status='saving'})
    try{const record=await this.transport({operation:'patch',patch:{search_mode:s.searchMode===mode?'off':mode},expected_revision:s.revision,request_id:crypto.randomUUID()});if(!this.disposed&&version===this.version)this.accept(record)}
    catch(error){if(!this.disposed&&version===this.version)this.state.update(v=>{v.status=error instanceof Error&&error.message==='PPT_CONFIG_CONFLICT'?'conflict':'error'});throw error}
  }
  /** Compatibility for already-open older controls; new entry points use conversation requirements. */
  async chooseTask(taskMode:PresentationConfig['task_mode'],creationKind=this.state.getSnapshot().creationKind):Promise<void>{this.edit({taskMode,creationKind,...(creationKind==='asset'?{fixed:true,countDraft:'1'}:creationKind==='pages'?{fixed:true,countDraft:'2'}:{fixed:false})});await this.save()}
  /** Persist an explicit legacy control edit; ordinary messages never call this to infer preferences. */
  async save():Promise<void>{
    const s=this.state.getSnapshot();if(s.status==='loading')await this.load()
    if(this.state.getSnapshot().status==='ready'&&samePresentationConfig(this.selected(),this.saved))return
    if(['saving','conflict','error'].includes(this.state.getSnapshot().status))throw new Error('PPT_CONFIG_NOT_READY')
    const version=++this.version,config=this.selected();this.requestId??=crypto.randomUUID();this.state.update(v=>{v.status='saving'})
    try{const record=await this.transport({operation:'save',config,expected_revision:this.state.getSnapshot().revision,request_id:this.requestId});if(!this.disposed&&version===this.version)this.accept(record)}
    catch(error){if(!this.disposed&&version===this.version)this.state.update(v=>{v.status=error instanceof Error&&error.message==='PPT_CONFIG_CONFLICT'?'conflict':'error'});throw error}
  }
  /** Preserve accepted conversation text verbatim; the scoped Host tools maintain its current requirements. */
  async submission(text:string,_t:Translate):Promise<string>{return text}
  /** Ignore late readbacks when this Session view is released. */
  dispose():void{this.disposed=true;this.version++}
}
