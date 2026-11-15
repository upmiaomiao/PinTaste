import {useState,useEffect,useRef} from 'react';
import {api} from './api';
import {getUiLanguage,getUiLanguageVersion,setUiLanguage,pendingSignInLanguage,clearSignInLanguage,subscribeUiLanguage,UI_LANGUAGE_KEY,UI_LANGUAGE_EVENT,t} from './uiLanguage.mjs';

export const defaultPreferences={theme:'light',font_size:'standard',send_key:'enter',ui_language:'zh-CN'};
export function usePreferences(user){
 const [preferences,setPreferences]=useState(defaultPreferences),[saving,setSaving]=useState(false),[error,setError]=useState(''),[ready,setReady]=useState(false);
 const uid=useRef(user?.id),epoch=useRef(0),queue=useRef(Promise.resolve()),pending=useRef(0),snapshot=useRef(defaultPreferences);
 const readSequence=useRef(0),writeVersion=useRef(0),signInWrite=useRef(null);
 uid.current=user?.id;
 const adopt=value=>{snapshot.current=value;setPreferences(value);};
 useEffect(()=>{
  let current=true;const generation=++epoch.current;pending.current=0;queue.current=Promise.resolve();signInWrite.current=null;readSequence.current++;writeVersion.current++;
  setReady(false);setError('');setSaving(false);adopt({...defaultPreferences,ui_language:getUiLanguage()});
  const active=()=>current&&generation===epoch.current&&uid.current===user?.id;
  async function refresh(){
   if(!user||pending.current||signInWrite.current)return;
   const request=++readSequence.current,write=writeVersion.current,languageVersion=getUiLanguageVersion();
   const fresh=()=>active()&&request===readSequence.current&&write===writeVersion.current&&!pending.current&&!signInWrite.current;
   let signInToken=null,retryNewChoice=false;
   try{
    let data=await api('/me/preferences',{cache:'no-store'});
    if(!fresh())return;
    const signInChoice=pendingSignInLanguage();
    if(signInChoice){
     signInToken={generation,choice:signInChoice};signInWrite.current=signInToken;
     const signInVersion=++writeVersion.current;readSequence.current++;
     data=await api('/me/preferences',{method:'PATCH',headers:{'X-Wuyu-Preferences-User-ID':user.id},body:JSON.stringify({ui_language:signInChoice})});
     if(!active()||signInWrite.current!==signInToken||signInVersion!==writeVersion.current)return;
     const latestChoice=pendingSignInLanguage();
     if(latestChoice&&latestChoice!==signInChoice){retryNewChoice=true;return;}
     clearSignInLanguage();
    }
    const currentLanguage=signInChoice|| (languageVersion===getUiLanguageVersion()?data.ui_language:getUiLanguage());
    // A storage notification may arrive after this GET began. Preserve that newer choice.
    adopt({...defaultPreferences,...data,ui_language:currentLanguage});
    if((signInChoice||languageVersion===getUiLanguageVersion())&&getUiLanguage()!==currentLanguage)setUiLanguage(currentLanguage);
    setReady(true);setError('');
   }catch(e){if(active()&&(signInToken?signInWrite.current===signInToken:fresh()))setError(e.message);}
   finally{
    if(signInToken&&signInWrite.current===signInToken)signInWrite.current=null;
    if(retryNewChoice&&active())void refresh();
   }
  }
  void refresh();
  const visibility=()=>{if(!document.hidden)void refresh();};
  const storage=event=>{if(event.key===UI_LANGUAGE_KEY)void refresh();};
  const acknowledged=()=>{if(user)void refresh();};
  window.addEventListener('focus',visibility);document.addEventListener('visibilitychange',visibility);window.addEventListener('storage',storage);window.addEventListener(UI_LANGUAGE_EVENT,acknowledged);
  return()=>{current=false;window.removeEventListener('focus',visibility);document.removeEventListener('visibilitychange',visibility);window.removeEventListener('storage',storage);window.removeEventListener(UI_LANGUAGE_EVENT,acknowledged);};
 },[user?.id]);
 useEffect(()=>subscribeUiLanguage(()=>adopt({...snapshot.current,ui_language:getUiLanguage()})),[]);
 useEffect(()=>{const media=window.matchMedia('(prefers-color-scheme: dark)');const apply=()=>{document.documentElement.dataset.theme=preferences.theme==='system'?(media.matches?'dark':'light'):preferences.theme;document.documentElement.dataset.font=preferences.font_size;};apply();media.addEventListener('change',apply);return()=>media.removeEventListener('change',apply);},[preferences.theme,preferences.font_size]);
 function update(change){
  const account=uid.current,generation=epoch.current;
  if(!account||!ready||signInWrite.current)return Promise.resolve(false);
  pending.current++;writeVersion.current++;readSequence.current++;setSaving(true);setError('');
  const operation=queue.current.catch(()=>{}).then(async()=>{
   if(uid.current!==account||epoch.current!==generation)return false;
   const previous=snapshot.current;adopt({...previous,...change});
   try{
    const result=await api('/me/preferences',{method:'PATCH',headers:{'X-Wuyu-Preferences-User-ID':account},body:JSON.stringify(change)});
    if(uid.current!==account||epoch.current!==generation)return false;
    const language=Object.hasOwn(change,'ui_language')?result.ui_language:getUiLanguage();
    adopt({...defaultPreferences,...result,ui_language:language});
    if(Object.hasOwn(change,'ui_language'))setUiLanguage(language);
    return true;
   }catch(e){if(uid.current===account&&epoch.current===generation){adopt({...previous,ui_language:getUiLanguage()});setError(e.code==='ACCOUNT_CHANGED'?t('账号已更改，请刷新页面后再修改设置。'):e.message);}return false;}
   finally{if(uid.current===account&&epoch.current===generation){pending.current--;setSaving(pending.current>0);}}
  });
  queue.current=operation;return operation;
 }
 return {preferences,update,saving,error,ready};
}
