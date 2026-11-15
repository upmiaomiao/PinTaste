// @vitest-environment node
import { afterEach, describe, expect, it } from 'vitest'
import { createServer, type Server } from 'node:http'
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PresentationRequirementsStore, requirementsBriefPatch, requirementsProjectionMatches } from '../src/presentation-requirements.ts'
import { createPresentationConfigHandler } from '../src/presentation-config-http.ts'
const roots:string[]=[],servers:Server[]=[]
afterEach(async()=>{for(const server of servers.splice(0))await new Promise<void>(resolve=>server.close(()=>resolve()));for(const root of roots.splice(0))await rm(root,{recursive:true,force:true})})
const options=(revision:number,id:string,source:'user'|'suggested'|'inherited'='user')=>({expected_revision:revision,request_id:id,source,source_message_id:'user:1'})
const legacy=(kind='deck',count:number|null=6)=>({revision:2,config:{search_mode:'off',task_mode:'create',creation_kind:kind,page_count:count,language:'zh',output_preference:'editable'},request_id:'legacy'})
async function setup(){const root=await mkdtemp(join(tmpdir(),'requirements-scope-'));roots.push(root);return {root,store:new PresentationRequirementsStore(root)}}
describe('page-count and creation-scope atomic updates',()=>{
  it('normalizes explicit deck plus one page without changing the original replay input',async()=>{
    const {root,store}=await setup(),patch={creation_kind:'deck',page_count:1} as const
    const value=await store.update('chat',patch,options(0,'single-page'))
    expect(value.values).toMatchObject({creation_kind:'pages',page_count:1})
    expect(value.sources).toMatchObject({creation_kind:'user',page_count:'user'})
    expect(patch).toEqual({creation_kind:'deck',page_count:1})
    const bytes=await readFile(join(root,'chat.json'),'utf8')
    expect((await store.update('chat',patch,options(0,'single-page'))).revision).toBe(value.revision)
    expect(await readFile(join(root,'chat.json'),'utf8')).toBe(bytes)
    expect(JSON.parse(bytes).field_revisions).toMatchObject({creation_kind:value.revision,page_count:value.revision})
    await expect(store.update('chat',{creation_kind:'pages',page_count:1},options(0,'single-page'))).rejects.toThrow('REQUEST_ID_CONFLICT')
  })
  it('changes an inherited old deck to pages with the current count instruction provenance',async()=>{
    const {root,store}=await setup();await writeFile(join(root,'chat.json'),JSON.stringify(legacy()))
    const value=await store.update('chat',{page_count:1},options(2,'one-page','suggested'))
    expect(value.values).toMatchObject({creation_kind:'pages',page_count:1,language:'zh',output_preference:'editable'})
    expect(value.sources.creation_kind).toBe('suggested');expect(value.sources.page_count).toBe('suggested');expect(value.sources.language).toBe('inherited')
    expect((await new PresentationRequirementsStore(root).read('chat')).values).toEqual(value.values)
  })
  it('repairs an existing saved deck-one combination before acknowledging a new empty update',async()=>{
    const {root,store}=await setup();await store.update('chat',{creation_kind:'deck',page_count:6},options(0,'seed'))
    const file=join(root,'chat.json'),old=JSON.parse(await readFile(file,'utf8'))
    old.current.revision=2;old.current.values.page_count=1;old.current.sources.page_count='inherited';old.current.sources.creation_kind='inherited'
    await writeFile(file,JSON.stringify(old))
    const value=await store.update('chat',{},options(2,'repair-existing'))
    expect(value.values).toMatchObject({creation_kind:'pages',page_count:1});expect(value.revision).toBe(3)
    expect(value.sources.page_count).toBe('inherited');expect(value.sources.creation_kind).toBe('inherited')
  })
  it('rejects an invalid pages combination without changing any owner or receipt bytes',async()=>{
    const {root,store}=await setup();await store.update('chat',{creation_kind:'deck',page_count:6},options(0,'seed'))
    const before=await readFile(join(root,'chat.json'),'utf8')
    for(const page_count of [null,9])await expect(store.update('chat',{creation_kind:'pages',page_count},options(1,'invalid-pages-'+String(page_count)))).rejects.toThrow('PPT_REQUIREMENTS_INVALID')
    expect(await readFile(join(root,'chat.json'),'utf8')).toBe(before)
    expect(await readdir(root)).toEqual(['chat.json'])
    await expect(store.update('new',{creation_kind:'pages',page_count:9},options(0,'invalid-new'))).rejects.toThrow('PPT_REQUIREMENTS_INVALID')
    expect(await readdir(root)).toEqual(['chat.json'])
  })
  it('keeps valid deck, pages, asset and existing automatic scope transitions',async()=>{
    const {store}=await setup()
    for(const [id,kind,count]of [['deck','deck',2],['pages','pages',8],['asset','asset',1]]as const){const value=await store.update(id,{creation_kind:kind,page_count:count},options(0,'seed'));expect(value.values).toMatchObject({creation_kind:kind,page_count:count})}
    expect((await store.update('asset',{page_count:2},options(1,'grow-asset'))).values).toMatchObject({creation_kind:'auto',page_count:2})
    expect((await store.update('pages',{page_count:9},options(1,'grow-pages'))).values).toMatchObject({creation_kind:'auto',page_count:9})
    expect((await store.update('asset-reset',{creation_kind:'asset',page_count:8},options(0,'choose-asset'))).values).toMatchObject({creation_kind:'asset',page_count:1})
  })
  it('preserves CAS and tracks the derived type among pending fields for a bound project',async()=>{
    const {store}=await setup();let value=await store.update('chat',{creation_kind:'deck',page_count:6},options(0,'seed'))
    value=await store.bind('chat','owned-project-handle-long',{project_id:'owned-project',revision:4,brief:{creation_kind:'deck',page_count:6}},{inherit_existing:false})
    value=await store.acknowledgeProjection('chat',value.revision,'owned-project',true,4)
    await expect(store.update('chat',{page_count:1},options(value.revision-1,'stale'))).rejects.toThrow('PPT_CONFIG_CONFLICT')
    const changed=await store.update('chat',{page_count:1},options(value.revision,'one-page'))
    expect(changed.projection_status).toBe('pending');expect(await store.pendingFields('chat')).toEqual(['creation_kind','page_count'])
    const snapshot={project_id:'owned-project',revision:4,brief:{creation_kind:'deck',page_count:6}}
    const patch=requirementsBriefPatch(changed,snapshot)
    expect(patch).toMatchObject({creation_kind:'pages',page_count:1,conversation_config_revision:changed.revision})
    Object.assign(snapshot.brief,patch);expect(requirementsProjectionMatches(changed,snapshot)).toBe(true)
  })
  it('saves legacy HTTP one-page requests, survives readback and projects an executable pages scope',async()=>{
    const {root,store}=await setup();await writeFile(join(root,'chat.json'),JSON.stringify(legacy()))
    const server=createServer(createPresentationConfigHandler({directory:root,requirements:store,reject:request=>request.headers.authorization==='fixture'?undefined:401,ownsSession:async id=>id==='chat'}));servers.push(server)
    await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve));const address=server.address();if(!address||typeof address==='string')throw Error('Missing fixture port')
    const send=(body:unknown)=>fetch('http://127.0.0.1:'+address.port,{method:'POST',headers:{Authorization:'fixture','Content-Type':'application/json'},body:JSON.stringify(body)})
    const request={session_id:'chat',config_version:3,operation:'save',config:{search_mode:'off',task_mode:'create',creation_kind:'deck',page_count:1,language:'zh',output_preference:'editable'},expected_revision:2,request_id:'00000000-0000-4000-8000-000000000001'}
    const saved=await send(request);expect(saved.status).toBe(200);expect((await saved.json()).config).toMatchObject({creation_kind:'pages',page_count:1})
    const bytes=await readFile(join(root,'chat.json'),'utf8');expect((await send(request)).status).toBe(200);expect(await readFile(join(root,'chat.json'),'utf8')).toBe(bytes)
    const readback=await(await send({session_id:'chat',config_version:4,operation:'read'})).json()
    expect(readback.current_requirements.values).toMatchObject({creation_kind:'pages',page_count:1})
    const project={brief:{creation_kind:'deck',page_count:6,task_mode:'create',language:'zh'}}
    Object.assign(project.brief,requirementsBriefPatch(readback.current_requirements,project))
    expect(project.brief).toMatchObject({creation_kind:'pages',page_count:1});expect(requirementsProjectionMatches(readback.current_requirements,project)).toBe(true)
  })
})
