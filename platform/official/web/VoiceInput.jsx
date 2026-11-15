import {t} from './i18n.mjs';
import React,{useEffect,useRef,useState} from 'react';
import {Mic,Square,X,LoaderCircle} from 'lucide-react';
import workletUrl from './recorder-worklet.js?url';
import {PcmUploadQueue} from './voice-recorder.mjs';

export default function VoiceInput({config,disabled,contextKey,onText,onActive,onNotice,label=t("语音输入"),purpose='question',jobId='',field=''}){
 const [phase,setPhase]=useState('idle'),[partial,setPartial]=useState(''),[seconds,setSeconds]=useState(0);
 const session=useRef(null),panel=useRef(null),callbacks=useRef({onText,onActive,onNotice});callbacks.current={onText,onActive,onNotice};
 const inline=purpose==='expert_feedback';
 const alive=s=>session.current===s&&!s.closed;
 function releaseCapture(s){s.stream?.getTracks().forEach(t=>t.stop());s.node?.disconnect();s.source?.disconnect();if(s.context?.state!=='closed')s.context?.close().catch(()=>{});}
 function release(s){
  clearInterval(s.timer);for(const name of ['connectDeadline','captureDeadline','recognitionDeadline','flushTimer','cueTimer'])clearTimeout(s[name]);
  releaseCapture(s);s.upload.clear();if(s.socket?.readyState<2)s.socket.close();
 }
 function close(){const s=session.current;if(s){s.closed=true;release(s);session.current=null;}setPhase('idle');setPartial('');callbacks.current.onActive(false);}
 function fail(s,text){if(!alive(s))return;close();callbacks.current.onNotice(text);}
 useEffect(()=>{close();return()=>{const s=session.current;if(s){s.closed=true;release(s);session.current=null;}callbacks.current.onActive(false);};},[contextKey]);
 useEffect(()=>{const hide=()=>{if(document.hidden)close();};document.addEventListener('visibilitychange',hide);window.addEventListener('pagehide',hide);return()=>{document.removeEventListener('visibilitychange',hide);window.removeEventListener('pagehide',hide);};},[]);
 useEffect(()=>{if(inline&&phase!=='idle')panel.current?.scrollIntoView({block:'nearest',inline:'nearest'});},[inline,phase]);
 function drain(s){
  if(!alive(s))return;
  try{if(s.upload.flush(s.socket)&&!s.recognitionDeadline)s.recognitionDeadline=setTimeout(()=>fail(s,t("语音识别超时，请重试。")),((config.recognition_timeout||30)+5)*1000);}catch(error){fail(s,error.message);}
 }
 function stop(s){
  if(!s||!alive(s)||s.ending)return;
  s.ending=true;setPhase('finishing');clearInterval(s.timer);clearTimeout(s.cueTimer);
  s.flushTimer=setTimeout(()=>fail(s,t("录音收尾超时，请重试。")),3000);
  s.node.port.postMessage({type:'flush'});s.stream.getTracks().forEach(t=>t.stop());
 }
 async function start(){
  if(disabled||session.current)return;
  if(!config?.ready){callbacks.current.onNotice(t("管理员尚未启用语音输入。"));return;}
  const s={closed:false,upload:new PcmUploadQueue(config.max_seconds)};session.current=s;setPhase('preparing');setPartial('');setSeconds(0);callbacks.current.onActive(true);
  try{
   if(!navigator.mediaDevices?.getUserMedia||!window.AudioWorkletNode)throw Error(t("录音需要 HTTPS 或本机 localhost，以及支持麦克风的浏览器。"));
   s.stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true}});
   if(!alive(s)){release(s);return;}
   s.context=new AudioContext();await s.context.resume();
   if(!alive(s)){release(s);return;}
   await s.context.audioWorklet.addModule(workletUrl);
   if(!alive(s)){release(s);return;}
   s.source=s.context.createMediaStreamSource(s.stream);s.node=new AudioWorkletNode(s.context,'wuyu-pcm-recorder');
   s.node.onprocessorerror=()=>fail(s,t("麦克风录音中断，请重新录制。"));
   s.node.port.onmessage=({data})=>{
    if(!alive(s))return;
    if(data.type==='pcm'){
     try{s.upload.push(data.buffer);}catch(error){return fail(s,error.message);}
     if(s.started===undefined){
      clearTimeout(s.captureDeadline);s.started=performance.now();
      // Show the speaking cue only after PCM is already being captured locally.
      s.cueTimer=setTimeout(()=>{if(alive(s)&&!s.ending)setPhase('recording');},600);
      s.timer=setInterval(()=>{const elapsed=(performance.now()-s.started)/1000;setSeconds(Math.floor(elapsed));if(elapsed>=s.upload.maxBytes/32000-.25)stop(s);},100);
     }
     drain(s);if(alive(s)&&s.upload.atLimit)stop(s);
    }else if(data.type==='flushed'){
     clearTimeout(s.flushTimer);releaseCapture(s);s.upload.finish();drain(s);
    }
   };
   s.captureDeadline=setTimeout(()=>fail(s,t("麦克风未返回录音，请检查设备后重试。")),5000);
   s.source.connect(s.node);s.node.connect(s.context.destination);
   const query=inline?`?purpose=expert_feedback&job_id=${encodeURIComponent(jobId)}&field=${encodeURIComponent(field)}`:'';
   s.socket=new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}/api/voice/stream${query}`);
   s.connectDeadline=setTimeout(()=>fail(s,t("语音服务连接超时，请重试。")),20000);
   s.socket.onmessage=event=>{
    if(!alive(s))return;
    let data;try{data=JSON.parse(event.data);}catch{return fail(s,t("语音服务返回格式异常。"));}
    if(data.type==='ready'){
     clearTimeout(s.connectDeadline);s.upload.setReady(data.max_seconds);drain(s);if(alive(s)&&s.upload.atLimit)stop(s);
    }else if(data.type==='partial')setPartial(data.text);
    else if(data.type==='final'){const value=data.text?.trim();close();if(value)callbacks.current.onText(value,{call_id:data.call_id,seconds:data.seconds});else callbacks.current.onNotice(t("未识别到语音，请重新录制。"));}
    else if(data.type==='error')fail(s,data.detail||t("语音识别失败。"));
   };
   s.socket.onerror=()=>fail(s,t("无法连接语音服务，请检查网络或联系管理员。"));
   s.socket.onclose=()=>{if(alive(s))fail(s,t("语音连接已关闭，请重新录制。"));};
  }catch(error){if(!alive(s)){release(s);return;}fail(s,error.name==='NotAllowedError'?t("请允许浏览器使用麦克风后重试。"):error.name==='NotFoundError'?t("未找到麦克风，请连接设备后重试。"):error.message||t("无法开始录音。"));}
 }
 const action=inline?t("保存"):t("发送");
 const hint=phase==='preparing'?t("正在准备录音，请等到“可以开始说话”后开口。"):phase==='recording'?t("正在录音，点击“完成录音”后核对文字再{v0}。", {v0: action.toLowerCase()}):t("正在识别录音，文字会填入输入框，核对后{v0}。", {v0: action.toLowerCase()});
 return <div className={`voice-input ${inline?'voice-input-inline':''}`}><button type="button" className={`icon-button voice-button ${phase==='recording'?'recording':''}`} aria-label={label} title={config?.ready?`${label} · ${config.model_label||t("语音识别")}`:t("管理员尚未启用语音输入")} disabled={disabled||phase!=='idle'} onClick={start}><Mic size={21}/></button>{phase!=='idle'&&<div className="voice-panel" ref={panel} role="region" aria-label={t("语音录入")} data-phase={phase}><div className="voice-panel-row">{phase==='recording'?<span className="voice-live-dot"/>:<LoaderCircle size={16} className="spin"/>}<strong role="status">{phase==='preparing'?t("正在准备录音…"):phase==='recording'?t("可以开始说话 · {v0}s", {v0: seconds}):t("正在转写…")}</strong>{phase==='recording'&&<button type="button" className="secondary" onClick={()=>stop(session.current)}><Square size={13}/>{t("完成录音")}</button>}<button type="button" className="icon-button" aria-label={t("取消录音")} onClick={close}><X size={16}/></button></div><p aria-live="polite">{partial||hint}</p></div>}</div>;
}

