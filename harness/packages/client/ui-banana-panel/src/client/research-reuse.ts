/** Explicit report reuse through the existing owner-checked, UI-only acceptance action. */
import { ResearchConversationClient } from './research-conversation.ts'
import type { ResearchSnapshot } from './research-conversation.ts'
type ReuseState = ResearchSnapshot['research'] & {can_reuse_existing_report?:boolean;consent_current?:boolean;ready_for_planning?:boolean;reused_for_current_scope?:boolean}
const record=(value:unknown):Record<string,unknown>|null=>value!==null&&typeof value==='object'&&!Array.isArray(value)?value as Record<string,unknown>:null
export function researchReuseAvailable(snapshot:ResearchSnapshot):boolean {
  const state=snapshot.research as ReuseState
  return state.can_reuse_existing_report===true&&state.consent_current===false
    &&typeof state.report_id==='string'&&!!state.report_id&&state.report?.partial===false
    &&Array.isArray(state.report.sources)&&state.report.sources.length>0
}
export type ResearchReuseResult='reused'|'already_ready'|'state_changed'
type Client=Pick<ResearchConversationClient,'read'|'write'>
/** No provider, search, generation or chat request is part of this operation. */
export async function reuseExistingResearch(projectId:string,signal?:AbortSignal,client:Client=new ResearchConversationClient(),request:typeof fetch=fetch):Promise<ResearchReuseResult>{
  const snapshot=await client.read(projectId,signal),state=snapshot.research as ReuseState
  signal?.throwIfAborted()
  if(state.ready_for_planning===true)return 'already_ready'
  if(!researchReuseAvailable(snapshot))return 'state_changed'
  const response=await request('/agent-api/api/projects/'+encodeURIComponent(projectId)+'/workflow',{
    credentials:'same-origin',redirect:'error',cache:'no-store',...(signal?{signal}:{}),
  })
  if(!response.ok)throw new Error('RESEARCH_REUSE_READ_FAILED')
  const raw=record(await response.json()),workflow=record(raw?.data)
  if(raw?.success!==true||workflow?.project_id!==projectId||workflow.revision!==snapshot.revision||!Array.isArray(workflow.jobs))return 'state_changed'
  if(workflow.jobs.some(value=>!['completed','failed','cancelled','interrupted'].includes(String(record(value)?.status))))return 'state_changed'
  signal?.throwIfAborted()
  const saved=await client.write({projectId,action:'accept_research',expectedRevision:snapshot.revision,
    requestId:crypto.randomUUID(),payload:{report_id:state.report_id,allow_partial:false}})
  const accepted=saved.research as ReuseState
  if(accepted.report_id!==state.report_id||accepted.ready_for_planning!==true||accepted.reused_for_current_scope!==true)throw new Error('RESEARCH_REUSE_NOT_VERIFIED')
  return 'reused'
}
