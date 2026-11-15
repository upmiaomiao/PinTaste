/** Authenticated access to the same requirements owner used by the conversation's local tools. */
import type { IncomingMessage, ServerResponse } from 'node:http'
import { PresentationRequirementsStore, RequirementsFailure, presentationRecord } from './presentation-requirements.ts'
import { type CurrentRequirements, type RequirementField } from './presentation-config.ts'

interface ConfigHost {
  directory:string
  reject(request:IncomingMessage):401|403|undefined
  ownsSession(id:string):Promise<boolean>
  requirements?:PresentationRequirementsStore
  bindProject?(id:string,handle:string,options:{expected_revision:number;inherit_existing:boolean;signal:AbortSignal}):Promise<CurrentRequirements>
  projectRequirements?(id:string,current:CurrentRequirements,signal:AbortSignal,forceFields?:readonly RequirementField[]):Promise<CurrentRequirements>
  observeRequirements?(id:string,current:CurrentRequirements,signal:AbortSignal):Promise<CurrentRequirements>
}
const object=(v:unknown):Record<string,unknown>=>v!==null&&typeof v==='object'&&!Array.isArray(v)?v as Record<string,unknown>:{}
const validId=(v:unknown)=>typeof v==='string'&&/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(v)
async function readBody(request:IncomingMessage):Promise<Record<string,unknown>>{
  if(!/^application\/json(?:;|$)/i.test(request.headers['content-type']||''))throw new RequirementsFailure(415,'JSON_REQUIRED')
  const chunks:Buffer[]=[];let bytes=0
  for await(const chunk of request){const data=Buffer.isBuffer(chunk)?chunk:Buffer.from(chunk);bytes+=data.length;if(bytes>65536)throw new RequirementsFailure(413,'REQUEST_TOO_LARGE');chunks.push(data)}
  try{return object(JSON.parse(Buffer.concat(chunks).toString('utf8')))}catch{throw new RequirementsFailure(400,'INVALID_JSON')}
}
function respond(response:ServerResponse,status:number,value:object):void{
  response.writeHead(status,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});response.end(JSON.stringify(value))
}
/** Preserve previous response fields while version four exposes the authoritative requirements and binding. */
function expose(current:CurrentRequirements,version:unknown):object{
  const full=presentationRecord(current)
  if(version===4)return full
  if(version===3)return {revision:full.revision,config:full.config}
  if(version===2)return {revision:full.revision,config:{task_mode:full.config.task_mode,creation_kind:full.config.creation_kind,page_count:full.config.page_count,output_preference:full.config.output_preference,language:full.config.language}}
  return {revision:full.revision,config:{page_count:full.config.page_count,output_preference:full.config.output_preference,language:full.config.language}}
}
/** Create the existing exact route; changing preferences starts no model, search or generation work. */
export function createPresentationConfigHandler(host:ConfigHost){
  const store=host.requirements??new PresentationRequirementsStore(host.directory)
  return async(req:IncomingMessage,res:ServerResponse):Promise<void>=>{
    try{
      const rejection=host.reject(req);if(rejection!==undefined)throw new RequirementsFailure(rejection,rejection===401?'UNAUTHORIZED':'FORBIDDEN')
      if(req.method!=='POST')throw new RequirementsFailure(405,'POST_REQUIRED')
      const request=await readBody(req),version=request.config_version,operation=request.operation,id=request.session_id
      if(typeof id!=='string'||!/^[A-Za-z0-9_-]{1,160}$/.test(id)||!['read','save','patch','bind'].includes(String(operation))||(version!==undefined&&version!==2&&version!==3&&version!==4))throw new RequirementsFailure(400,'PPT_CONFIG_INVALID')
      if((operation==='patch'||operation==='bind')&&version!==4)throw new RequirementsFailure(400,'PPT_CONFIG_INVALID')
      const keys=operation==='read'?['session_id','operation','config_version']:operation==='save'?['session_id','operation','config_version','config','expected_revision','request_id']:operation==='patch'?['session_id','operation','config_version','patch','expected_revision','request_id']:['session_id','operation','config_version','project_handle','expected_revision','request_id','inherit_existing']
      if(Object.keys(request).some(key=>!keys.includes(key)))throw new RequirementsFailure(400,'PPT_CONFIG_INVALID')
      if(!await host.ownsSession(id))throw new RequirementsFailure(404,'SESSION_NOT_FOUND')
      if(operation==='read'){let current=await store.read(id);if(version===4&&host.observeRequirements)current=await host.observeRequirements(id,current,AbortSignal.timeout(15000));respond(res,200,expose(current,version));return}
      if(!Number.isSafeInteger(request.expected_revision)||Number(request.expected_revision)<0||!validId(request.request_id))throw new RequirementsFailure(400,'PPT_CONFIG_INVALID')
      let current:CurrentRequirements;const signal=AbortSignal.timeout(20000)
      if(operation==='bind'){
        if(typeof request.project_handle!=='string'||request.project_handle.length<20||request.project_handle.length>8192||(request.inherit_existing!==undefined&&typeof request.inherit_existing!=='boolean'))throw new RequirementsFailure(400,'PPT_CONFIG_INVALID')
        if(!host.bindProject)throw new RequirementsFailure(503,'PPT_BINDING_UNAVAILABLE')
        current=await host.bindProject(id,request.project_handle,{expected_revision:Number(request.expected_revision),inherit_existing:request.inherit_existing!==false,signal})
      }else if(operation==='patch'){
        const patch=object(request.patch)
        if(!('search_mode'in patch)||Object.keys(patch).some(key=>key!=='search_mode'))throw new RequirementsFailure(400,'PPT_REQUIREMENTS_INVALID')
        current=await store.update(id,patch,{expected_revision:Number(request.expected_revision),request_id:String(request.request_id),source:'user'})
        if(host.projectRequirements)current=await host.projectRequirements(id,current,signal)
      }else{
        if(!('config'in request))throw new RequirementsFailure(400,'PPT_CONFIG_INVALID')
        current=await store.saveLegacy(id,request.config,Number(request.expected_revision),String(request.request_id))
        if(host.projectRequirements)current=await host.projectRequirements(id,current,signal)
      }
      respond(res,200,expose(current,version))
    }catch(error){
      const invalid=error instanceof Error&&['PPT_CONFIG_INVALID','PPT_REQUIREMENTS_INVALID'].includes(error.message)
      const status=error instanceof RequirementsFailure?error.status:invalid?400:503,code=error instanceof RequirementsFailure?error.code:invalid?'PPT_CONFIG_INVALID':'PPT_CONFIG_UNAVAILABLE'
      if(!res.headersSent)respond(res,status,{error:{code}})
    }
  }
}
