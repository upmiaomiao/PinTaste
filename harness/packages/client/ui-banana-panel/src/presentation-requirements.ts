/** Atomic conversation requirements; project briefs are checked execution projections. */
import { mkdir, open, rename, unlink, lstat } from 'node:fs/promises'
import { constants } from 'node:fs'
import { join, isAbsolute } from 'node:path'
import { randomUUID, createHash } from 'node:crypto'
import { defaultRequirementValues, defaultRequirementSources, parseCurrentRequirements, parsePresentationRecord, parseRequirementPatch, legacyPresentationConfig, requirementFields, type CurrentRequirements, type CurrentPresentationRecord, type RequirementValues, type RequirementField, type RequirementSource } from './presentation-config.ts'

type Receipt = { fingerprint:string; revision:number }
interface StoredRequirements { schema_version:4; current:CurrentRequirements; receipts:Record<string,Receipt>; field_revisions:Partial<Record<RequirementField,number>>; style_barrier_message_id:string|null }
/** Fixed public error codes never contain document text, handles or upstream errors. */
export class RequirementsFailure extends Error {
  constructor(readonly status:number, readonly code:string) { super(code) }
}
const record=(v:unknown):Record<string,unknown>=>v!==null&&typeof v==='object'&&!Array.isArray(v)?v as Record<string,unknown>:{}
const fingerprint=(v:unknown)=>createHash('sha256').update(JSON.stringify(v)).digest('hex')
const clone=<T>(v:T):T=>structuredClone(v)

/** Resolve one empty conversation without writing a file or selecting user requirements. */
export function emptyCurrentRequirements():CurrentRequirements {
  return {revision:0,values:defaultRequirementValues(),sources:defaultRequirementSources(),binding:null,projection_status:'unbound',source_message_id:null}
}
/** Build the only compatibility projection returned to previous clients. */
export function presentationRecord(current:CurrentRequirements):CurrentPresentationRecord {
  return {revision:current.revision,config:legacyPresentationConfig(current.values),current_requirements:clone(current)}
}
/** Each account Host owns one instance; writes are serialized per owned Session and survive restart. */
export class PresentationRequirementsStore {
  private readonly pending=new Map<string,Promise<void>>()
  constructor(readonly directory:string) { if(!isAbsolute(directory))throw new Error('Requirements require an absolute account directory') }
  private id(value:string):void { if(!/^[A-Za-z0-9_-]{1,160}$/.test(value))throw new RequirementsFailure(400,'PPT_REQUIREMENTS_INVALID') }
  private async exclusive<T>(id:string,action:()=>Promise<T>):Promise<T> {
    this.id(id);const prior=this.pending.get(id)??Promise.resolve();let result!:T
    const next=prior.then(async()=>{result=await action()});const settled=next.catch(()=>{});this.pending.set(id,settled)
    try{await next;return result}finally{if(this.pending.get(id)===settled)this.pending.delete(id)}
  }
  private async load(id:string):Promise<StoredRequirements> {
    try{const directory=await lstat(this.directory);if(!directory.isDirectory()||directory.isSymbolicLink())throw new Error('PPT_REQUIREMENTS_STORAGE_INVALID')}
    catch(error){if((error as NodeJS.ErrnoException).code==='ENOENT')return {schema_version:4,current:emptyCurrentRequirements(),receipts:{},field_revisions:{},style_barrier_message_id:null};throw error}
    let file
    try{file=await open(join(this.directory,id+'.json'),constants.O_RDONLY|constants.O_NOFOLLOW)}
    catch(error){if((error as NodeJS.ErrnoException).code==='ENOENT')return {schema_version:4,current:emptyCurrentRequirements(),receipts:{},field_revisions:{},style_barrier_message_id:null};throw error}
    try{
      if((await file.stat()).size>131072)throw new Error('PPT_REQUIREMENTS_STORAGE_INVALID')
      const value=record(JSON.parse(await file.readFile('utf8')))
      if(value.schema_version===4){
        const current=parseCurrentRequirements(value.current),receipts=record(value.receipts)
        if(Object.keys(receipts).length>64||Object.entries(receipts).some(([key,item])=>key.length>200||typeof record(item).fingerprint!=='string'||!Number.isSafeInteger(record(item).revision)))throw new Error('PPT_REQUIREMENTS_STORAGE_INVALID')
        const fieldRevisions=record(value.field_revisions)
        if(Object.entries(fieldRevisions).some(([field,revision])=>!requirementFields.includes(field as RequirementField)||!Number.isSafeInteger(revision)||Number(revision)<0))throw new Error('PPT_REQUIREMENTS_STORAGE_INVALID')
        const styleBarrier=value.style_barrier_message_id??null
        if(styleBarrier!==null&&(typeof styleBarrier!=='string'||styleBarrier.length>200))throw new Error('PPT_REQUIREMENTS_STORAGE_INVALID')
        return {schema_version:4,current,receipts:receipts as Record<string,Receipt>,field_revisions:fieldRevisions as Partial<Record<RequirementField,number>>,style_barrier_message_id:styleBarrier as string|null}
      }
      const old=parsePresentationRecord(value),values=defaultRequirementValues(),sources=defaultRequirementSources()
      for(const field of ['task_mode','creation_kind','page_count','language','search_mode','output_preference'] as const){values[field]=old.config[field] as never;sources[field]='inherited'}
      // Old unspecified counts remain unspecified; automatic output is retained until delivery unless it was explicitly saved.
      return {schema_version:4,current:{revision:old.revision,values,sources,binding:null,projection_status:'unbound',source_message_id:null},receipts:{},field_revisions:{},style_barrier_message_id:null}
    }finally{await file.close()}
  }
  private async write(id:string,value:StoredRequirements):Promise<void> {
    parseCurrentRequirements(value.current)
    await mkdir(this.directory,{recursive:true,mode:0o700});const info=await lstat(this.directory)
    if(!info.isDirectory()||info.isSymbolicLink())throw new Error('PPT_REQUIREMENTS_STORAGE_INVALID')
    const file=join(this.directory,id+'.json'),temporary=join(this.directory,randomUUID()+'.tmp'),handle=await open(temporary,'wx',0o600)
    try{await handle.writeFile(JSON.stringify(value),'utf8');await handle.sync()}finally{await handle.close()}
    try{await rename(temporary,file)}catch(error){await unlink(temporary);throw error}
  }
  /** Read a fresh authoritative record; callers receive a detached value. */
  read(id:string):Promise<CurrentRequirements>{return this.exclusive(id,async()=>clone((await this.load(id)).current))}
  /** Save an idempotent requirement patch without performing model work or granting approval. */
  update(id:string,rawPatch:unknown,options:{expected_revision:number;request_id:string;source:RequirementSource;source_message_id?:string|null;fingerprint?:string}):Promise<CurrentRequirements>{
    const patch={...parseRequirementPatch(rawPatch)}
    if(!Number.isSafeInteger(options.expected_revision)||options.expected_revision<0||typeof options.request_id!=='string'||!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/.test(options.request_id)||!['user','suggested','inherited','default'].includes(options.source))throw new RequirementsFailure(400,'PPT_REQUIREMENTS_INVALID')
    const signature=options.fingerprint??fingerprint({patch,source:options.source,source_message_id:options.source_message_id??null})
    return this.exclusive(id,async()=>{
      const stored=await this.load(id),current=stored.current,receipt=Object.hasOwn(stored.receipts,options.request_id)?stored.receipts[options.request_id]:undefined
      if(receipt){if(receipt.fingerprint!==signature)throw new RequirementsFailure(409,'REQUEST_ID_CONFLICT');return clone(current)}
      if(current.revision!==options.expected_revision)throw new RequirementsFailure(409,'PPT_CONFIG_CONFLICT')
      if('style'in patch&&patch.style!==current.values.style&&current.binding?.template&&stored.style_barrier_message_id&&options.source_message_id===stored.style_barrier_message_id)throw new RequirementsFailure(409,'PPT_REQUIREMENTS_STYLE_SUPERSEDED')
      // A one-page request uses the pages scope, including an inherited or explicitly supplied deck scope.
      // Normalize after identifying the original request so its replay keeps the same receipt fingerprint.
      let normalizedKindSource:RequirementSource|undefined
      if((patch.creation_kind??current.values.creation_kind)==='deck'&&('page_count'in patch?patch.page_count:current.values.page_count)===1){normalizedKindSource='page_count'in patch||'creation_kind'in patch?options.source:current.sources.page_count;patch.creation_kind='pages'}
      let changed=false;const changedFields:RequirementField[]=[]
      for(const field of requirementFields){if(!(field in patch))continue
        const source=field==='creation_kind'?normalizedKindSource??options.source:options.source
        if(current.values[field]!==patch[field]||current.sources[field]!==source){current.values[field]=patch[field] as never;current.sources[field]=source;changedFields.push(field);changed=true}
      }
      if('page_count'in patch&&patch.page_count!==null&&patch.page_count!==1&&current.values.creation_kind==='asset'&&!('creation_kind'in patch)){current.values.creation_kind='auto';current.sources.creation_kind=options.source;changed=true}
      if(current.values.creation_kind==='asset'&&current.values.page_count!==1){current.values.page_count=1;current.sources.page_count=options.source;changed=true}
      if(current.values.creation_kind==='pages'&&(current.values.page_count===null||current.values.page_count>8)){
        if(!('creation_kind'in patch)&&'page_count'in patch){current.values.creation_kind='auto';current.sources.creation_kind=options.source;changed=true}
        else throw new RequirementsFailure(400,'PPT_REQUIREMENTS_INVALID')
      }
      // A validated native answer can confirm unchanged values without granting work approval.
      // Persist its provenance so the pre-execution gate survives Host restart.
      if(options.source_message_id?.startsWith('question:')&&current.source_message_id!==options.source_message_id)changed=true
      if(changed){current.revision++;for(const field of changedFields)stored.field_revisions[field]=current.revision;current.source_message_id=options.source_message_id??null;current.projection_status=current.binding?'pending':'unbound'}
      stored.receipts[options.request_id]={fingerprint:signature,revision:current.revision};const keys=Object.keys(stored.receipts);for(const key of keys.slice(0,Math.max(0,keys.length-64)))delete stored.receipts[key]
      await this.write(id,stored);return clone(current)
    })
  }
  /** Bind a verified project once; observations cannot silently replace another conversation's active document. */
  bind(id:string,handle:string,snapshot:unknown,options:{expected_revision?:number;inherit_existing?:boolean}={}):Promise<CurrentRequirements>{
    const workflow=record(snapshot),brief=record(workflow.brief),projectId=workflow.project_id
    if(typeof handle!=='string'||handle.length<20||handle.length>8192||typeof projectId!=='string'||!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(projectId))throw new RequirementsFailure(403,'PROJECT_CONTEXT_DENIED')
    return this.exclusive(id,async()=>{
      const stored=await this.load(id),current=stored.current
      if(current.binding){if(current.binding.project_id!==projectId||current.binding.project_handle!==handle)throw new RequirementsFailure(409,'PPT_PROJECT_ALREADY_BOUND');return clone(current)}
      if(options.expected_revision!==undefined&&options.expected_revision!==current.revision)throw new RequirementsFailure(409,'PPT_CONFIG_CONFLICT')
      if(options.inherit_existing){
        const supported=['task_mode','creation_kind','page_count','language','topic','audience','goal','style','requirements','output_preference'] as const
        for(const field of supported){if(current.sources[field]==='user'||brief[field]===undefined)continue
          try{const accepted=parseRequirementPatch({[field]:brief[field]});current.values[field]=accepted[field] as never;current.sources[field]='inherited'}catch{/* An unsupported historical field remains unresolved; no value is invented. */}
        }
      }
      current.revision++;current.binding={project_handle:handle,project_id:projectId,applied_revision:null};current.projection_status='pending'
      await this.write(id,stored);return clone(current)
    })
  }
  /** Commit only a projection acknowledgement for the exact still-current requirements revision. */
  acknowledgeProjection(id:string,revision:number,projectId:string,applied:boolean,projectRevision?:number):Promise<CurrentRequirements>{
    return this.exclusive(id,async()=>{
      const stored=await this.load(id),current=stored.current
      if(current.revision===revision&&current.binding?.project_id===projectId){current.projection_status=applied?'applied':'pending';if(applied){current.binding.applied_revision=revision;if(projectRevision!==undefined)current.binding.project_revision=projectRevision}await this.write(id,stored)}
      return clone(current)
    })
  }
  /** Adopt only a genuine confirmed template's effective style, never over a newer unapplied style instruction. */
  observeTemplate(id:string,snapshot:unknown,sourceMessageId?:string|null):Promise<CurrentRequirements>{
    const data=record(snapshot),selection=record(data.template_selection),brief=record(data.brief)
    return this.exclusive(id,async()=>{
      const stored=await this.load(id),current=stored.current,binding=current.binding
      if(!binding||data.project_id!==binding.project_id)return clone(current)
      if(typeof data.revision!=='number'||data.revision<(binding.project_revision??0))return clone(current)
      if(selection.confirmed!==true){if(binding.template){delete binding.template;await this.write(id,stored)}return clone(current)}
      if(selection.style_mode!=='template'||typeof selection.effective_style!=='string'||selection.effective_style!==brief.style||selection.effective_style.length>2000||typeof selection.source_id!=='string'||!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(selection.source_id)||typeof selection.title!=='string'||selection.title.length>300)return clone(current)
      if((stored.field_revisions.style??-1)>(binding.applied_revision??-1))return clone(current)
      const signature=fingerprint({source_id:selection.source_id,title:selection.title,source_kind:selection.source_kind,style_mode:selection.style_mode,effective_style:selection.effective_style,signature:selection.signature??selection.selection_fingerprint??null})
      if(current.values.style!==selection.effective_style||current.sources.style!=='inherited'||binding.template?.signature!==signature){
        current.values.style=selection.effective_style;current.sources.style='inherited';binding.template={source_id:selection.source_id,title:selection.title,signature};stored.style_barrier_message_id=sourceMessageId??current.source_message_id;current.revision++;stored.field_revisions.style=current.revision;current.projection_status='pending';await this.write(id,stored)
      }
      return clone(current)
    })
  }
  /** Return fields changed since the last accepted execution projection. */
  pendingFields(id:string):Promise<RequirementField[]>{return this.exclusive(id,async()=>{const value=await this.load(id);return requirementFields.filter(field=>(value.field_revisions[field]??-1)>(value.current.binding?.applied_revision??-1))})}
  /** Apply only changed legacy fields; new requirements and provenance are never cleared by an old client. */
  async saveLegacy(id:string,rawConfig:unknown,expectedRevision:number,requestId:string):Promise<CurrentRequirements>{
    const current=await this.read(id),config=parsePresentationRecord({revision:0,config:rawConfig}).config,old=legacyPresentationConfig(current.values),supplied=record(rawConfig),patch:Partial<RequirementValues>={}
    for(const field of ['task_mode','creation_kind','page_count','language','search_mode','output_preference'] as const)if(field in supplied&&config[field]!==old[field])patch[field]=config[field] as never
    return this.update(id,patch,{expected_revision:expectedRevision,request_id:requestId,source:'user',fingerprint:fingerprint({legacy:Object.fromEntries(Object.keys(supplied).sort().map(field=>[field,supplied[field]]))})})
  }
}

/** Exact, non-authorizing content projection. Output remains a delivery preference; runtime image-first delivery never changes that intent. */
export function requirementsBriefPatch(current:CurrentRequirements,snapshot:unknown,_forceFields:readonly RequirementField[]=[]):Record<string,unknown>{
  const data=record(snapshot),brief=record(data.brief),patch:Record<string,unknown>={conversation_config_revision:current.revision}
  for(const field of ['task_mode','creation_kind','page_count','language','topic','audience','goal','style','requirements'] as const){
    const value=current.values[field]
    if(value===null||value==='auto'||value==='')continue
    if(brief[field]!==value)patch[field]=value
  }
  if(current.values.creation_kind==='auto'&&current.values.page_count!==null){
    const desired=current.values.page_count===1?'pages':current.values.page_count>8||brief.creation_kind==='asset'?'deck':brief.creation_kind
    if(desired&&desired!==brief.creation_kind)patch.creation_kind=desired
  }
  return patch
}
/** Verify values and the revision marker together; the marker alone never proves a matching execution input. */
export function requirementsProjectionMatches(current:CurrentRequirements,snapshot:unknown):boolean{
  const patch=requirementsBriefPatch(current,snapshot),brief=record(record(snapshot).brief)
  return Object.keys(patch).length===1&&brief.conversation_config_revision===current.revision
}
