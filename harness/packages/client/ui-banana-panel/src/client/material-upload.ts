/** Private attachment context stays in the authenticated host request body. */
import type { BananaClient } from './banana-mcp.ts'
export type MaterialRole = 'material' | 'template' | 'logo'
/** Picker restrictions match each user-selected attachment purpose. */
export const MATERIAL_ACCEPT:Record<MaterialRole,string>={
  material:'.pdf,.pptx,.docx,.xlsx,.csv,.tsv,.txt,.md,.png,.jpg,.jpeg,.webp',
  template:'.pdf,.pptx,.png,.jpg,.jpeg,.webp',
  logo:'.png,.jpg,.jpeg,.webp',
}
/** Use the selected purpose on the actual existing upload tool call. */
export function uploadSelectedMaterial(client:Pick<BananaClient,'callTool'>,projectHandle:string,file:Pick<File,'name'|'type'>,dataBase64:string,role:MaterialRole){
  return client.callTool('upload_presentation_material',{project_handle:projectHandle,filename:file.name,mime_type:file.type||'application/octet-stream',data_base64:dataBase64,role})
}
export interface MaterialBinding { projectHandle: string; materialIds: string[]; requestId: string }
export interface MaterialNotice { success: true; accepted: true; summary: string; context: string; project_id: string; material_count: number; duplicate: boolean; submitted: boolean }
/** Read only canonical material ids, never OCR or source instructions. */
export function materialIds(value: unknown): string[] {
  const rows=(value as {materials?:unknown[]}|null)?.materials
  if(!Array.isArray(rows))return []
  return [...new Set(rows.flatMap(row=>{const id=(row as {id?:unknown}|null)?.id;return typeof id==='string'&&id.length>0?[id]:[]}))]
}
/** Idempotent queue admission; the caller reuses this binding after a lost response. */
export async function bindMaterialContext(sessionId:string,binding:MaterialBinding,request:typeof fetch=fetch):Promise<MaterialNotice>{
  const response=await request('/api/wuyu/material-context',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sessionId,project_handle:binding.projectHandle,material_ids:binding.materialIds,request_id:binding.requestId})})
  if(!response.ok)throw new Error('material-context-unavailable')
  const notice=await response.json() as Partial<MaterialNotice>
  if(notice.success!==true||notice.accepted!==true||typeof notice.summary!=='string'||typeof notice.context!=='string'||!notice.context||typeof notice.project_id!=='string'||!notice.project_id||typeof notice.material_count!=='number'||typeof notice.submitted!=='boolean')throw new Error('material-context-not-accepted')
  return notice as MaterialNotice
}
