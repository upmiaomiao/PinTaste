/** UI-only paging and scroll retention; no template choice or workflow actions. */
import {useEffect,useId,useLayoutEffect,useRef} from 'react'
import type {TemplateFilters,TemplateLibrarySnapshot,NeutralTemplateCard} from './template-library.ts'

export const TEMPLATE_PAGE_SIZE=7
export function templateFilterKey(filters:TemplateFilters,tab:string){
  return JSON.stringify([tab,Object.entries(filters).filter(([key,value])=>!['offset','limit'].includes(key)&&value!==undefined&&value!=='').sort(([a],[b])=>a.localeCompare(b))])
}
export function templatePage(total:number,offset=0){
  const count=Number.isSafeInteger(total)&&total>=0?total:0
  const requested=Number.isSafeInteger(offset)&&offset>=0?offset:0
  const pages=Math.max(1,Math.ceil(count/TEMPLATE_PAGE_SIZE)),lastOffset=(pages-1)*TEMPLATE_PAGE_SIZE
  const current=Math.min(lastOffset,Math.floor(requested/TEMPLATE_PAGE_SIZE)*TEMPLATE_PAGE_SIZE)
  return {total:count,pages,page:Math.floor(current/TEMPLATE_PAGE_SIZE)+1,offset:current,lastOffset,needsCorrection:current!==requested}
}
export function useTemplateBrowser(snapshot:TemplateLibrarySnapshot,enabled:boolean,load:(filters?:TemplateFilters,tab?:TemplateLibrarySnapshot['tab'])=>Promise<void>){
  const key=templateFilterKey(snapshot.filters,snapshot.tab)
  const ready=useRef<{key:string,cards:NeutralTemplateCard[],total:number}|null>(null)
  if(snapshot.phase==='ready')ready.current={key,cards:snapshot.items.slice(0,TEMPLATE_PAGE_SIZE),total:snapshot.total}
  const current=ready.current?.key===key?ready.current:null
  const page=templatePage(current?.total??0,snapshot.filters.offset)
  const correction=useRef(''),optionsId=useId()
  useEffect(()=>{
    if(!enabled||snapshot.phase!=='ready')return
    if(!page.needsCorrection){correction.current='';return}
    if(snapshot.filters.limit!==TEMPLATE_PAGE_SIZE)return
    const stamp=JSON.stringify([key,snapshot.filters.offset,page.total,page.offset])
    if(correction.current===stamp)return
    correction.current=stamp
    void load({...snapshot.filters,offset:page.offset,limit:TEMPLATE_PAGE_SIZE},snapshot.tab)
  },[enabled,snapshot.phase,snapshot.filters,key,page.needsCorrection,page.total,page.offset,load,snapshot.tab])
  return {cards:current?.cards??[],page,optionsId}
}

/** Preserve every scrolled container within this stable surface across detail/status renders. */
export function useTemplateScroll<T extends HTMLElement>(scope:string){
  const root=useRef<T>(null),positions=useRef(new Map<HTMLElement,{top:number,left:number}>()),previousScope=useRef(scope)
  const remember=(node:HTMLElement)=>positions.current.set(node,{top:node.scrollTop,left:node.scrollLeft})
  const onScrollCapture=(event:{target:EventTarget|null})=>{
    const node=event.target
    if(node instanceof HTMLElement&&root.current?.contains(node))remember(node)
  }
  const capture=()=>{
    const node=root.current;if(!node)return
    for(const target of [node,...node.querySelectorAll<HTMLElement>('*')])if(target.scrollTop||target.scrollLeft||positions.current.has(target))remember(target)
  }
  useLayoutEffect(()=>{
    const owner=root.current;if(!owner)return
    if(previousScope.current!==scope){positions.current.clear();previousScope.current=scope;owner.scrollTop=owner.scrollLeft=0;return}
    for(const [node,position]of positions.current){
      if(node!==owner&&!owner.contains(node)){positions.current.delete(node);continue}
      node.scrollTop=position.top;node.scrollLeft=position.left
    }
  })
  return {root,onScrollCapture,capture}
}

export function TemplateBrowserPagination({page,disabled,onPage,label,status,previous,next,controls,buttonClassName}: {
  page:ReturnType<typeof templatePage>,disabled:boolean,onPage:(offset:number)=>void,label:string,status:string,
  previous:string,next:string,controls:string,buttonClassName?:string
}){
  if(page.pages<=1)return null
  return <nav aria-label={label} data-template-pager style={{display:'flex',alignItems:'center',flexWrap:'wrap',gap:8,marginTop:12}}>
    <button type="button" className={buttonClassName} style={{marginTop:0}} aria-controls={controls} disabled={disabled||page.page===1} onClick={()=>onPage(Math.max(0,page.offset-TEMPLATE_PAGE_SIZE))}>{previous}</button>
    <span role="status" aria-live="polite" aria-atomic="true" aria-current="page">{status}</span>
    <button type="button" className={buttonClassName} style={{marginTop:0}} aria-controls={controls} disabled={disabled||page.page===page.pages} onClick={()=>onPage(page.offset+TEMPLATE_PAGE_SIZE)}>{next}</button>
  </nav>
}
