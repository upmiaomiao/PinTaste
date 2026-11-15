/** Requirement provenance from accepted chat or an exact built-in question/answer pair.
 * Never infer a human decision from assistant prose, arbitrary tools or materials.
 * This receipt saves requirements only; existing spending/approval checks remain separate.
 */
type RecordValue = Record<string, unknown>
export interface RequirementConfirmationSource { id:string; text:string; kind:'message'|'question' }
const record=(value:unknown):RecordValue=>value!==null&&typeof value==='object'&&!Array.isArray(value)?value as RecordValue:{}
const bounded=(value:unknown):value is string=>typeof value==='string'&&value.length>0&&value.length<=2000
function jsonObject(value:unknown):RecordValue {
  if(typeof value!=='string')return record(value)
  if(value.length>64000)return {}
  try{return record(JSON.parse(value))}catch{return {}}
}
function answerText(questions:unknown, value:unknown):string|undefined {
  if(!Array.isArray(questions)||questions.length<1||questions.length>20)return
  const answer=jsonObject(value),answers=answer.answers
  if(Object.keys(answer).some(key=>key!=='answers')||!Array.isArray(answers)||answers.length!==questions.length)return
  const ids=new Set<string>(),lines:string[]=[]
  for(const raw of questions){
    const question=record(raw),id=question.id
    if(!bounded(id)||ids.has(id))return
    ids.add(id)
    const matches=answers.filter(rawAnswer=>record(rawAnswer).id===id)
    if(matches.length!==1)return
    const item=record(matches[0]),selected=item.selected,custom=item.custom
    if(Object.keys(item).some(key=>!['id','selected','custom'].includes(key))||!Array.isArray(selected)
      ||selected.some(value=>!bounded(value))||new Set(selected).size!==selected.length
      ||custom!==undefined&&(typeof custom!=='string'||custom.length>2000))return
    const options=question.options
    if(options!==undefined&&!Array.isArray(options))return
    const labels=(Array.isArray(options)?options:[]).map(option=>record(option).label)
    if(labels.some(label=>!bounded(label))||new Set(labels).size!==labels.length
      ||selected.some(value=>!labels.includes(value))||question.multi_select!==true&&selected.length>1)return
    const texts=[...selected as string[],...(typeof custom==='string'&&custom.trim()?[custom]:[])]
    if(!texts.length)return
    // Question prose/descriptions are model-authored. Only selected labels and typed text qualify.
    lines.push(...texts)
  }
  const text=lines.join('\n')
  return text.trim()&&text.length<=12000?text:undefined
}
/** A newer accepted human message invalidates every older question receipt, including late results. */
export function latestRequirementSource(events:readonly unknown[]):RequirementConfirmationSource|undefined {
  let latest:RequirementConfirmationSource|undefined
  const pending=new Map<string,{seq:number;questions:unknown}>()
  let lastSeq=-1
  let lastQuestionCall=-1
  for(const raw of events){
    const event=record(raw),seq=event.seq,data=record(event.data)
    if(!Number.isSafeInteger(seq)||Number(seq)<=lastSeq)continue
    lastSeq=Number(seq)
    if(event.type==='user/message'&&record(data.source).kind==='user'){
      const content=data.content
      if(!Array.isArray(content))continue
      latest={id:'user:'+String(seq),text:content.filter(block=>record(block).type==='text').map(block=>String(record(block).text??'')).join('\n'),kind:'message'}
      pending.clear()
      lastQuestionCall=-1
    }else if(latest&&event.type==='tool/call'&&data.name==='ask_user_question'&&typeof data.callId==='string'){
      if(pending.size>=20)pending.clear()
      pending.set(data.callId,{seq:Number(seq),questions:jsonObject(data.arguments).questions})
    }else if(latest&&event.type==='tool/result'){
      const message=record(data.message),source=record(message.source)
      // Native session tools identify their result in message.source, never in arbitrary output JSON.
      if(source.kind!=='tool'||typeof source.callId!=='string')continue
      const call=pending.get(source.callId)
      if(!call)continue
      pending.delete(source.callId)
      const blocks=message.content
      if(!Array.isArray(blocks)||blocks.length!==1)continue
      const block=record(blocks[0]),content=block.content
      if(block.type!=='tool-result'||block.isError===true||!Array.isArray(content)||content.length!==1)continue
      const textBlock=record(content[0])
      if(textBlock.type!=='text')continue
      const text=answerText(call.questions,textBlock.text)
      if(text&&call.seq>lastQuestionCall){lastQuestionCall=call.seq;latest={id:'question:'+call.seq+':'+String(seq),text,kind:'question'}}
    }
  }
  return latest
}
