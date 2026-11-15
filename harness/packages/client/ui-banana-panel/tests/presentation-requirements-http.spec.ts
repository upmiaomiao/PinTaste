// @vitest-environment node
import { afterEach, describe, expect, it } from 'vitest'
import { createServer, type Server } from 'node:http'
import { mkdtemp, rm, readdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PresentationRequirementsStore } from '../src/presentation-requirements.ts'
import { createPresentationConfigHandler } from '../src/presentation-config-http.ts'
const roots:string[]=[],servers:Server[]=[]
afterEach(async()=>{for(const server of servers.splice(0))await new Promise<void>(resolve=>server.close(()=>resolve()));for(const root of roots.splice(0))await rm(root,{recursive:true,force:true})})
async function setup(){
  const directory=await mkdtemp(join(tmpdir(),'requirements-http-'));roots.push(directory);const store=new PresentationRequirementsStore(directory)
  const server=createServer(createPresentationConfigHandler({directory,requirements:store,reject:request=>request.headers.authorization==='fixture'?undefined:401,ownsSession:async id=>['chat-a','chat-b'].includes(id),bindProject:async(id,handle,options)=>{if(handle!=='owned-project-handle-long')throw Error('Denied fixture handle');return store.bind(id,handle,{project_id:'owned-project',brief:{page_count:12,language:'zh'}},options)}}));servers.push(server);await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve));const address=server.address();if(!address||typeof address==='string')throw Error('Missing fixture port')
  return {store,directory,send:(body:unknown,authorization='fixture')=>fetch('http://127.0.0.1:'+address.port,{method:'POST',headers:{Authorization:authorization,'Content-Type':'application/json'},body:JSON.stringify(body)})}
}
const id='00000000-0000-4000-8000-000000000001'
describe('version-four requirements HTTP alongside previous clients',()=>{
  it('exposes the same unresolved current record and derives old response fields',async()=>{
    const {send}=await setup();const current=await(await send({session_id:'chat-a',operation:'read',config_version:4})).json();expect(current.current_requirements.values).toMatchObject({task_mode:'auto',page_count:null,language:'auto'})
    expect(await(await send({session_id:'chat-a',operation:'read'})).json()).toEqual({revision:0,config:{page_count:null,language:'zh',output_preference:'editable'}})
    expect(Object.keys((await(await send({session_id:'chat-a',operation:'read',config_version:3})).json()).config)).toHaveLength(6)
  })
  it('accepts only explicit search UI patches, retaining other requirements and session isolation',async()=>{
    const {send,store}=await setup();await store.update('chat-a',{page_count:10,language:'en',goal:'Retain verified facts'},{expected_revision:0,request_id:'seed',source:'user',source_message_id:'user:1'})
    const request={session_id:'chat-a',operation:'patch',config_version:4,patch:{search_mode:'web'},expected_revision:1,request_id:id}
    expect((await send(request)).status).toBe(200);expect((await send(request)).status).toBe(200)
    const value=(await(await send({session_id:'chat-a',operation:'read',config_version:4})).json()).current_requirements;expect(value.values).toMatchObject({page_count:10,language:'en',goal:'Retain verified facts',search_mode:'web'})
    expect((await send({...request,patch:{page_count:4}})).status).toBe(400)
    expect((await send({...request,patch:{search_mode:'web',confirmed:true}})).status).toBe(400)
    expect((await(await send({session_id:'chat-b',operation:'read',config_version:4})).json()).current_requirements.values.page_count).toBeNull()
  })
  it('binds only after ownership resolution and does not accept a claimed project id or approvals',async()=>{
    const {send}=await setup();const bind={session_id:'chat-a',operation:'bind',config_version:4,project_handle:'owned-project-handle-long',inherit_existing:false,expected_revision:0,request_id:id}
    expect((await send({...bind,project_id:'foreign-project'})).status).toBe(400)
    const value=await(await send(bind)).json();expect(value.current_requirements.binding.project_id).toBe('owned-project');expect(value.current_requirements.values.page_count).toBeNull()
    expect((await send({...bind,source_message_id:'forged'})).status).toBe(400)
  })
  it('rejects anonymous, foreign and path-traversal requests without writing files',async()=>{
    const {send,directory}=await setup();const request={session_id:'chat-a',operation:'patch',config_version:4,patch:{search_mode:'web'},expected_revision:0,request_id:id}
    expect((await send(request,'wrong')).status).toBe(401);expect((await send({...request,session_id:'other'})).status).toBe(404);expect((await send({...request,session_id:'../chat-a'})).status).toBe(400);expect(await readdir(directory)).toEqual([])
  })
})
