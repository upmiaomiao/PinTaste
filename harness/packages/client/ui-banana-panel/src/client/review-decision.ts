import { toolJsonObjects } from './tool-json.ts'
/** Project-bound, versioned decisions projected from the actual settled MCP result. */
export type ReviewDecision={kind:'template'}|{kind:'save';revision:number}|{kind:'images';revision:number;versions:Record<string,number>;previews:{number:number;label:string;url:string}[]}
const object=(value:unknown):value is Record<string,unknown>=>!!value&&typeof value==='object'&&!Array.isArray(value)

/** Only same-origin owned images are rendered; source-document JSON cannot mint a decision. */
export function reviewDecision(texts:readonly string[],handle:string,base:string):ReviewDecision|null{
  for(const raw of toolJsonObjects(texts)){
    if(!object(raw)||raw.project_handle!==handle||!object(raw.conversation))continue
    const conversation=raw.conversation
    if(conversation.stage==='exported'&&Array.isArray(conversation.choices)&&conversation.choices.some(choice=>object(choice)&&choice.id==='save_personal_template')&&Number.isSafeInteger(raw.revision)&&Number(raw.revision)>=0)return {kind:'save',revision:Number(raw.revision)}
    if(conversation.stage==='template_selection'&&conversation.requires_user_confirmation===true)return {kind:'template'}
    if(conversation.stage!=='final_image_review'||conversation.requires_user_confirmation!==true||!Number.isSafeInteger(raw.revision)||Number(raw.revision)<0||!object(conversation.review_target)||!object(conversation.review_target.image_versions)||!Array.isArray(conversation.previews))continue
    const versions:Record<string,number>={}
    let valid=true
    for(const[key,value]of Object.entries(conversation.review_target.image_versions)){
      if(!/^[1-9][0-9]?$/.test(key)||Number(key)>80||!Number.isSafeInteger(value)||Number(value)<1){valid=false;break}
      versions[key]=Number(value)
    }
    if(!valid||!Object.keys(versions).length||conversation.previews.length!==Object.keys(versions).length)continue
    const previews=[]
    for(const [index,item]of conversation.previews.entries()){
      if(!object(item)||typeof item.url!=='string'||typeof item.label!=='string'||!(String(index+1)in versions)){valid=false;break}
      try{const url=new URL(item.url,base),origin=new URL(base).origin
        if(url.origin!==origin||url.username||url.password||!['/ppt-agent/assets','/agent-api/api/projects/'].some(path=>url.pathname===path||path.endsWith('/')&&url.pathname.startsWith(path))){valid=false;break}
        previews.push({number:index+1,label:item.label,url:url.href})
      }catch{valid=false;break}
    }
    if(valid)return {kind:'images',revision:Number(raw.revision),versions,previews}
  }
  return null
}
