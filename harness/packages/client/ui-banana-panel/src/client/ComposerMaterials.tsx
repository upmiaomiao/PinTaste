import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { InjectFace, PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots'
import type { ObservableSnapshot } from '@deepseek-ai/dsh-client-store'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import { previewTargetFromUrl } from './preview-target.ts'
import type { PreviewTarget } from './preview-target.ts'
import type { createBananaPanelStore } from './store.ts'
import { MATERIAL_ACCEPT, type MaterialRole } from './material-upload.ts'
import { bindMaterialInputs } from './material-input.ts'
import { isGenerationWorkSession } from './generation-settings.ts'
import type { UploadSnapshot, UploadPhase } from './material-queue.ts'
import type { BananaPanelKey } from './locales.ts'
import css from './ImageModeSelect.module.css'
import { MaterialFileIcon } from './MaterialFileIcon.tsx'
import { materialUploadErrorKey, materialUploadStageKey } from './material-upload-errors.ts'
export interface MaterialUploadsInjected {
  hooks:{materialUploads:ObservableSnapshot<UploadSnapshot>}
  intakeFiles(files:File[],role:MaterialRole,handle:string|null):Promise<void>
  confirmUploads():Promise<void>
  retryUpload(id?:string):void
  pauseUpload(id:string):void
  removeUpload(id:string):void
  replaceUpload(id:string,file:File):Promise<void>
  syncUploads():Promise<void>
  openDetails():void
}
type Props=PropsRuntime<'conversation.input.right'>&PropsStore<ReturnType<typeof createBananaPanelStore>>&PropsLocale<'bananaPanel'>&InjectFace<MaterialUploadsInjected>
const phaseKeys:Record<UploadPhase,BananaPanelKey>={staged:'upload.staged',queued:'upload.queued',uploading:'upload.sending',registering:'upload.registering',uploaded:'upload.uploaded',attached:'upload.attached',failed:'upload.failed',paused:'upload.paused'}
/** Material files remain in the Session upload controller; the view handles gestures only. */
export function ComposerMaterials({sessionId,useSessions,useStore,useMaterialUploads,actions,t,openDetails,intakeFiles,confirmUploads,retryUpload,pauseUpload,removeUpload,replaceUpload,syncUploads}:Props){
  const handle=useStore(s=>s.projectHandle),projects=useStore(s=>s.projects),currentId=useStore(s=>s.projectId),uploads=useMaterialUploads(s=>s)
  const work=useSessions(s=>isGenerationWorkSession(s.byId[sessionId]))
  const state=useRef({handle,projects,currentId});state.current={handle,projects,currentId}
  const [notice,setNotice]=useState(''),[menuOpen,setMenuOpen]=useState(false),[dragging,setDragging]=useState(false)
  const root=useRef<HTMLDivElement>(null),picker=useRef<HTMLInputElement>(null),menu=useRef<HTMLDivElement>(null),attachButton=useRef<HTMLButtonElement>(null),uploadRole=useRef<MaterialRole>('material'),intake=useRef(intakeFiles)
  const replacement=useRef<string|null>(null)
  const queue=useRef<HTMLDetailsElement>(null),queuePanel=useRef<HTMLElement>(null)
  intake.current=intakeFiles
  useLayoutEffect(()=>{
    const card=root.current?.closest<HTMLElement>('[data-composer-card]'),details=queue.current,panel=queuePanel.current
    if(!card||!details||!panel)return
    const scrollport=card.closest<HTMLElement>('[data-conversation-scroll]')
    const position=()=>{
      if(!details.open)return
      const clip=scrollport?.getBoundingClientRect(),anchor=card.getBoundingClientRect()
      const top=Math.max(0,clip?.top??0)+8,bottom=Math.min(window.innerHeight,clip?.bottom??window.innerHeight)-8
      panel.style.setProperty('--wuyu-upload-space',Math.max(0,bottom-top)+'px')
      const height=panel.getBoundingClientRect().height,above=anchor.top-8-height,below=anchor.bottom+8
      const preferred=above>=top||below+height>bottom?above:below
      const visibleTop=Math.max(top,Math.min(preferred,bottom-height))
      panel.style.setProperty('--wuyu-upload-top',visibleTop-anchor.top-card.clientTop+'px')
    }
    position()
    const observer=new ResizeObserver(position);observer.observe(card);observer.observe(panel);if(scrollport)observer.observe(scrollport)
    details.addEventListener('toggle',position);window.addEventListener('resize',position);window.addEventListener('scroll',position,true)
    return()=>{observer.disconnect();details.removeEventListener('toggle',position);window.removeEventListener('resize',position);window.removeEventListener('scroll',position,true)}
  },[uploads.rows.length])
  useEffect(()=>{const card=root.current?.closest<HTMLElement>('[data-composer-card]');if(!card||!work)return;const zone=card.closest<HTMLElement>('[data-details-collapsed]')||card.closest<HTMLElement>('[data-phase]')||card;return bindMaterialInputs(card,files=>{void intake.current(files,'material',state.current.handle)},setDragging,zone)},[work])
  useEffect(()=>{
    const open=(target:PreviewTarget)=>{const s=state.current;if(!s.handle||target.project_id&&!s.projects[target.project_id]){setNotice(t('preview.unknownProject'));return}actions.openPreview(target);openDetails()}
    const click=(event:MouseEvent)=>{if(event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;const anchor=(event.target as Element|null)?.closest<HTMLAnchorElement>('a[href]');if(!anchor)return;const target=previewTargetFromUrl(anchor.href,location.href);if(!target)return;event.preventDefault();event.stopPropagation();open(target)}
    const hash=()=>{const target=previewTargetFromUrl(location.href,location.href);if(target)open(target)}
    document.addEventListener('click',click,true);window.addEventListener('hashchange',hash);hash();return()=>{document.removeEventListener('click',click,true);window.removeEventListener('hashchange',hash)}
  },[actions,openDetails,t,handle,projects,currentId])
  const choose=(role:MaterialRole)=>{if(!picker.current)return;replacement.current=null;uploadRole.current=role;picker.current.multiple=true;picker.current.accept=MATERIAL_ACCEPT[role];setMenuOpen(false);picker.current.click()}
  const closeQueue=()=>{if(!queue.current)return;queue.current.open=false;queue.current.querySelector('summary')?.focus()}
  const limits=uploads.limits,mb=(bytes:number)=>Math.round(bytes/1024/1024),failed=uploads.rows.filter(row=>row.phase==='failed'||row.phase==='paused').length
  const prepared=uploads.rows.filter(row=>row.phase==='attached'&&!row.sent).length
  const pending=uploads.rows.filter(row=>row.phase==='staged').length,uncheckedFailure=uploads.rows.some(row=>!row.submitted&&row.phase==='failed'),submittedFailure=uploads.rows.some(row=>row.submitted&&(row.phase==='failed'||row.phase==='paused'))
  const error=(code:string)=>t(materialUploadErrorKey(code),{mb:limits?mb(limits.max_file_bytes):50,count:limits?.max_project_files||50})
  return <div ref={root} className={css.materialControls} data-material-upload-controls>
    <div ref={menu} className={css.attachmentMenu} onBlur={event=>{if(!event.currentTarget.contains(event.relatedTarget))setMenuOpen(false)}} onKeyDown={event=>{if(event.key==='Escape'){setMenuOpen(false);attachButton.current?.focus();event.preventDefault()}else if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)&&menuOpen){const items=Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('[role=menuitem]')||[]),index=items.indexOf(document.activeElement as HTMLButtonElement),next=event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length;items[next]?.focus();event.preventDefault()}}}>
      <button type="button" className={css.attachIcon} aria-label={t('material.add')} title={t('material.add')} onClick={()=>choose('material')}><svg viewBox="0 0 24 24" width="21" height="21" fill="none" aria-hidden><path d="m9 13 6.3-6.3a2.5 2.5 0 0 1 3.5 3.5l-8.5 8.5a4 4 0 0 1-5.6-5.6l9.1-9.2a5.5 5.5 0 1 1 7.8 7.8l-9.2 9.2" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"/></svg></button>
      <button ref={attachButton} type="button" className={css.attachIcon} aria-label={t('current.moreAttachments')} title={t('current.moreAttachments')} aria-haspopup="menu" aria-expanded={menuOpen} onClick={()=>setMenuOpen(open=>!open)}><svg viewBox="0 0 20 20" width="15" height="15" fill="none" aria-hidden><path d="m5 7 5 5 5-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg></button>
      {menuOpen?<div role="menu" aria-label={t('material.add')} className={css.attachmentOptions}>{(['template','logo'] as const).map(role=><button type="button" role="menuitem" data-material-role={role} key={role} onClick={()=>choose(role)}>{t(role==='template'?'material.roleTemplate':'material.roleLogo')}</button>)}{handle?<button type="button" role="menuitem" className={css.materialDetails} onClick={()=>{setMenuOpen(false);actions.openPreview({view:'materials'});openDetails()}}>{t('preview.open')}</button>:null}</div>:null}
    </div>
    <input ref={picker} type="file" multiple hidden data-material-file-picker accept={MATERIAL_ACCEPT.material} aria-label={t('material.selectFiles')} onChange={event=>{const files=Array.from(event.target.files||[]);event.target.value='';const replaceId=replacement.current;replacement.current=null;if(replaceId&&files[0])void replaceUpload(replaceId,files[0]);else void intakeFiles(files,uploadRole.current,handle)}} />
    {dragging?<span className={css.uploadNotice} role="status">{t('upload.dropHint')}</span>:null}
    {uploads.rows.length?<details ref={queue} className={css.uploadQueue} open={uploads.busy||uploads.syncFailed||failed>0} onKeyDown={event=>{if(event.key==='Escape'){closeQueue();event.preventDefault();event.stopPropagation()}}}>
      <summary>{pending?t(uploads.batchActive?'upload.nextBatchSummary':'upload.pendingSummary',{count:pending}):t(prepared?'upload.preparedSummary':'upload.fileSummary',{count:prepared||uploads.rows.length})}</summary>
      <section ref={queuePanel} data-material-queue-panel className={css.uploadQueueBody} aria-label={t('upload.queueLabel')}>
        <header className={css.uploadQueueHeader}><strong>{t('upload.queueLabel')}</strong><span>{t('upload.fileSummary',{count:uploads.rows.length})}</span><button type="button" className={css.closeQueue} aria-label={t('upload.closeSelection')} title={t('upload.closeSelection')} onClick={closeQueue}><svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden><path d="m4 4 8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg></button></header>
        <p className={css.uploadSelectionHint}>{t(uploads.batchActive?'upload.batchActiveHint':'upload.selectionHint')}</p>
        <p className={css.uploadLimits}>{limits?t('upload.limits',{mb:mb(limits.max_file_bytes),project:limits.max_project_files,batch:limits.max_batch_files,batchMb:mb(limits.max_batch_bytes)}):t(uploads.intaking||!uploads.intakeError?'upload.loadingLimits':'upload.limitsFailed')}</p>
        <p className={css.uploadLimits}>{t('upload.pasteHint')}</p>
        <ol className={css.uploadRows}>{uploads.rows.map(row=><li key={row.id} data-upload-id={row.id} data-upload-phase={row.phase} data-material-role={row.role} data-upload-submitted={row.submitted}>
          <div className={css.uploadName}><MaterialFileIcon filename={row.filename}/><strong title={row.filename}>{row.filename}</strong><small>{row.size<1024*1024?t('upload.sizeKb',{size:Math.ceil(row.size/1024)}):t('upload.sizeMb',{size:(row.size/1024/1024).toFixed(1)})}</small></div>
          <span>{t(row.sending?'upload.submitting':row.sent?'upload.sent':!row.submitted&&row.phase==='failed'?['UPLOAD_SERVICE_UNAVAILABLE','UNAUTHORIZED'].includes(row.error||'')?'upload.checkUnavailable':'upload.checkFailed':phaseKeys[row.phase])}{row.phase==='uploading'?' '+row.percent+'%':''}</span>
          {row.phase==='uploading'||row.phase==='registering'?<progress max="100" value={row.percent} aria-label={t('upload.progress',{filename:row.filename})}/>:null}
          {row.error?<p className={css.uploadError}>{error(row.error)}</p>:null}
          {row.error&&(row.errorDetails?.status||row.errorDetails?.requestId||row.errorDetails?.stage)?<p className={css.uploadLimits}>{[row.errorDetails.status?t('upload.error.httpStatus',{status:row.errorDetails.status}):null,row.errorDetails.requestId?t('upload.error.requestId',{id:row.errorDetails.requestId}):null,row.errorDetails.stage?t(materialUploadStageKey(row.errorDetails.stage)):null].filter(Boolean).join(' · ')}</p>:null}
          <div className={css.uploadActions}>{row.phase==='queued'||row.phase==='uploading'||row.phase==='registering'?<button type="button" onClick={()=>pauseUpload(row.id)}>{t('upload.pause')}</button>:null}{row.submitted&&(row.phase==='failed'||row.phase==='paused')?<button type="button" onClick={()=>retryUpload(row.id)}>{t('upload.retry')}</button>:null}{!row.submitted&&row.phase==='failed'&&['UPLOAD_SERVICE_UNAVAILABLE','UNAUTHORIZED'].includes(row.error||'')?<button type="button" disabled={uploads.intaking} aria-label={t('upload.recheckFile',{filename:row.filename})} onClick={()=>retryUpload(row.id)}>{t('upload.recheck')}</button>:null}{!row.submitted?<button type="button" aria-label={t('upload.replaceFile',{filename:row.filename})} onClick={()=>{if(!picker.current)return;replacement.current=row.id;uploadRole.current=row.role;picker.current.accept=MATERIAL_ACCEPT[row.role];picker.current.multiple=false;picker.current.click()}}>{t('upload.replace')}</button>:null}{!row.submitted?<button type="button" aria-label={t('upload.removeFile',{filename:row.filename})} onClick={()=>removeUpload(row.id)}>{t('upload.removeRecord')}</button>:null}</div>
        </li>)}</ol>
        {submittedFailure?<p role="status" className={css.uploadError}>{t('upload.partialPending')}</p>:null}
        {pending?<button type="button" className={css.confirmUpload} data-material-confirm disabled={uploads.busy||uploads.batchActive||uploads.intaking||uncheckedFailure} onClick={()=>void confirmUploads()}>{t('upload.confirm',{count:pending})}</button>:null}
        {uploads.intaking?<p role="status" className={css.uploadLimits}>{t('upload.checking')}</p>:null}
        {uploads.syncFailed?<div role="status" className={css.uploadError}>{t('upload.syncPending')}<button type="button" disabled={uploads.syncing} onClick={()=>void syncUploads()}>{t('material.retryContext')}</button></div>:null}
      </section>
    </details>:null}
    {uploads.intaking&&!uploads.rows.length?<span role="status" className={css.uploadNotice}>{t('upload.checking')}</span>:null}
    {uploads.intakeError?<span role="status" className={css.uploadNotice}>{error(uploads.intakeError)}</span>:null}
    {notice?<span role="status" className={css.uploadNotice}>{notice}</span>:null}
  </div>
}
