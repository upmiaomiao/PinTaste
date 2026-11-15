/** Safe presentation metadata only; never retain response bodies, headers or exception text. */
export type TemplateActionOperation='confirm'|'upload'
export const templateActionCodes={
  TEMPLATE_READING_IN_PROGRESS:[409],TEMPLATE_PARSE_PENDING:[409],TEMPLATE_PARSE_INCOMPLETE:[409],
  TEMPLATE_NOT_READY:[409],TEMPLATE_NOT_AVAILABLE:[409],TEMPLATE_SCOPE_MISMATCH:[409],
  TEMPLATE_PREVIEW_INSUFFICIENT:[409],TEMPLATE_SOURCE_CHANGED:[409],REVISION_CONFLICT:[409],
  TEMPLATE_CONFIRMATION_UI_REQUIRED:[403],MATERIAL_UPLOAD_LIMIT:[413],
  TEMPLATE_LIMITS_UNAVAILABLE:[503],TEMPLATE_UPLOAD_INVALID_FILE:[400],
  TEMPLATE_ACTION_UNAVAILABLE:[]
} as const
export type TemplateActionCode=keyof typeof templateActionCodes
export interface TemplateActionFailure{code:TemplateActionCode;status:number;operation:TemplateActionOperation}
const record=(value:unknown):value is Record<string,unknown>=>value!==null&&typeof value==='object'&&!Array.isArray(value)
export function templateActionFailure(code:unknown,status:unknown,operation:TemplateActionOperation):TemplateActionFailure{
  const http=Number.isSafeInteger(status)&&Number(status)>=400&&Number(status)<=599?Number(status):503
  const known=typeof code==='string'&&Object.prototype.hasOwnProperty.call(templateActionCodes,code)?code as TemplateActionCode:'TEMPLATE_ACTION_UNAVAILABLE'
  const allowed=templateActionCodes[known] as readonly number[]
  const operationAllowed=['TEMPLATE_READING_IN_PROGRESS','TEMPLATE_PARSE_PENDING','TEMPLATE_PARSE_INCOMPLETE'].includes(known)||(operation==='upload'?['MATERIAL_UPLOAD_LIMIT','TEMPLATE_LIMITS_UNAVAILABLE','TEMPLATE_UPLOAD_INVALID_FILE','TEMPLATE_ACTION_UNAVAILABLE'].includes(known):!['MATERIAL_UPLOAD_LIMIT','TEMPLATE_LIMITS_UNAVAILABLE','TEMPLATE_UPLOAD_INVALID_FILE'].includes(known))
  return{code:operationAllowed&&(known==='TEMPLATE_ACTION_UNAVAILABLE'||allowed.includes(http))?known:'TEMPLATE_ACTION_UNAVAILABLE',status:http,operation}
}
export function templateActionFailureFromResponse(value:unknown,status:number,operation:TemplateActionOperation):TemplateActionFailure{
  const code=record(value)?typeof value.code==='string'?value.code:record(value.error)?value.error.code:undefined:undefined
  return templateActionFailure(code,status,operation)
}
export function isTemplateActionFailure(value:unknown,operation?:TemplateActionOperation):value is TemplateActionFailure{
  if(!record(value)||!['confirm','upload'].includes(String(value.operation))||operation&&value.operation!==operation)return false
  const normalized=templateActionFailure(value.code,value.status,value.operation as TemplateActionOperation)
  return normalized.code===value.code&&normalized.status===value.status
}
const messageKeys={
  TEMPLATE_READING_IN_PROGRESS:'templateAction.readingInProgress',TEMPLATE_PARSE_PENDING:'templateAction.parsePending',TEMPLATE_PARSE_INCOMPLETE:'templateAction.parseIncomplete',
  TEMPLATE_NOT_READY:'templateAction.notReady',TEMPLATE_NOT_AVAILABLE:'templateAction.notAvailable',
  TEMPLATE_SCOPE_MISMATCH:'templateAction.scopeMismatch',TEMPLATE_PREVIEW_INSUFFICIENT:'templateAction.previewInsufficient',
  TEMPLATE_SOURCE_CHANGED:'templateAction.sourceChanged',REVISION_CONFLICT:'templateAction.revisionChanged',
  TEMPLATE_CONFIRMATION_UI_REQUIRED:'templateAction.refreshEntry',MATERIAL_UPLOAD_LIMIT:'templateAction.uploadLimit',
  TEMPLATE_LIMITS_UNAVAILABLE:'templateAction.limitsUnavailable',TEMPLATE_UPLOAD_INVALID_FILE:'templateAction.invalidFile',
  TEMPLATE_ACTION_UNAVAILABLE:'templateDecision.error'
} as const
export function templateActionMessageKey(failure:TemplateActionFailure|null|undefined){
  return failure&&isTemplateActionFailure(failure)?messageKeys[failure.code]:'templateDecision.error'
}
