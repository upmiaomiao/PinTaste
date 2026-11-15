import {afterEach,describe,expect,it} from 'vitest'
import {mkdtemp,readFile,rm,writeFile} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {PresentationRequirementsStore,requirementsBriefPatch,requirementsProjectionMatches} from '../src/presentation-requirements.ts'
const roots:string[]=[]
afterEach(async()=>{await Promise.all(roots.splice(0).map(root=>rm(root,{recursive:true,force:true})))})
async function setup(){const root=await mkdtemp(join(tmpdir(),'requirements-output81-'));roots.push(root);return {root,store:new PresentationRequirementsStore(root)}}
const options=(revision:number,id:string)=>({expected_revision:revision,request_id:id,source:'user' as const,source_message_id:'user:2'})
const snapshot=()=>({project_id:'self-project',revision:1,brief:{task_mode:'create',creation_kind:'pages',page_count:6,language:'zh',style:'Default',output_preference:'image'},template_selection:{confirmed:false}})
describe('output delivery intent is distinct from the content projection',()=>{
 it('preserves explicit editable intent and provenance without setting runtime output',async()=>{
  const {root,store}=await setup();let current=await store.update('self',{page_count:6,language:'zh',creation_kind:'pages',output_preference:'editable'},options(0,'explicit-delivery'))
  const actual=snapshot();current=await store.bind('self','self-owned-project-handle-long',actual,{inherit_existing:false});Object.assign(actual.brief,{conversation_config_revision:current.revision})
  expect(current.values.output_preference).toBe('editable');expect(current.sources.output_preference).toBe('user')
  expect(requirementsBriefPatch(current,actual)).toEqual({conversation_config_revision:current.revision})
  expect(requirementsProjectionMatches(current,actual)).toBe(true)
  expect((await new PresentationRequirementsStore(root).read('self')).values.output_preference).toBe('editable')
 })
 it('keeps legacy output readback while never projecting inherited or explicit output',async()=>{
  const {root,store}=await setup();await writeFile(join(root,'legacy.json'),JSON.stringify({revision:1,config:{task_mode:'create',creation_kind:'pages',page_count:6,language:'zh',output_preference:'editable',search_mode:'off'},request_id:'legacy'}))
  let current=await store.read('legacy');expect(current.values.output_preference).toBe('editable');expect(current.sources.output_preference).toBe('inherited')
  expect(requirementsBriefPatch(current,snapshot())).not.toHaveProperty('output_preference')
  current=await store.update('legacy',{output_preference:'editable'},options(current.revision,'explicit-after-legacy'))
  expect(current.sources.output_preference).toBe('user');expect(requirementsBriefPatch(current,snapshot())).not.toHaveProperty('output_preference')
  expect(JSON.parse(await readFile(join(root,'legacy.json'),'utf8')).current.values.output_preference).toBe('editable')
 })
 it('still rejects real content differences and a stale marker',async()=>{
  const {store}=await setup();let current=await store.update('self',{page_count:6,language:'zh',topic:'Own text',output_preference:'editable'},options(0,'content'))
  const actual=snapshot();current=await store.bind('self','self-owned-project-handle-long',actual,{inherit_existing:false});Object.assign(actual.brief,{topic:'Own text',conversation_config_revision:current.revision})
  expect(requirementsProjectionMatches(current,actual)).toBe(true)
  for(const patch of [{page_count:7},{language:'en'},{topic:'Different text'},{conversation_config_revision:current.revision-1}]){
   expect(requirementsProjectionMatches(current,{...actual,brief:{...actual.brief,...patch}})).toBe(false)
  }
 })
})
