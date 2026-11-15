import {describe,expect,it,vi} from 'vitest'
import {recoverWithExistingMaterials,hasResearchRecovery} from '../src/client/research-recovery.ts'
import {ResearchConversationClient} from '../src/client/research-conversation.ts'

const profile={label:'Synthetic',model_calls:0,searches:0,fetches:0,token_limit:0}
function snapshot(){return {project_id:'self-170',revision:8,research:{required:true,status:'failed',mode:'deep',prompt:'Self-created public question',report_id:'retained-report',consent_current:true,can_use_existing_materials:true,profiles:{materials:profile,web:profile,deep:profile},report:{partial:true,summary:'',findings:[],sources:[]},partial_sources:Array.from({length:25},(_,n)=>({id:'S'+n,title:'Synthetic',url:'https://example.invalid/'+n}))}}}
function setup(){
 const current:any=snapshot(),calls:any[]=[]
 const client={read:vi.fn(async()=>structuredClone(current)),write:vi.fn(async(op:any)=>{calls.push(op);return {...current,revision:9,research:{...current.research,materials_only_current:true}}})}
 const request=vi.fn(async()=>new Response(JSON.stringify({success:true,data:{project_id:'self-170',revision:8,epoch:2,jobs:[]}}),{status:200}))
 return {current,calls,client,request}
}
describe('evidence-preserving material scope recovery',()=>{
 it('only a project-bound trusted workflow result advertises the control',()=>{
  const text=JSON.stringify({project_id:'self-170',project_handle:'self-handle',conversation:{recovery:{kind:'research',use_existing_materials:true,keep_project:true}}})
  expect(hasResearchRecovery('mcp__banana__get_presentation_workflow',[text],'self-170','self-handle')).toBe(true)
  expect(hasResearchRecovery('mcp__banana__search_presentation_web',[text],'self-170','self-handle')).toBe(false)
  expect(hasResearchRecovery('mcp__banana__get_presentation_workflow',[text],'another','self-handle')).toBe(false)
 })
 it.each(['failed','paused'])('real click %s/25 sources keeps the exact revision/report and uses the local material contract',async status=>{
  const f=setup();f.current.research.status=status
  expect(await recoverWithExistingMaterials('self-170',undefined,f.client as any,f.request)).toBe('recovered')
  expect(f.calls).toHaveLength(1);expect(f.calls[0]).toMatchObject({action:'confirm_research',expectedRevision:8,payload:{mode:'materials',prompt:f.current.research.prompt,acknowledge_cost:false,preserve_research:true,report_id:'retained-report'}})
 })
 it.each([['legacy projection',{can_use_existing_materials:undefined}],['complete',{status:'accepted',can_use_existing_materials:false}],['review',{status:'review_required',can_use_existing_materials:false}],['busy',{status:'running',can_use_existing_materials:false}],['scope stale',{consent_current:false}],['missing report id',{report_id:null}]])('rejects %s before writing',async(_label,patch)=>{
  const f=setup();Object.assign(f.current.research,patch)
  expect(await recoverWithExistingMaterials('self-170',undefined,f.client as any,f.request)).toBe('state_changed');expect(f.calls).toEqual([])
 })
 it.each(['running','queued','unknown_paid_state',null])('rejects any active or uncertain job %s even from an old epoch',async status=>{
  const f=setup();f.request.mockResolvedValue(new Response(JSON.stringify({success:true,data:{project_id:'self-170',revision:8,epoch:2,jobs:[{status,input_epoch:0}]}})))
  expect(await recoverWithExistingMaterials('self-170',undefined,f.client as any,f.request)).toBe('state_changed');expect(f.calls).toEqual([])
 })
 it('cancelled mounted view cannot write',async()=>{
  const f=setup(),control=new AbortController();control.abort()
  await expect(recoverWithExistingMaterials('self-170',control.signal,f.client as any,f.request)).rejects.toThrow();expect(f.calls).toEqual([])
 })
 it('revision race or mismatched server response cannot claim recovery',async()=>{
  const f=setup();f.request.mockResolvedValue(new Response(JSON.stringify({success:true,data:{project_id:'self-170',revision:9,epoch:2,jobs:[]}})))
  expect(await recoverWithExistingMaterials('self-170',undefined,f.client as any,f.request)).toBe('state_changed');expect(f.calls).toEqual([])
  const other=setup();other.client.write.mockResolvedValue(snapshot() as any)
  expect(await recoverWithExistingMaterials('self-170',undefined,other.client as any,other.request)).toBe('state_changed')
 })
 it('uncertain result is checked on the next explicit click without duplicate write',async()=>{
  const f=setup();f.client.write.mockImplementation(async()=>{f.current.research.materials_only_current=true;throw Error('lost receipt')})
  await expect(recoverWithExistingMaterials('self-170',undefined,f.client as any,f.request)).rejects.toThrow()
  expect(await recoverWithExistingMaterials('self-170',undefined,f.client as any,f.request)).toBe('already_ready');expect(f.client.write).toHaveBeenCalledTimes(1)
 })
 it('actual same-origin client supplies the trusted UI header; no chat/search/generation endpoint',async()=>{
  const current:any=snapshot(),calls:any[]=[]
  const request:any=vi.fn(async(input:any,init:any={})=>{
   calls.push({url:String(input),...init});const data=String(input).endsWith('/workflow')?{project_id:'self-170',revision:8,epoch:2,jobs:[]}:String(input).endsWith('/workflow/action')?{...current,revision:9,research:{...current.research,materials_only_current:true}}:current
   if(!String(input).match(/\/workflow(?:\/research|\/action)?$/))throw Error('External/paid endpoint forbidden')
   return new Response(JSON.stringify({success:true,data}),{status:200})
  })
  expect(await recoverWithExistingMaterials('self-170',undefined,new ResearchConversationClient(request),request)).toBe('recovered')
  expect(calls).toHaveLength(3);expect(calls[2].headers['X-Wuyu-Interaction']).toBe('research-confirmation')
 })
})
