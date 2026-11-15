/**
 * React wrapper for the Banana Slides project panel. Connects to the banana
 * MCP server from the browser, loads the official single-file panel HTML into
 * a sandboxed iframe, and attaches the postMessage host bridge.
 */
import { useEffect, useRef, useState } from 'react'
import type { PropsLocale } from '@deepseek-ai/dsh-client-ui-slots'
import { BANANA_MCP_TOKEN, BANANA_MCP_URL, PANEL_RESOURCE_URI } from './config.ts'
import { connectBananaClient, type BananaClient } from './banana-mcp.ts'
import { attachBananaHost, type BananaHostBridge } from './banana-host.ts'
import { uiLanguage, WORK_UI_LANGUAGE_APPLIED_EVENT } from './ui-language-bridge.ts'
import css from './BananaPanelRow.module.css'
import type { CurrentRequirements } from '../presentation-config.ts'
import type { PreviewTarget } from './preview-target.ts'
interface BananaPanelHostProps {
  currentRequirements: CurrentRequirements | null
  /** Opaque project handle the panel loads and operates on. */
  projectHandle: string
  t: PropsLocale<'bananaPanel'>['t']
  target: PreviewTarget
  targetRevision: number
  writeToConversation(text:string):Promise<void>
  templateRequest(handle:string,request:unknown):Promise<unknown>
}
type Phase = 'connecting' | 'loading' | 'ready' | 'error'
/** Host the banana panel for one project handle. */
export function BananaPanelHost({ currentRequirements, projectHandle, target, targetRevision, writeToConversation, templateRequest, t }: BananaPanelHostProps) {
  const [phase, setPhase] = useState<Phase>('connecting')
  const [error, setError] = useState<string | null>(null)
  const iframeRef = useRef<HTMLIFrameElement | null>(null)
  const bridgeRef=useRef<BananaHostBridge|null>(null),targetRef=useRef(target),messageRef=useRef(writeToConversation)
  targetRef.current=target;messageRef.current=writeToConversation
  const requirementsRef=useRef(currentRequirements);requirementsRef.current=currentRequirements
  useEffect(()=>{bridgeRef.current?.notifyRequirements(currentRequirements)},[currentRequirements])
  const templateRef=useRef(templateRequest);templateRef.current=templateRequest
  useEffect(()=>{bridgeRef.current?.notifyPreview({...target})},[targetRevision,target.view,target.page_number])
  useEffect(() => {
    const changed = (event: Event) => {
      const language = uiLanguage((event as CustomEvent<{ language?: unknown }>).detail?.language)
      if (language !== null) bridgeRef.current?.notifyLocale(language)
    }
    window.addEventListener(WORK_UI_LANGUAGE_APPLIED_EVENT, changed)
    return () => { window.removeEventListener(WORK_UI_LANGUAGE_APPLIED_EVENT, changed) }
  }, [])
  useEffect(() => {
    let disposed = false
    let client: BananaClient | null = null
    let bridge: BananaHostBridge | null = null
    async function boot(): Promise<void> {
      try {
        setPhase('connecting')
        client = await connectBananaClient(BANANA_MCP_URL, BANANA_MCP_TOKEN)
        if (disposed) { client.dispose(); return }
        setPhase('loading')
        const html = await client.readResource(PANEL_RESOURCE_URI)
        if (disposed) { client.dispose(); return }
        const iframe = iframeRef.current
        if (iframe === null) { client.dispose(); return }
        iframe.srcdoc = html
        bridge = attachBananaHost({
          iframe,
          client,
          uiLanguage: uiLanguage(document.documentElement.lang) ?? 'zh-CN',
          toolInput: { project_handle: projectHandle, preview_target: targetRef.current },
          currentRequirements: requirementsRef.current,
          onMessage: (text)=>messageRef.current(text),
          onTemplates: (handle,request)=>templateRef.current(handle,request),
        })
        bridgeRef.current=bridge
        setPhase('ready')
      } catch (err) {
        if (!disposed) {
          setPhase('error')
          setError(t('host.safeError'))
        }
      }
    }
    void boot()
    return () => {
      disposed = true
      bridge?.dispose()
      bridgeRef.current=null
      client?.dispose()
    }
  }, [projectHandle])
  return (
    <div className={css.host} data-phase={phase}>
      {phase === 'connecting' || phase === 'loading' ? (
        <div className={css.loading}>{phase === 'connecting' ? t('host.connecting') : t('host.loading')}</div>
      ) : null}
      {phase === 'error' ? (
        <div className={css.error}>{t('host.error')}{error ?? ''}</div>
      ) : null}
      <iframe
        ref={iframeRef}
        className={css.iframe}
        title={t('host.iframeTitle')}
        sandbox="allow-scripts allow-same-origin allow-popups allow-forms allow-downloads"
      />
    </div>
  )
}
