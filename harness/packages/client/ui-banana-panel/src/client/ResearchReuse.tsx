import {useEffect,useRef,useState} from 'react'
import type {PropsLocale} from '@deepseek-ai/dsh-client-ui-slots'
import {ResearchConversationClient} from './research-conversation.ts'
import {researchReuseAvailable,reuseExistingResearch} from './research-reuse.ts'
import css from './BananaPanelRow.module.css'

/** Opening an existing conversation can reveal retained research without editing its history. */
export function ResearchReuse({projectId,requirementsRevision,t}:{projectId:string;requirementsRevision:number}&PropsLocale<'bananaPanel'>){
  const operation=useRef<AbortController|null>(null)
  const [visible,setVisible]=useState(false),[state,setState]=useState<'idle'|'busy'|'ready'|'changed'|'error'>('idle')
  useEffect(()=>{
    const read=new AbortController();setVisible(false);setState('idle')
    void new ResearchConversationClient().read(projectId,read.signal).then(snapshot=>{if(!read.signal.aborted)setVisible(researchReuseAvailable(snapshot))}).catch(()=>{})
    return ()=>{read.abort();operation.current?.abort();operation.current=null}
  },[projectId,requirementsRevision])
  const reuse=async()=>{
    if(operation.current||state==='ready'||state==='changed')return
    const current=new AbortController();operation.current=current;setState('busy')
    try{
      const result=await reuseExistingResearch(projectId,AbortSignal.any([current.signal,AbortSignal.timeout(20000)]))
      if(!current.signal.aborted)setState(result==='state_changed'?'changed':'ready')
    }catch{if(!current.signal.aborted)setState('error')}
    finally{if(operation.current===current)operation.current=null}
  }
  if(!visible)return null
  return <section className={css.reviewCard} data-wuyu-research-reuse>
    <strong>{t('reuse.research.title')}</strong><p>{t('reuse.research.hint')}</p>
    <button type="button" className={css.reviewAction} disabled={state==='busy'||state==='ready'||state==='changed'} onClick={()=>void reuse()}>{t(state==='busy'?'reuse.research.busy':'reuse.research.action')}</button>
    {state==='ready'?<p role="status">{t('reuse.research.ready')}</p>:null}
    {state==='changed'?<p role="status">{t('reuse.research.changed')}</p>:null}
    {state==='error'?<p role="alert">{t('reuse.research.error')}</p>:null}
  </section>
}
