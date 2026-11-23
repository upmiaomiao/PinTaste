/** Task-matched previews and private uploads are opened from an actual conversation decision. */
import {useEffect,useRef,useState} from 'react'
import type {InjectFace,PropsLocale,PropsRuntime,PropsStore} from '@deepseek-ai/dsh-client-ui-slots'
import type {createBananaPanelStore} from './store.ts'
import type {TemplateLibraryChooserInjected} from './TemplateLibraryChooser.tsx'
import {TemplateLibraryChooser} from './TemplateLibraryChooser.tsx'
import css from './TemplateDecisionPanel.module.css'
import {templateActionMessageKey} from './template-action-error.ts'
import {TEMPLATE_PAGE_SIZE,TemplateBrowserPagination,useTemplateBrowser,useTemplateScroll} from './template-browser.tsx'

export interface TemplateDecisionInjected extends TemplateLibraryChooserInjected {
  loadRecommendations(handle:string):Promise<void>
  confirmTemplateChoice(handle:string,choice:{source_id:string}|{material_id:string}):Promise<boolean>
  deleteMaterial(handle: string, materialId: string, revision: number, requestId: string): Promise<import('./template-library.ts').MaterialDeletionResult>
  uploadPrivateTemplate(handle:string,file:File):Promise<void>
}
type Props=PropsRuntime<'wuyu.work.templates'>&PropsLocale<'bananaPanel'>&PropsStore<ReturnType<typeof createBananaPanelStore>>&InjectFace<TemplateDecisionInjected>
const categoryLabels = {
  '整套PPT': 'templateLibrary.categoryDeck',
  '工艺流程图': 'templateLibrary.categoryProcess',
  '技术路线': 'templateLibrary.categoryRoadmap',
  '摘要图': 'templateLibrary.categoryAbstract',
  '论文方法图': 'templateLibrary.categoryMethods',
} as const
const imageCategories = ['工艺流程图', '技术路线', '摘要图', '论文方法图'] as const

/** Administrative review remains available only through its existing protected Settings link. */
export function TemplateDecisionSurface(props:Props){
  return props.mode==='review'?<TemplateLibraryChooser {...props}/>:<TemplateDecisionPanel {...props}/>
}

function TemplateDecisionPanel({useStore,useTemplateLibrary,loadRecommendations,loadTemplateLibrary,inspectTemplate,loadTemplateMaterials,confirmTemplateChoice,uploadPrivateTemplate,t}:Props){
  const handle=useStore(state=>state.projectHandle),snapshot=useTemplateLibrary(state=>state)
  const [tab,setTab]=useState<'recommended'|'all'|'upload'>('recommended'),[file,setFile]=useState<File|null>(null),[query,setQuery]=useState(''),[category,setCategory]=useState('整套PPT'),[materialId,setMaterialId]=useState<string|null>(null),[large,setLarge]=useState<number|null>(null),[loaded,setLoaded]=useState<Set<string>>(()=>new Set())
  const fileInput=useRef<HTMLInputElement>(null),dialog=useRef<HTMLDialogElement>(null)
  const scroll=useTemplateScroll<HTMLElement>(handle??'')
  const browser=useTemplateBrowser(snapshot,tab==='all',loadTemplateLibrary)
  useEffect(()=>{if(handle)void loadRecommendations(handle)},[handle,loadRecommendations])
  const material=snapshot.materials.find(item=>item.id===materialId)
  const pages=material?material.pages?.map(page=>({id:material.id+':'+page.number,title:t('templateDecision.page',{number:page.number}),image_url:page.image_url}))??[]:snapshot.detail?.pages??[]
  const activeId=material?.id??snapshot.detail?.id??null
  const pageSignature=pages.map(page=>page.image_url).join('|')
  const pollStarted=useRef(0)
  useEffect(()=>{setLoaded(new Set());setLarge(null)},[activeId,pageSignature])
  useEffect(()=>{pollStarted.current=Date.now()},[handle,file])
  useEffect(()=>{
    if(large!==null&&!dialog.current?.open)dialog.current?.showModal()
    if(large===null&&dialog.current?.open)dialog.current.close()
  },[large])
  useEffect(()=>{
    if(!handle||Date.now()-pollStarted.current>120000||!snapshot.materials.some(item=>!item.ready&&!['failed','cancelled'].includes(item.status??'')))return
    const timer=setTimeout(()=>{void loadTemplateMaterials(handle)},1500)
    return()=>clearTimeout(timer)
  },[handle,snapshot.materials,loadTemplateMaterials])
  const cards=tab==='all'?browser.cards:snapshot.recommended
  const loadAll=(nextCategory=category)=>{scroll.capture();void loadTemplateLibrary({search:query,...(nextCategory?{template_category:nextCategory}:{}),offset:0,limit:TEMPLATE_PAGE_SIZE},'library')}
  const selectCategory=(nextCategory:string)=>{setCategory(nextCategory);loadAll(nextCategory)}
  const title=material?.filename??snapshot.detail?.title
  const required=snapshot.detail?.reference_scope==='single_image'||material||['private_generated_design','private_uploaded_template'].includes(snapshot.detail?.preview_kind ?? '')?1:4
  const canConfirm=!!handle&&!!activeId&&!snapshot.busy&&pages.length>=required&&loaded.size>=required
  const confirm=()=>{if(!handle||!canConfirm)return;void confirmTemplateChoice(handle,material?{material_id:material.id}:{source_id:snapshot.detail!.id})}
  return <section ref={scroll.root} onScrollCapture={scroll.onScrollCapture} className={css.root} aria-label={t('templateDecision.title')}>
    <p className={css.intro}>{t('templateDecision.intro')}</p>
    <nav className={css.tabs} aria-label={t('templateDecision.tabs')}>
      {(['recommended','all','upload']as const).map(value=><button type="button" key={value} aria-pressed={tab===value} onClick={()=>{setTab(value);setMaterialId(null);if(value==='all')loadAll()}}>{t(`templateDecision.${value}`)}</button>)}
    </nav>
    {(snapshot.actionError||tab==='recommended'&&snapshot.recommendationPhase==='error')?<p className={css.error} role="alert">{t(templateActionMessageKey(snapshot.actionFailure))}<button type="button" disabled={snapshot.busy} onClick={()=>{if(handle)void loadRecommendations(handle)}}>{t('templateAction.refreshTemplate')}</button></p>:null}
    {tab==='upload'?<div className={css.upload}>
      <input ref={fileInput} type="file" hidden accept=".pptx,.pdf,.png,.jpg,.jpeg,.webp" aria-label={t('templateDecision.file')} onChange={event=>{setFile(event.target.files?.[0]??null);event.target.value=''}}/>
      <button type="button" className={css.fileButton} disabled={snapshot.busy} onClick={()=>fileInput.current?.click()}><svg viewBox="0 0 24 24" width="22" height="22" fill="none" aria-hidden="true"><path d="M12 3v12m-4-8 4-4 4 4M4 15v6h16v-6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/></svg>{file?.name??t('templateDecision.file')}</button>
      <small>{snapshot.templateLimitsStatus==='ready'?t('templateAction.uploadHint',{bytes:snapshot.maxTemplateBytes,mb:Number((snapshot.maxTemplateBytes/1024/1024).toFixed(2))}):t(snapshot.templateLimitsStatus==='error'?'templateAction.limitsUnavailable':'templateAction.limitsLoading')}</small>
      <button type="button" disabled={!handle||snapshot.busy||snapshot.templateLimitsStatus==='loading'} onClick={()=>{if(handle)void loadTemplateMaterials(handle)}}>{t('templateAction.refreshLimits')}</button>
      {file&&snapshot.templateLimitsStatus==='ready'&&file.size>snapshot.maxTemplateBytes?<p className={css.error} role="alert">{t('templateAction.uploadLimit')}</p>:null}
      {file?<button type="button" className={css.primary} disabled={!handle||snapshot.busy||snapshot.templateLimitsStatus!=='ready'||file.size>snapshot.maxTemplateBytes||file.size===0} onClick={()=>{if(handle)void uploadPrivateTemplate(handle,file)}}>{t(snapshot.busy?'templateDecision.uploading':'templateDecision.uploadAction')}</button>:null}
      <p className={css.note}>{t('templateDecision.private')}</p>
      <div className={css.options}>{snapshot.materials.map(item=><button type="button" key={item.id} disabled={!item.ready||snapshot.busy} className={css.option} aria-pressed={materialId===item.id} onClick={()=>setMaterialId(item.id)}><strong>{item.filename}</strong><span>{t(item.ready?'templateDecision.uploadReady':item.status==='failed'?'templateDecision.uploadFailed':'templateLibrary.parsing')}</span></button>)}</div>
    </div>:<>
      {tab==='all'?<div className={css.categories} role="group" aria-label={t('templateLibrary.template_category')}>
        <button type="button" aria-pressed={category==='整套PPT'} onClick={()=>selectCategory('整套PPT')}>{t('templateLibrary.groupPpt')}</button>
        <button type="button" aria-pressed={category!=='整套PPT'} onClick={()=>{if(category==='整套PPT')selectCategory(imageCategories[0])}}>{t('templateLibrary.groupImage')}</button>
      </div>:null}
      {tab==='all'&&category!=='整套PPT'?<div className={css.categories} role="group" aria-label={t('templateLibrary.groupImage')}>
        {imageCategories.filter(value=>snapshot.facets.template_category.includes(value)).map(value=><button type="button" key={value} aria-pressed={category===value} onClick={()=>selectCategory(value)}>{t(categoryLabels[value])}</button>)}
      </div>:null}
      {tab==='all'?<form className={css.search} onSubmit={event=>{event.preventDefault();loadAll()}}><input type="search" maxLength={200} aria-label={t('templateLibrary.search')} placeholder={t('templateLibrary.search')} value={query} onChange={event=>setQuery(event.target.value)}/><button type="submit">{t('templateDecision.search')}</button></form>:null}
      {(tab==='all'?snapshot.phase==='loading':snapshot.recommendationPhase==='loading')?<p role="status">{t('templateLibrary.loading')}</p>:null}
      {tab==='all'&&snapshot.phase==='error'?<p className={css.error} role="alert">{t('templateLibrary.unavailable')}<button type="button" onClick={()=>{scroll.capture();void loadTemplateLibrary({...snapshot.filters,limit:TEMPLATE_PAGE_SIZE},'library')}}>{t('templateLibrary.retry')}</button></p>:null}
      <div id={browser.optionsId} className={css.options} aria-busy={tab==='all'&&snapshot.phase==='loading'}>{cards.map(card=><button type="button" data-template-card={card.id} className={css.option} key={card.id} aria-pressed={!materialId&&snapshot.detail?.id===card.id} onClick={()=>{scroll.capture();setMaterialId(null);void inspectTemplate(card.id)}}>{card.cover_url?<img src={card.cover_url} alt="" loading="lazy"/>:null}<span><strong>{card.title}</strong><span>{card.recommendation_reason??card.tags.purpose.join(' · ')}</span><small>{t('templateDecision.previewCount',{count:card.pages?.length??card.page_count})}</small></span></button>)}</div>
      {!cards.length&&(tab==='all'?snapshot.phase==='ready':snapshot.recommendationPhase==='ready')?<p className={css.note}>{t(tab==='all'?'templateLibrary.empty':'templateDecision.noRecommendations')}</p>:null}
      {tab==='all'?<TemplateBrowserPagination page={browser.page} disabled={snapshot.busy||snapshot.phase==='loading'} controls={browser.optionsId} buttonClassName={css.more} label={t('templateBrowser.pagination')} status={t('templateBrowser.page',{page:browser.page.page,pages:browser.page.pages,count:browser.page.total})} previous={t('templateDecision.previous')} next={t('templateDecision.next')} onPage={offset=>{scroll.capture();void loadTemplateLibrary({...snapshot.filters,offset,limit:TEMPLATE_PAGE_SIZE},'library')}}/>:null}
    </>}
    {title?<section className={css.preview} aria-label={t('templateDecision.preview')}><header><h3>{title}</h3><span>{t('templateDecision.previewCount',{count:pages.length})}</span></header>
      <p className={css.note}>{!material&&snapshot.detail?.preview_kind==='original_template_render'?snapshot.detail.preview_disclaimer:t(material||['private_generated_design','private_uploaded_template'].includes(snapshot.detail?.preview_kind ?? '')?'templateDecision.originalPrivate':'templateDecision.neutral')}</p>
      <div className={css.pages}>{pages.map((page,index)=><button type="button" key={page.id} aria-label={t('templateDecision.enlarge',{number:index+1})} onClick={()=>setLarge(index)}><img src={page.image_url} alt={page.title} loading={index<4?'eager':'lazy'} onLoad={()=>setLoaded(previous=>new Set([...previous,page.id]))}/><span>{index+1}. {page.title}</span></button>)}</div>
      {pages.length<required?<p className={css.note}>{t('templateDecision.insufficient')}</p>:null}
      <footer className={css.footer}><span>{t(snapshot.detail?.reference_scope==='single_image'?'templateDecision.singleReviewFirst':'templateDecision.reviewFirst')}</span><button type="button" className={css.primary} disabled={!canConfirm} onClick={confirm}>{t(snapshot.busy?'templateDecision.confirming':'templateDecision.confirm')}</button></footer>
    </section>:null}
    <dialog ref={dialog} className={css.lightbox} aria-label={t('templateDecision.preview')} onCancel={()=>setLarge(null)} onClose={()=>setLarge(null)}>
      {large!==null&&pages[large]?<><header><strong>{pages[large]!.title}</strong><button type="button" aria-label={t('templateLibrary.close')} onClick={()=>setLarge(null)}>×</button></header><img src={pages[large]!.image_url} alt={pages[large]!.title}/><nav><button type="button" disabled={large===0} onClick={()=>setLarge(index=>Math.max(0,(index??0)-1))}>{t('templateDecision.previous')}</button><span>{large+1} / {pages.length}</span><button type="button" disabled={large===pages.length-1} onClick={()=>setLarge(index=>Math.min(pages.length-1,(index??0)+1))}>{t('templateDecision.next')}</button></nav></>:null}
    </dialog>
  </section>
}
