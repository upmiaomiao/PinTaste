import {describe,expect,it,vi} from 'vitest'
import {researchReuseAvailable,reuseExistingResearch} from '../src/client/research-reuse.ts'
import {ResearchConversationClient} from '../src/client/research-conversation.ts'

function snapshot(){return {project_id:'own-project',revision:8,research:{required:true,status:'choice_required',consent_current:false,ready_for_planning:false,can_reuse_existing_report:true,report_id:'original-report',profiles:{materials:{label:'M',model_calls:0,searches:0,fetches:0,token_limit:0},web:{label:'W',model_calls:2,searches:2,fetches:3,token_limit:12},deep:{label:'D',model_calls:3,searches:6,fetches:8,token_limit:30}},report:{summary:'Original summary',partial:false,sources:[{id:'S1',title:'Source',url:'https://example.test/evidence'}]}}}}
const workflow=(revision=8,jobs:unknown[]=[])=>new Response(JSON.stringify({success:true,data:{project_id:'own-project',revision,jobs}}),{status:200,headers:{'Content-Type':'application/json'}})
function setup(){
 const current:any=snapshot(),calls:any[]=[]
 const client={read:vi.fn(async()=>structuredClone(current)),write:vi.fn(async(operation:any)=>{calls.push(operation);return {project_id:'own-project',revision:9,research:{...current.research,consent_current:true,ready_for_planning:true,reused_for_current_scope:true,can_reuse_existing_report:false}}})}
 const request=vi.fn(async()=>workflow())
 return {current,calls,client,request}
}

describe('explicit no-search reuse client',()=>{
 it('only offers an intact completed report advertised by the backend',()=>expect(researchReuseAvailable(snapshot())).toBe(true))
 it.each([
  ['no eligibility',{can_reuse_existing_report:false}],['already current',{consent_current:true}],
  ['partial report',{report:{partial:true,sources:[{}]}}],['no sources',{report:{partial:false,sources:[]}}],
  ['no report ID',{report_id:null}],['no report',{report:null}],
 ])('does not offer %s',(_label,patch)=>{const s:any=snapshot();Object.assign(s.research,patch);expect(researchReuseAvailable(s)).toBe(false)})
 it('one click re-reads and posts the same report and exact current revision once',async()=>{const f=setup();expect(await reuseExistingResearch('own-project',undefined,f.client as any,f.request)).toBe('reused');expect(f.calls).toHaveLength(1);expect(f.calls[0]).toMatchObject({projectId:'own-project',action:'accept_research',expectedRevision:8,payload:{report_id:'original-report',allow_partial:false}});expect(f.calls[0].requestId).toMatch(/^[0-9a-f-]{36}$/);expect(f.request).toHaveBeenCalledWith('/agent-api/api/projects/own-project/workflow',expect.objectContaining({credentials:'same-origin',redirect:'error',cache:'no-store'}))})
 it('an already applied result makes a repeated click read-only',async()=>{const f=setup();f.current.research.ready_for_planning=true;expect(await reuseExistingResearch('own-project',undefined,f.client as any,f.request)).toBe('already_ready');expect(f.calls).toEqual([]);expect(f.request).not.toHaveBeenCalled()})
 it('a changed workflow revision cannot be acknowledged by a historical click',async()=>{const f=setup();f.request.mockResolvedValue(workflow(9));expect(await reuseExistingResearch('own-project',undefined,f.client as any,f.request)).toBe('state_changed');expect(f.calls).toEqual([])})
 it.each(['queued','running','unknown_paid_state',null])('refuses active or uncertain job status %s',async status=>{const f=setup();f.request.mockResolvedValue(workflow(8,[{status}]));expect(await reuseExistingResearch('own-project',undefined,f.client as any,f.request)).toBe('state_changed');expect(f.calls).toEqual([])})
 it('terminal historical jobs do not trigger cancellation or retries',async()=>{const f=setup();f.request.mockResolvedValue(workflow(8,[{status:'completed'},{status:'failed'},{status:'cancelled'},{status:'interrupted'}]));expect(await reuseExistingResearch('own-project',undefined,f.client as any,f.request)).toBe('reused');expect(f.calls.map(c=>c.action)).toEqual(['accept_research'])})
 it('a server change in eligibility returns without a write',async()=>{const f=setup();f.current.research.can_reuse_existing_report=false;expect(await reuseExistingResearch('own-project',undefined,f.client as any,f.request)).toBe('state_changed');expect(f.calls).toEqual([])})
 it('aborted mounted view cannot issue acceptance',async()=>{const f=setup(),control=new AbortController();control.abort();await expect(reuseExistingResearch('own-project',control.signal,f.client as any,f.request)).rejects.toThrow();expect(f.calls).toEqual([])})
 it('a lost acknowledgement is checked on the next explicit click without duplicate acceptance',async()=>{const f=setup();f.client.write.mockImplementation(async()=>{f.current.research.ready_for_planning=true;throw Error('lost acknowledgement')});await expect(reuseExistingResearch('own-project',undefined,f.client as any,f.request)).rejects.toThrow();expect(await reuseExistingResearch('own-project',undefined,f.client as any,f.request)).toBe('already_ready');expect(f.client.write).toHaveBeenCalledTimes(1)})
 it('does not claim success for an unverified response',async()=>{const f=setup();f.client.write.mockResolvedValue(snapshot() as any);await expect(reuseExistingResearch('own-project',undefined,f.client as any,f.request)).rejects.toThrow('RESEARCH_REUSE_NOT_VERIFIED')})
 it('the actual same-origin client uses the existing UI consent header, with no search or chat endpoint',async()=>{
  const calls:any[]=[]
  const request=vi.fn(async(input:any,init:any={})=>{
   calls.push({url:String(input),...init})
   if(String(input).endsWith('/workflow/research'))return new Response(JSON.stringify({success:true,data:snapshot()}),{status:200})
   if(String(input).endsWith('/workflow'))return workflow()
   if(String(input).endsWith('/workflow/action'))return new Response(JSON.stringify({success:true,data:{...snapshot(),revision:9,research:{...snapshot().research,consent_current:true,ready_for_planning:true,reused_for_current_scope:true}}}),{status:200})
   throw Error('Any model, chat, search or external request is forbidden')
  })
  expect(await reuseExistingResearch('own-project',undefined,new ResearchConversationClient(request as any),request as any)).toBe('reused')
  expect(calls).toHaveLength(3);expect(calls[2].headers['X-Wuyu-Interaction']).toBe('research-confirmation')
  expect(JSON.parse(calls[2].body)).toEqual({action:'accept_research',payload:{report_id:'original-report',allow_partial:false},expected_revision:8})
 })
})
