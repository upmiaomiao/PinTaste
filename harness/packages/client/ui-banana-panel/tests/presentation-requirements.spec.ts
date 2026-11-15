// @vitest-environment node
import { afterEach, describe, expect, it } from 'vitest'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { PresentationRequirementsStore, requirementsBriefPatch, requirementsProjectionMatches } from '../src/presentation-requirements.ts'
const roots:string[]=[]
afterEach(async()=>{for(const root of roots.splice(0))await rm(root,{recursive:true,force:true})})
async function setup(){const root=await mkdtemp(join(tmpdir(),'requirements-'));roots.push(root);return {root,store:new PresentationRequirementsStore(root)}}
const options=(revision:number,id:string)=>({expected_revision:revision,request_id:id,source:'user' as const,source_message_id:'user:1'})
const handle='owned-fixture-project-handle'
const snapshot=(revision:number,style:string,confirmed=false)=>({project_id:'project-a',revision,brief:{page_count:12,language:'zh',task_mode:'create',creation_kind:'deck',style},template_selection:{confirmed,style_mode:confirmed?'template':'brief',effective_style:style,source_kind:'library',source_id:'template-1',title:'Fixture'}})
describe('one current conversation requirements record',()=>{
  it('starts unresolved and persists explicit requirements across store instances without inheriting a new upload placeholder',async()=>{
    const {store,root}=await setup();expect((await store.read('chat')).values).toMatchObject({task_mode:'auto',page_count:null,language:'auto'})
    const saved=await store.update('chat',{page_count:10,language:'en'},options(0,'same-request'));expect(saved.sources.page_count).toBe('user')
    const bound=await store.bind('chat',handle,snapshot(1,'Default'),{inherit_existing:false});expect(bound.values).toMatchObject({page_count:10,language:'en',task_mode:'auto'})
    expect(await new PresentationRequirementsStore(root).read('chat')).toEqual(bound)
  })
  it('deduplicates accepted patches and refuses stale or altered requests',async()=>{
    const {store}=await setup();await store.update('chat',{page_count:10},options(0,'r1'))
    expect((await store.update('chat',{page_count:10},options(0,'r1'))).revision).toBe(1)
    await expect(store.update('chat',{page_count:9},options(0,'r1'))).rejects.toThrow('REQUEST_ID_CONFLICT')
    await expect(store.update('chat',{page_count:9},options(0,'r2'))).rejects.toThrow('PPT_CONFIG_CONFLICT')
  })
  it('preserves new fields when a legacy client writes only changed count or language fields',async()=>{
    const {store}=await setup();await store.update('chat',{goal:'Keep evidence',page_count:10},options(0,'r1'))
    await store.saveLegacy('chat',{page_count:8,language:'zh',output_preference:'editable'},1,'legacy')
    const current=await store.read('chat');expect(current.values.goal).toBe('Keep evidence');expect(current.values.page_count).toBe(8)
    expect((await store.saveLegacy('chat',{page_count:8,language:'zh',output_preference:'editable'},1,'legacy')).revision).toBe(current.revision)
  })
  it('reads old six-field files without migrating them until an actual update occurs',async()=>{
    const {store,root}=await setup();await writeFile(join(root,'old.json'),JSON.stringify({revision:2,config:{search_mode:'off',task_mode:'create',creation_kind:'deck',page_count:6,language:'en',output_preference:'image'},request_id:'old'}))
    const current=await store.read('old');expect(current.values).toMatchObject({page_count:6,language:'en'});expect(current.sources.language).toBe('inherited')
  })
  it('adopts a genuine confirmed template, applies a later style request and rejects stale template observations',async()=>{
    const {store}=await setup();let current=await store.bind('chat',handle,snapshot(1,'Default'),{inherit_existing:false})
    await store.acknowledgeProjection('chat',current.revision,'project-a',true,1)
    current=await store.observeTemplate('chat',snapshot(2,'Follow template',true));expect(current.values.style).toBe('Follow template');expect(current.sources.style).toBe('inherited')
    await store.acknowledgeProjection('chat',current.revision,'project-a',true,2)
    current=await store.update('chat',{style:'Red'},options(current.revision,'red'))
    expect((await store.observeTemplate('chat',snapshot(2,'Follow template',true))).values.style).toBe('Red')
    expect(requirementsBriefPatch(current,snapshot(2,'Follow template',true)).style).toBe('Red')
    await store.acknowledgeProjection('chat',current.revision,'project-a',true,3)
    expect((await store.observeTemplate('chat',snapshot(2,'Follow template',true))).values.style).toBe('Red')
    current=await store.observeTemplate('chat',snapshot(4,'Follow template',true));expect(current.values.style).toBe('Follow template')
  })
  it('requires matching values as well as the marker and keeps pending state after a failed projection',async()=>{
    const {store}=await setup();let current=await store.update('chat',{page_count:10,language:'en'},options(0,'r1'));current=await store.bind('chat',handle,snapshot(1,'Default'),{inherit_existing:false})
    const wrong=snapshot(2,'Default');Object.assign(wrong.brief,{conversation_config_revision:current.revision});expect(requirementsProjectionMatches(current,wrong)).toBe(false)
    expect((await store.acknowledgeProjection('chat',current.revision,'project-a',false)).projection_status).toBe('pending')
    await expect(store.bind('chat','another-fixture-handle-long',{...wrong,project_id:'project-b'})).rejects.toThrow('PPT_PROJECT_ALREADY_BOUND')
  })
  it('does not replay an older human style over a later confirmed template but accepts a new human message',async()=>{
    const {store}=await setup();let current=await store.update('chat',{style:'Business'},options(0,'old-style'));current=await store.bind('chat',handle,snapshot(1,'Business'),{inherit_existing:false});await store.acknowledgeProjection('chat',current.revision,'project-a',true,1)
    current=await store.observeTemplate('chat',snapshot(2,'Follow template',true),'user:1');await store.acknowledgeProjection('chat',current.revision,'project-a',true,2)
    await expect(store.update('chat',{style:'Business'},options(current.revision,'replay-old-style'))).rejects.toThrow('PPT_REQUIREMENTS_STYLE_SUPERSEDED')
    current=await store.update('chat',{style:'Red'},{...options(current.revision,'new-style'),source_message_id:'user:2'});expect(current.values.style).toBe('Red')
  })
  it('retains inherited and explicit delivery intent without projecting runtime output',async()=>{
    const {store,root}=await setup();await writeFile(join(root,'old.json'),JSON.stringify({revision:1,config:{search_mode:'off',task_mode:'create',creation_kind:'deck',page_count:6,language:'zh',output_preference:'editable'},request_id:'old'}))
    let current=await store.read('old');const project=snapshot(1,'Default');Object.assign(project.brief,{output_preference:'image'})
    expect(current.values.output_preference).toBe('editable');expect(requirementsBriefPatch(current,project)).not.toHaveProperty('output_preference')
    current=await store.update('old',{output_preference:'editable'},options(current.revision,'explicit-editable'));expect(current.sources.output_preference).toBe('user');expect(requirementsBriefPatch(current,project)).not.toHaveProperty('output_preference')
  })
})
