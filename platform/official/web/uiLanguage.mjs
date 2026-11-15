/** One account language across QA, Work and their same-origin settings iframe. */
import chat from './messages/chat.mjs';
import settings from './messages/settings.mjs';
import core from './messages/core.mjs';

export const UI_LANGUAGE_KEY='wuyu-ui-language';
export const UI_LANGUAGE_EVENT='wuyu-ui-language-change';
const SIGN_IN_LANGUAGE_KEY='wuyu-ui-language-sign-in-choice';
const english={...chat,...settings,...core};
export function normalizeUiLanguage(value){return value==='en'?'en':'zh-CN';}
function readCache(){try{return normalizeUiLanguage(window.localStorage.getItem(UI_LANGUAGE_KEY));}catch{return 'zh-CN';}}
let language=typeof window==='undefined'?'zh-CN':readCache();
let version=0;
const listeners=new Set();
export function getUiLanguage(){return language;}
export function getUiLanguageVersion(){return version;}
export function subscribeUiLanguage(listener){listeners.add(listener);return()=>listeners.delete(listener);}
function apply(value){
 const next=normalizeUiLanguage(value);
 if(typeof document!=='undefined')document.documentElement.lang=next;
 if(next===language)return;
 language=next;version++;
 for(const listener of [...listeners])listener();
}
/** Publish only an acknowledged account setting, or an explicit signed-out choice. */
export function setUiLanguage(value){
 const next=normalizeUiLanguage(value);apply(next);
 if(typeof window==='undefined')return;
 try{window.localStorage.setItem(UI_LANGUAGE_KEY,next);}catch{/* Server preference remains authoritative. */}
 window.dispatchEvent(new CustomEvent(UI_LANGUAGE_EVENT,{detail:{language:next}}));
}
/** An explicit choice on the sign-in page should remain selected after sign-in. */
export function setSignInLanguage(value){
 const next=normalizeUiLanguage(value);
 try{window.sessionStorage.setItem(SIGN_IN_LANGUAGE_KEY,next);}catch{/* Still apply locally. */}
 setUiLanguage(next);
}
export function pendingSignInLanguage(){
 try{const value=window.sessionStorage.getItem(SIGN_IN_LANGUAGE_KEY);return value==='en'||value==='zh-CN'?value:null;}catch{return null;}
}
export function clearSignInLanguage(){try{window.sessionStorage.removeItem(SIGN_IN_LANGUAGE_KEY);}catch{}}
if(typeof window!=='undefined'){
 apply(language);
 window.addEventListener('storage',event=>{if(event.key===UI_LANGUAGE_KEY)apply(event.newValue);});
 window.addEventListener(UI_LANGUAGE_EVENT,event=>apply(event.detail?.language));
}
export function t(message,values){
 const descriptor=message&&typeof message==='object'&&typeof message.message==='string'?message:null;
 const key=String(descriptor?descriptor.message:(message??''));
 const parameters=values??descriptor?.values;
 const text=language==='en'?(english[key]??key):key;
 return parameters?text.replace(/\{([A-Za-z0-9_]+)\}/g,(whole,name)=>Object.hasOwn(parameters,name)?String(parameters[name]):whole):text;
}
