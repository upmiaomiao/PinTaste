import React,{useEffect,useRef,useState} from 'react';
import {t} from './i18n.mjs';

// Keep this breakpoint aligned with the overlay rules in style.css/neutral.css.
const compactQuery='(max-width: 900px)';

export default function HistorySidebar({children,onClose,blocked=false}){
 const [compact,setCompact]=useState(()=>window.matchMedia(compactQuery).matches);
 const panel=useRef(null),blockedRef=useRef(blocked);
 blockedRef.current=blocked;
 useEffect(()=>{
  const media=window.matchMedia(compactQuery),update=()=>setCompact(media.matches);
  media.addEventListener('change',update);
  return()=>media.removeEventListener('change',update);
 },[]);
 useEffect(()=>{
  // Opening a phone drawer must not summon the on-screen keyboard. Do not
  // refocus when a child confirmation dialog opens/closes above this drawer.
  if(compact&&!blockedRef.current)panel.current?.querySelector('[data-history-close]')?.focus({preventScroll:true});
 },[compact]);
 function keyboard(event){
  if(!compact||blocked||event.key!=='Tab')return;
  const elements=[...panel.current.querySelectorAll('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex]')]
   .filter(element=>element.tabIndex>=0&&element.getClientRects().length);
  const first=elements[0],last=elements.at(-1);
  if(!first){event.preventDefault();panel.current.focus({preventScroll:true});return;}
  if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
  else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
 }
 return <>
  {compact&&<button type="button" className="mobile-scrim" tabIndex={-1} aria-label={t('关闭历史侧栏')} onClick={onClose} disabled={blocked}/>}
  <aside className="sidebar" id="history-sidebar" ref={panel} tabIndex={-1}
   role={compact?'dialog':'complementary'} aria-modal={compact||undefined}
   aria-labelledby="history-sidebar-heading" onKeyDown={keyboard}>{children}</aside>
 </>;
}
