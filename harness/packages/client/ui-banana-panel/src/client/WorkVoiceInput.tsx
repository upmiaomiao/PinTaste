/** Work uses the primary platform's authenticated streaming speech API and settings. */
import {useEffect,useRef,useState} from 'react'
import type {InjectFace,PropsLocale,PropsRuntime} from '@deepseek-ai/dsh-client-ui-slots'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import {PcmUploadQueue} from './voice-pcm.ts'
import {isGenerationWorkSession} from './generation-settings.ts'
import css from './WorkVoiceInput.module.css'

export interface WorkVoiceConfig {ready:boolean;max_seconds:number;recognition_timeout:number}
export interface WorkVoiceInjected {
  loadVoiceConfig():Promise<WorkVoiceConfig>
  appendVoiceText(text:string):void
  voiceActive(active:boolean):void
}
type Props=PropsRuntime<'conversation.input.right'>&PropsLocale<'bananaPanel'>&InjectFace<WorkVoiceInjected>
interface Capture {closed:boolean;ending:boolean;queue:PcmUploadQueue;stream?:MediaStream;context?:AudioContext;node?:AudioWorkletNode;source?:MediaStreamAudioSourceNode;socket?:WebSocket;timers:Set<ReturnType<typeof setTimeout>>;clock?:ReturnType<typeof setInterval>;started?:number;recognitionTimer?:ReturnType<typeof setTimeout>}

/** Transcripts only append to this Session's unsent input; leaving or hiding cancels capture. */
export function WorkVoiceInput({sessionId,useSessions,useInput,loadVoiceConfig,appendVoiceText,voiceActive,t}:Props){
  const work=useSessions(state=>isGenerationWorkSession(state.byId[sessionId]))
  const inputPhase=useInput(state=>state.phase)
  const [phase,setPhase]=useState<'idle'|'preparing'|'recording'|'finishing'>('idle'),[partial,setPartial]=useState(''),[seconds,setSeconds]=useState(0),[notice,setNotice]=useState(''),[pendingText,setPendingText]=useState('')
  const current=useRef<Capture|null>(null),root=useRef<HTMLDivElement>(null),callbacks=useRef({appendVoiceText,voiceActive,t})
  callbacks.current={appendVoiceText,voiceActive,t}
  const alive=(capture:Capture)=>current.current===capture&&!capture.closed
  const releaseCapture=(capture:Capture)=>{capture.stream?.getTracks().forEach(track=>track.stop());capture.node?.disconnect();capture.source?.disconnect();if(capture.context&&capture.context.state!=='closed')void capture.context.close().catch(()=>{})}
  const release=(capture:Capture)=>{capture.closed=true;for(const timer of capture.timers)clearTimeout(timer);if(capture.clock)clearInterval(capture.clock);releaseCapture(capture);capture.queue.clear();if(capture.socket&&capture.socket.readyState<2)capture.socket.close()}
  const close=()=>{if(current.current)release(current.current);current.current=null;setPhase('idle');setPartial('');callbacks.current.voiceActive(false)}
  const fail=(capture:Capture,key:'voice.unavailable'|'voice.unsupported'|'voice.permission'|'voice.device'|'voice.service'|'voice.timeout'|'voice.noSpeech'|'voice.inputBusy')=>{if(!alive(capture))return;close();setNotice(callbacks.current.t(key))}
  useEffect(()=>{const hidden=()=>{if(document.hidden)close()};document.addEventListener('visibilitychange',hidden);window.addEventListener('pagehide',close)
    return()=>{document.removeEventListener('visibilitychange',hidden);window.removeEventListener('pagehide',close);if(current.current)release(current.current);current.current=null;callbacks.current.voiceActive(false)}
  },[sessionId])
  const deadline=(capture:Capture,run:()=>void,ms:number)=>{const id=setTimeout(run,ms);capture.timers.add(id);return id}
  const stop=(capture:Capture)=>{if(!alive(capture)||capture.ending)return;capture.ending=true;setPhase('finishing');if(capture.clock)clearInterval(capture.clock);capture.node?.port.postMessage({type:'flush'});capture.stream?.getTracks().forEach(track=>track.stop());deadline(capture,()=>{if(capture.node)fail(capture,'voice.timeout')},3000)}
  const start=async()=>{
    if(current.current||inputPhase!=='plain')return
    const capture:Capture={closed:false,ending:false,queue:new PcmUploadQueue(120),timers:new Set()};current.current=capture
    setNotice('');setPartial('');setSeconds(0);setPhase('preparing');callbacks.current.voiceActive(true)
    try{
      const config=await loadVoiceConfig();if(!alive(capture))return
      if(!config.ready){fail(capture,'voice.unavailable');return}
      capture.queue=new PcmUploadQueue(config.max_seconds)
      if(!navigator.mediaDevices?.getUserMedia||!globalThis.AudioWorkletNode){fail(capture,'voice.unsupported');return}
      capture.stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true}})
      if(!alive(capture)){release(capture);return}
      capture.context=new AudioContext();await capture.context.resume()
      if(!alive(capture)){release(capture);return}
      await capture.context.audioWorklet.addModule('/api/voice/recorder-worklet.js')
      if(!alive(capture)){release(capture);return}
      capture.source=capture.context.createMediaStreamSource(capture.stream);capture.node=new AudioWorkletNode(capture.context,'wuyu-pcm-recorder')
      const drain=()=>{if(!alive(capture))return;try{if(capture.queue.flush(capture.socket??null)&&!capture.recognitionTimer)capture.recognitionTimer=deadline(capture,()=>fail(capture,'voice.timeout'),(config.recognition_timeout+5)*1000)}catch{fail(capture,'voice.service')}}
      capture.node.onprocessorerror=()=>fail(capture,'voice.device')
      capture.node.port.onmessage=({data}:{data:unknown})=>{
        if(!alive(capture)||!data||typeof data!=='object')return
        const message=data as {type?:unknown;buffer?:unknown}
        if(message.type==='pcm'){
          if(!(message.buffer instanceof ArrayBuffer)){fail(capture,'voice.service');return}
          try{capture.queue.push(message.buffer)}catch{fail(capture,'voice.service');return}
          if(capture.started===undefined){capture.started=performance.now();deadline(capture,()=>{if(alive(capture)&&!capture.ending)setPhase('recording')},600)
            capture.clock=setInterval(()=>{const elapsed=(performance.now()-capture.started!)/1000;setSeconds(Math.floor(elapsed));if(elapsed>=capture.queue.maxBytes/32000-.25)stop(capture)},100)}
          drain();if(alive(capture)&&capture.queue.atLimit)stop(capture)
        }else if(message.type==='flushed'){releaseCapture(capture);delete capture.node;capture.queue.finish();drain()}
      }
      deadline(capture,()=>{if(capture.started===undefined)fail(capture,'voice.device')},5000)
      capture.source.connect(capture.node);capture.node.connect(capture.context.destination)
      capture.socket=new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}/api/voice/stream?purpose=work`)
      let ready=false;deadline(capture,()=>{if(!ready)fail(capture,'voice.timeout')},20000)
      capture.socket.onmessage=event=>{
        if(!alive(capture))return
        let message:unknown;try{message=JSON.parse(String(event.data))}catch{fail(capture,'voice.service');return}
        if(!message||typeof message!=='object'){fail(capture,'voice.service');return}
        const data=message as {type?:unknown;text?:unknown;max_seconds?:unknown}
        if(data.type==='ready'){if(!Number.isSafeInteger(data.max_seconds)||Number(data.max_seconds)<1||Number(data.max_seconds)>120){fail(capture,'voice.service');return}ready=true;capture.queue.setReady(Number(data.max_seconds));drain();if(alive(capture)&&capture.queue.atLimit)stop(capture)}
        else if(data.type==='partial'&&typeof data.text==='string')setPartial(data.text.slice(0,20000))
        else if(data.type==='final'){const text=typeof data.text==='string'?data.text.trim():'';close();if(!text)setNotice(callbacks.current.t('voice.noSpeech'));else try{callbacks.current.appendVoiceText(text);root.current?.closest('[data-composer-card]')?.querySelector<HTMLElement>('[data-composer-input]')?.focus()}catch{setPendingText(text);setNotice(callbacks.current.t('voice.inputBusy'))}}
        else if(data.type==='error')fail(capture,'voice.service')
      }
      capture.socket.onerror=()=>fail(capture,'voice.service');capture.socket.onclose=()=>{if(alive(capture))fail(capture,'voice.service')}
    }catch(error){if(!alive(capture)){release(capture);return}fail(capture,error instanceof Error&&error.name==='NotAllowedError'?'voice.permission':error instanceof Error&&error.name==='NotFoundError'?'voice.device':'voice.service')}
  }
  if(!work)return null
  return <div ref={root} className={css.root}>
    <button type="button" className={css.trigger} aria-label={t('voice.input')} title={t('voice.input')} disabled={inputPhase!=='plain'||phase!=='idle'} onClick={()=>{void start()}}><svg viewBox="0 0 24 24" width="21" height="21" fill="none" aria-hidden="true"><rect x="9" y="2" width="6" height="13" rx="3" stroke="currentColor" strokeWidth="1.7"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"/></svg></button>
    {phase!=='idle'?<section className={css.panel} role="region" aria-label={t('voice.recording')}><header><strong role="status">{t(phase==='preparing'?'voice.preparing':phase==='recording'?'voice.speaking':'voice.finishing',{seconds})}</strong>{phase==='recording'?<button type="button" onClick={()=>{if(current.current)stop(current.current)}}>{t('voice.finish')}</button>:null}<button type="button" aria-label={t('voice.cancel')} onClick={close}>×</button></header><p aria-live="polite">{partial||t('voice.hint')}</p></section>:null}
    {notice?<span className={css.notice} role="status">{notice}<button type="button" aria-label={t('voice.closeNotice')} onClick={()=>setNotice('')}>×</button></span>:null}
    {pendingText?<section className={css.panel} aria-label={t('voice.pending')}><p>{pendingText}</p><button type="button" disabled={inputPhase!=='plain'} onClick={()=>{try{appendVoiceText(pendingText);setPendingText('');setNotice('')}catch{setNotice(t('voice.inputBusy'))}}}>{t('voice.apply')}</button></section>:null}
  </div>
}
